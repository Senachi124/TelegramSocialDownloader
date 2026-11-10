"""Offline provider regressions with synthetic payloads and no live sessions."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
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


if __name__ == '__main__':
    unittest.main()
