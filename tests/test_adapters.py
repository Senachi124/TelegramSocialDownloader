"""Offline provider regressions with synthetic payloads and no live sessions."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from media_bot import instagram, tiktok, twitter


class RoutingAndSelectionTests(unittest.TestCase):
    def test_tiktok_only_video_posts_use_video_engine(self):
        samples = (
            ('https://www.tiktok.com/@example/video/123', 'post', True),
            ('https://www.tiktok.com/@example/photo/123', 'post', False),
            ('https://www.tiktok.com/@example/video/123', 'stories', False),
            ('https://www.tiktok.com/@example/stories', 'stories', False),
        )
        for url, mode, expected in samples:
            with self.subTest(url=url, mode=mode):
                self.assertEqual(tiktok.uses_yt_dlp({'platform': 'tiktok', 'mode': mode, 'url': url}), expected)

    def test_tiktok_story_target_and_credentials_are_explicit(self):
        self.assertEqual(tiktok.target('https://www.tiktok.com/@example/video/123', 'stories'),
                         'https://www.tiktok.com/@example/stories')
        options = tiktok.options({'sessionid': 'fixture'})
        self.assertFalse(options['cookies-update'])
        self.assertFalse(options['netrc'])
        self.assertIsNone(options['password'])
        self.assertEqual(options['cookies'], {'sessionid': 'fixture'})

    def test_instagram_keeps_media_downloader_and_disables_previews(self):
        options = instagram.options({'sessionid': 'fixture'})
        self.assertEqual(options['videos'], 'merged')
        self.assertEqual(options['user-cache'], 'memory')
        self.assertFalse(options['previews'])

    def test_twitter_original_photo_and_highest_bitrate_mp4(self):
        media = NS(
            photos=[NS(fullUrl='https://pbs.twimg.com/media/photo?format=png&name=orig')],
            videos=[NS(variants=[
                NS(contentType='video/mp4', bitrate=100, url='https://video.twimg.com/low.mp4'),
                NS(contentType='video/mp4', bitrate=900, url='https://video.twimg.com/high.mp4'),
                NS(contentType='application/x-mpegURL', bitrate=9999, url='https://video.twimg.com/list.m3u8'),
            ])],
            animated=[NS(videoUrl='https://video.twimg.com/animated.mp4')],
        )
        self.assertEqual(twitter.assets(NS(media=media)), [
            ('https://pbs.twimg.com/media/photo?format=png&name=orig', 'png'),
            ('https://video.twimg.com/high.mp4', 'mp4'),
            ('https://video.twimg.com/animated.mp4', 'mp4'),
        ])

    def test_twitter_no_media_is_distinct_from_unsupported_video(self):
        self.assertEqual(twitter.assets(NS(media=None)), [])
        with self.assertRaisesRegex(ValueError, 'unsupported_video'):
            twitter.assets(NS(media=NS(videos=[NS(variants=[])])))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[1] / 'artifacts'
        artifacts.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=artifacts)
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {'MEDIA_BOT_DATA': str(self.root)})
        self.environment.start()
        from media_bot import core, twitter_runner, yt_runner
        self.core, self.runner, self.yt = core, twitter_runner, yt_runner
        self.data = patch.object(core, 'DATA', self.root)
        self.data.start()
        core.initialize()

    def tearDown(self):
        self.data.stop()
        self.environment.stop()
        self.tmp.cleanup()

    def result(self):
        return dict(code=1, retry_at=0, auth_required=False, files=[], requests=0,
                    media_attempts=0, wait_reason='')

    def test_verified_twitter_receipt_skips_network_and_quota(self):
        path = self.root / 'picture.jpg'
        content = b'synthetic-picture'
        path.write_bytes(content)
        path.with_suffix('.jpg.receipt.json').write_text(json.dumps({
            'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}))
        client = Mock()
        with patch.object(self.core, 'reserve_media') as reserve:
            asyncio.run(self.runner.fetch(client, 'https://pbs.twimg.com/media/fixture.jpg', path, self.result()))
        client.stream.assert_not_called()
        reserve.assert_not_called()

    def test_twitter_media_rejects_non_provider_destinations(self):
        for url in ('http://pbs.twimg.com/file', 'https://twimg.com.evil.test/file',
                    'https://user@pbs.twimg.com/file', 'https://pbs.twimg.com:8443/file'):
            with self.subTest(url=url), self.assertRaises(self.runner.CaptureFailure):
                self.runner.media_url(url)

    def test_twitter_media_limit_stops_before_network(self):
        client = Mock()
        with patch.object(self.core, 'reserve_media', side_effect=self.core.RateLimited(1234)):
            with self.assertRaises(self.core.RateLimited):
                asyncio.run(self.runner.fetch(client, 'https://pbs.twimg.com/media/a.jpg',
                                              self.root / 'a.jpg', self.result()))
        client.stream.assert_not_called()

    def test_twitter_download_records_checksum_and_preserves_original_on_failure(self):
        response = Mock(status_code=200, headers={'content-type': 'image/jpeg', 'content-length': '3'})
        async def chunks():
            yield b'abc'
        response.aiter_bytes = chunks
        @asynccontextmanager
        async def stream(*args):
            yield response
        client = NS(stream=stream)
        path = self.root / 'a.jpg'
        with patch.object(self.core, 'reserve_media', return_value=True):
            result = self.result()
            asyncio.run(self.runner.fetch(client, 'https://pbs.twimg.com/media/a.jpg', path, result))
            self.assertEqual(path.read_bytes(), b'abc')
            receipt = json.loads(path.with_suffix('.jpg.receipt.json').read_text())
            self.assertEqual(receipt['sha256'], hashlib.sha256(b'abc').hexdigest())
            self.assertEqual(result['media_attempts'], 1)
            path.write_bytes(b'original')
            response.headers['content-length'] = '99'
            with self.assertRaises(self.runner.CaptureFailure):
                asyncio.run(self.runner.fetch(client, 'https://pbs.twimg.com/media/a.jpg', path, self.result()))
            self.assertEqual(path.read_bytes(), b'original')
            self.assertEqual(path.with_suffix('.jpg.part').read_bytes(), b'abc')

    def test_twitter_runner_suppresses_provider_dumps_and_raw_error_text(self):
        original = self.runner.models._write_dump
        async def capture(payload, result):
            self.assertIsNot(self.runner.models._write_dump, original)
            self.runner.models._write_dump('SYNTHETIC_PRIVATE_VALUE')
            raise RuntimeError('SYNTHETIC_PRIVATE_VALUE')
        payload = {'root': str(self.root / 'capture'), 'cookies': {}}
        with patch.object(self.runner, 'capture', side_effect=capture):
            self.runner.run(payload)
        text = (Path(payload['root']) / 'result.json').read_text()
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE', text)
        self.assertEqual(json.loads(text)['failure'], 'extractor_error')
        self.assertIs(self.runner.models._write_dump, original)

    def test_twitter_quota_result_records_retry_without_exposing_cookies(self):
        payload = {'root': str(self.root / 'quota'), 'cookies': {'ct0': 'fixture-sensitive'}}
        with patch.object(self.runner, 'capture', new=AsyncMock(side_effect=self.core.RateLimited(1234))):
            self.runner.run(payload)
        text = (Path(payload['root']) / 'result.json').read_text()
        result = json.loads(text)
        self.assertEqual((result['retry_at'], result['wait_reason']), (1234, 'quota'))
        self.assertNotIn('fixture-sensitive', text)

    def test_tiktok_video_engine_has_no_persistent_cookie_or_cache_files(self):
        captured = {}
        def extract(downloader, url, **kwargs):
            captured.update(downloader.params)
            self.assertEqual({cookie.name: cookie.value for cookie in downloader.cookiejar}['sessionid'], 'fixture')
            self.assertEqual(kwargs['ie_key'], 'TikTok')
            (self.root / '123_00_.mp4').write_bytes(b'fixture-video')
            return {'id': '123'}
        with patch.object(self.yt.YoutubeDL, 'extract_info', extract):
            result = self.yt.run({'root': str(self.root), 'url': 'https://www.tiktok.com/@example/video/123',
                                  'cookies': {'sessionid': 'fixture'}})
        self.assertEqual(result['code'], 0)
        self.assertFalse(captured['cachedir'])
        self.assertFalse(captured['usenetrc'])
        self.assertIsNone(captured['cookiefile'])
        self.assertEqual(len(result['files']), 1)

    def test_tiktok_raw_failure_is_not_persisted(self):
        with patch.object(self.yt.YoutubeDL, 'extract_info', side_effect=RuntimeError('SYNTHETIC_PRIVATE_VALUE')):
            result = self.yt.run({'root': str(self.root), 'url': 'https://www.tiktok.com/@example/video/123', 'cookies': {}})
        self.assertEqual(result['code'], 1)
        self.assertEqual(result['failure'], 'extractor_error')
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE', (self.root / 'result.json').read_text())

    def select_tiktok_formats(self, formats):
        """Exercise the runner and yt-dlp selection, replacing only provider I/O."""
        from yt_dlp.extractor.tiktok import TikTokIE
        selected = []
        def extract(extractor, url):
            return {'id': '123', 'title': 'synthetic', 'formats': formats}
        def download(downloader, name, info, subtitle=False, test=False):
            selected.append(info['format_id'])
            Path(name).write_bytes(b'synthetic-video')
            return True, True
        with patch.object(TikTokIE, '_real_extract', extract), \
                patch.object(self.yt.YoutubeDL, 'dl', download), \
                patch.object(self.yt.YoutubeDL, 'urlopen', side_effect=AssertionError('Unexpected network')):
            result = self.yt.run({'root': str(self.root / 'capture'),
                                  'url': 'https://www.tiktok.com/@example/video/123', 'cookies': {}})
        return selected, result

    @staticmethod
    def tiktok_format(name, codec='h264', height=720, protocol='https', **extra):
        return {'format_id': name, 'url': f'https://example.test/{name}.mp4',
                'ext': 'mp4', 'vcodec': codec, 'acodec': 'aac',
                'height': height, 'width': round(height * 16 / 9),
                'protocol': protocol, **extra}

    def test_tiktok_prefers_h264_over_hevc_at_same_resolution(self):
        selected, result = self.select_tiktok_formats([
            self.tiktok_format('avc', tbr=2800), self.tiktok_format('hevc', codec='h265', tbr=1200)])
        self.assertEqual(selected, ['avc'])
        self.assertEqual((result['code'], result['media_attempts']), (0, 1))

    def test_tiktok_selects_best_h264_and_accepts_avc1_alias(self):
        selected, result = self.select_tiktok_formats([
            self.tiktok_format('avc-low', height=360),
            self.tiktok_format('avc-high', codec='avc1.64001f'),
            self.tiktok_format('hevc-higher', codec='h265', height=1080)])
        self.assertEqual(selected, ['avc-high'])
        self.assertEqual(result['code'], 0)

    def test_tiktok_falls_back_to_complete_hevc_without_h264_audio(self):
        selected, result = self.select_tiktok_formats([
            self.tiktok_format('silent-avc', acodec='none'),
            self.tiktok_format('hevc', codec='h265')])
        self.assertEqual(selected, ['hevc'])
        self.assertEqual(result['code'], 0)

    def test_tiktok_keeps_http_fallback(self):
        selected, result = self.select_tiktok_formats([
            self.tiktok_format('http-hevc', codec='h265', protocol='http',
                               url='http://example.test/video.mp4')])
        self.assertEqual(selected, ['http-hevc'])
        self.assertEqual(result['code'], 0)

    def test_tiktok_rejects_hls_even_when_h264(self):
        selected, result = self.select_tiktok_formats([
            self.tiktok_format('hls', protocol='m3u8_native')])
        self.assertEqual(selected, [])
        self.assertEqual(result['code'], 1)
        self.assertEqual(result['media_attempts'], 0)

    def transfer_tiktok(self, codec='h264', format_id='avc', quota_blocked=False):
        """Run a real HTTP downloader against a server that supports Range."""
        from yt_dlp.extractor.tiktok import TikTokIE
        body = b'synthetic-complete-avc-video-' * 1024
        ranges = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                header = self.headers.get('Range')
                ranges.append(header)
                offset = int(header.split('=')[1].split('-')[0]) if header else 0
                self.send_response(206 if header else 200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(len(body) - offset))
                self.send_header('Accept-Ranges', 'bytes')
                if header:
                    self.send_header('Content-Range', f'bytes {offset}-{len(body)-1}/{len(body)}')
                self.end_headers()
                self.wfile.write(body[offset:])
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        info = {'id': '123', 'title': 'synthetic', 'formats': [self.tiktok_format(
            format_id, codec=codec, protocol='http', filesize=len(body), fps=30,
            url=f'http://127.0.0.1:{server.server_port}/video.mp4')]}
        reserve = self.core.reserve_media
        def budget(*args):
            if quota_blocked:
                raise self.core.RateLimited(1234)
            return reserve(*args)
        try:
            with patch.object(TikTokIE, '_real_extract', lambda extractor, url: info), \
                    patch.object(self.core, 'reserve_media', side_effect=budget):
                result = self.yt.run({'root': str(self.root / 'capture'),
                                      'url': 'https://www.tiktok.com/@example/video/123', 'cookies': {}})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        return result, ranges, body

    def tiktok_partial(self, content=b'old-hevc-prefix'):
        root = self.root / 'capture'
        root.mkdir(exist_ok=True)
        part = root / '123_00_.mp4.part'
        part.write_bytes(content)
        return part

    def test_tiktok_legacy_partial_is_preserved_without_range_resume(self):
        part = self.tiktok_partial()
        result, ranges, body = self.transfer_tiktok()
        self.assertEqual(result['code'], 0)
        self.assertEqual(ranges, [None])
        self.assertEqual(part.with_suffix('').read_bytes(), body)
        preserved = list(part.parent.glob(part.name + '.preserved-*'))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), b'old-hevc-prefix')

    def test_tiktok_changed_format_partial_is_preserved(self):
        result, _, _ = self.transfer_tiktok(codec='h265', format_id='hevc')
        self.assertEqual(result['code'], 0)
        (self.root / 'capture/123_00_.mp4').unlink()
        part = self.tiktok_partial()
        result, ranges, body = self.transfer_tiktok()
        self.assertEqual(result['code'], 0)
        self.assertEqual(ranges, [None])
        self.assertEqual(part.with_suffix('').read_bytes(), body)
        self.assertEqual(next(part.parent.glob(part.name + '.preserved-*')).read_bytes(), b'old-hevc-prefix')
        marker = (part.parent / '123_00_.mp4.format.json').read_text()
        self.assertEqual(json.loads(marker)['format']['vcodec'], 'h264')
        self.assertNotIn('http', marker)

    def test_tiktok_same_format_partial_retains_http_range_resume(self):
        result, _, body = self.transfer_tiktok()
        self.assertEqual(result['code'], 0)
        (self.root / 'capture/123_00_.mp4').unlink()
        part = self.tiktok_partial(body[:1000])
        result, ranges, _ = self.transfer_tiktok()
        self.assertEqual(result['code'], 0)
        self.assertEqual(ranges, ['bytes=1000-'])
        self.assertEqual(part.with_suffix('').read_bytes(), body)
        self.assertEqual(list(part.parent.glob(part.name + '.preserved-*')), [])

    def test_tiktok_quota_block_keeps_partial_and_marker_untouched(self):
        part = self.tiktok_partial()
        marker = part.parent / '123_00_.mp4.format.json'
        marker.write_text('{"old":true}')
        result, ranges, _ = self.transfer_tiktok(quota_blocked=True)
        self.assertEqual(result['wait_reason'], 'quota')
        self.assertEqual(ranges, [])
        self.assertEqual(part.read_bytes(), b'old-hevc-prefix')
        self.assertEqual(marker.read_text(), '{"old":true}')
        self.assertEqual(list(part.parent.glob(part.name + '.preserved-*')), [])

    def test_tiktok_partial_state_symlinks_are_rejected(self):
        root = self.root / 'capture'
        root.mkdir()
        outside = self.root / 'outside'
        outside.write_bytes(b'untouched')
        for suffix in ('.part', '.format.json'):
            candidate = root / ('123_00_.mp4' + suffix)
            candidate.symlink_to(outside)
            with self.subTest(suffix=suffix):
                result, ranges, _ = self.transfer_tiktok()
                self.assertEqual(result['code'], 1)
                self.assertEqual(ranges, [])
                self.assertEqual(outside.read_bytes(), b'untouched')
                self.assertTrue(candidate.is_symlink())
            candidate.unlink()

    def test_tiktok_completed_file_is_not_replaced_after_format_change(self):
        result, _, body = self.transfer_tiktok(codec='h265', format_id='hevc')
        self.assertEqual(result['code'], 0)
        root = self.root / 'capture'
        marker = root / '123_00_.mp4.format.json'
        before = marker.read_bytes()
        result, ranges, _ = self.transfer_tiktok()
        self.assertEqual(result['code'], 0)
        self.assertEqual(ranges, [])
        self.assertEqual((root / '123_00_.mp4').read_bytes(), body)
        self.assertEqual(marker.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
