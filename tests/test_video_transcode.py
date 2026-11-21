"""Original-container delivery and legacy scratch recovery with isolated media."""
import fcntl
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from media_bot import video


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class NativeVideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'fixture.mkv'
        subprocess.run([
            'ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
            'color=c=blue:s=64x96:r=12:d=0.5', '-f', 'lavfi', '-i',
            'sine=frequency=440:duration=0.5', '-c:v', 'ffv1', '-c:a', 'pcm_s16le',
            '-shortest', str(self.source),
        ], check=True, capture_output=True)

    def test_probe_original_without_transcoding(self):
        before = self.source.read_bytes()
        with patch.object(video.subprocess, 'run', wraps=subprocess.run) as commands:
            attrs = video.attributes(self.source)
        self.assertEqual((attrs['width'], attrs['height']), (64, 96))
        self.assertEqual([call.args[0][0] for call in commands.call_args_list], ['ffprobe'])
        self.assertEqual(self.source.read_bytes(), before)

    def test_legacy_cleanup_preserves_active_encoder_and_original(self):
        scratch = self.root / ('.playback-' + '1' * 32 + '.encoding.mp4')
        with scratch.open('wb') as active:
            fcntl.flock(active.fileno(), fcntl.LOCK_EX)
            video.cleanup_playback_scratch(self.root)
            self.assertTrue(scratch.exists())
        video.cleanup_playback_scratch(self.root)
        self.assertFalse(scratch.exists())
        self.assertTrue(self.source.exists())

    def test_youtube_emits_only_the_original(self):
        with patch.dict(os.environ, {'MEDIA_BOT_DATA': str(self.root)}):
            from media_bot import yt_runner
        source = self.root / 'Abc123_X-yZ_00_.mkv'
        shutil.copyfile(self.source, source)
        def extract(*args, **kwargs):
            return {'id': 'Abc123_X-yZ', 'requested_downloads': [{'filepath': str(source)}]}
        payload = dict(platform='youtube', root=str(self.root), url='https://youtu.be/Abc123_X-yZ')
        with patch.object(yt_runner.YoutubeDL, 'extract_info', extract):
            result = yt_runner.run(payload)
        self.assertEqual(result['code'], 0, result)
        item, = result['files']
        self.assertEqual(item, {'path': str(source), 'name': source.name, 'size': source.stat().st_size})
        self.assertEqual(self.source.read_bytes(), source.read_bytes())
        self.assertEqual(list(self.root.glob('*.h265.mp4')), [])

    def test_malformed_http_400_is_an_uncertain_reply(self):
        with patch.dict(os.environ, {'MEDIA_BOT_DATA': str(self.root)}):
            from media_bot import worker
        with ExitStack() as stack:
            session = Mock()
            response = session.post.return_value
            response.status_code = 400
            stack.enter_context(patch.object(worker._transport, 'session', session, create=True))
            stack.enter_context(patch.object(worker.config, 'token', return_value='fixture'))
            for error, payload in ((ValueError('invalid JSON'), None), (None, {'error_code': 400})):
                with self.subTest(payload=payload):
                    response.json.side_effect = error
                    response.json.return_value = payload
                    with self.assertRaises(ValueError):
                        worker.telegram('sendVideo', {'chat_id': 123})

    def test_native_video_is_sent_once_with_identical_document_fallback_only_on_rejection(self):
        with patch.dict(os.environ, {'MEDIA_BOT_DATA': str(self.root), 'MEDIA_BOT_ALLOWED_USERS': '123'}):
            from media_bot import core as c, worker
            with patch.object(c, 'DATA', self.root), patch.object(worker, 'status'):
                c.initialize()

                for outcome, methods, state in (
                    ('accept', ['sendVideo'], 'completed'),
                    ('reject', ['sendVideo', 'sendDocument'], 'completed'),
                    ('unknown', ['sendVideo'], 'partial'),
                ):
                    with self.subTest(outcome=outcome):
                        key = c.enqueue(outcome, 123, 'https://youtu.be/Abc123_X-yZ')
                        job = c.job_by_id(key)
                        target = self.root / 'media' / key / 'native.mkv'
                        target.parent.mkdir()
                        shutil.copyfile(self.source, target)
                        worker.add_files(job, [{'path': str(target), 'name': target.name,
                                                'size': target.stat().st_size}])
                        calls = []
                        def send(method, data, files):
                            name, stream, mime = next(iter(files.values()))
                            self.assertEqual(name, target.name)
                            self.assertEqual(stream.read(), self.source.read_bytes())
                            calls.append(method)
                            if method == 'sendVideo':
                                self.assertEqual((data['width'], data['height']), (64, 96))
                                self.assertEqual(mime, 'video/x-matroska')
                                if outcome == 'reject':
                                    raise worker.TelegramError(400)
                                if outcome == 'unknown':
                                    raise TimeoutError()
                            else:
                                self.assertTrue(data['disable_content_type_detection'])
                            return {'message_id': 32, method[4:].lower(): {}}
                        with patch.object(worker, 'telegram', side_effect=send):
                            worker.deliver(job)
                            worker.deliver(c.job_by_id(key))
                        self.assertEqual(calls, methods)
                        self.assertEqual(c.job_by_id(key)['state'], state)
