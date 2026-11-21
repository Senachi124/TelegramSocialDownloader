"""Local conversion regressions with generated media and no Telegram calls."""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from media_bot import video


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class PlaybackConversionTests(unittest.TestCase):
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

    def test_mkv_becomes_hevc_aac_mp4_and_preserves_source(self):
        original = self.source.read_bytes()
        output = video.telegram_playback(self.source)
        data = json.loads(subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(output),
        ]))
        picture, audio = data['streams']
        self.assertEqual(output.suffix, '.mp4')
        self.assertEqual((picture['codec_name'], picture['codec_tag_string'],
                          picture['width'], picture['height'], picture['r_frame_rate']),
                         ('hevc', 'hvc1', 64, 96, '12/1'))
        self.assertEqual(audio['codec_name'], 'aac')
        self.assertEqual(video.attributes(output)['width'], 64)
        subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(output),
                        '-f', 'null', '-'], check=True, capture_output=True)
        self.assertEqual(self.source.read_bytes(), original)

    def test_failed_encode_never_returns_partial_mp4(self):
        original = self.source.read_bytes()
        real_run = subprocess.run
        def run(command, **kwargs):
            if command[0] == 'ffmpeg':
                Path(command[-1]).write_bytes(b'incomplete')
                raise subprocess.CalledProcessError(1, command)
            return real_run(command, **kwargs)
        with patch.object(video.subprocess, 'run', side_effect=run), self.assertRaises(ValueError):
            video.telegram_playback(self.source)
        self.assertEqual(list(self.root.glob('*.mp4')), [])
        self.assertEqual(self.source.read_bytes(), original)


    def test_exhausted_capture_budget_preserves_original_without_starting_ffmpeg(self):
        result = {'code': 0, 'files': [{'path': str(self.source), 'name': self.source.name,
                                        'size': self.source.stat().st_size}]}
        with patch.dict(os.environ, {'MEDIA_BOT_CAPTURE_DEADLINE': '10'}), \
                patch.object(video.time, 'monotonic', return_value=8), \
                patch.object(video.subprocess, 'run') as run:
            video.prepare_playback_files(result)
        run.assert_not_called()
        self.assertEqual((result['code'], result['failure']), (1, 'conversion_failed'))
        self.assertTrue(result['files'][0]['force_document'])
        self.assertTrue(self.source.is_file())
        with patch.dict(os.environ, {'MEDIA_BOT_CAPTURE_DEADLINE': '40'}), \
                patch.object(video.time, 'monotonic', return_value=25):
            self.assertEqual(video._playback_timeout(1500), 10)
            self.assertEqual(video._playback_timeout(15), 10)

    def test_interrupted_encoder_releases_anonymous_output_and_reaps_only_idle_fallback(self):
        # Park the publisher after writing bytes and kill it without finally.
        script = """
import errno, os, sys, time
from pathlib import Path
from media_bot import video
if sys.argv[2] == 'named':
    original = os.open
    def unsupported(path, flags, *args, **kwargs):
        if flags & os.O_TMPFILE == os.O_TMPFILE:
            raise OSError(errno.EOPNOTSUPP, 'fixture')
        return original(path, flags, *args, **kwargs)
    os.open = unsupported
with video._playback_output(Path(sys.argv[1])) as (path, fd):
    os.write(fd, b'partial output')
    print('encoding', flush=True)
    time.sleep(60)
"""
        unrelated = self.root / 'original.encoding.mp4'
        unrelated.write_bytes(b'original')
        output = self.root / 'test.mkv.h265.mp4'
        for mode in ('anonymous', 'named'):
            with self.subTest(mode=mode), subprocess.Popen(
                    [sys.executable, '-c', script, str(output), mode],
                    stdout=subprocess.PIPE, text=True) as child:
                try:
                    self.assertEqual(child.stdout.readline().strip(), 'encoding')
                    video.cleanup_playback_scratch(self.root)
                    if mode == 'named':
                        self.assertEqual(len(list(self.root.glob('.playback-*.encoding.mp4'))), 1)
                finally:
                    child.kill()
                    child.wait()
                video.cleanup_playback_scratch(self.root)
                self.assertFalse(output.exists())
                self.assertFalse(list(self.root.glob('.playback-*.encoding.mp4')))
                self.assertEqual(unrelated.read_bytes(), b'original')

    def test_named_staging_fallback_can_publish_and_replace_preview(self):
        original_open = os.open
        def no_anonymous_file(path, flags, *args, **kwargs):
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                raise OSError(errno.EOPNOTSUPP, 'fixture')
            return original_open(path, flags, *args, **kwargs)
        with patch.object(video.os, 'open', side_effect=no_anonymous_file):
            preview = video.telegram_playback(self.source)
            self.assertEqual(video.telegram_playback(self.source), preview)
        self.assertTrue(video.attributes(preview))
        self.assertFalse(list(self.root.glob('.playback-*.encoding.mp4')))

    def test_hdr_remains_ten_bit_with_pq_color_tags(self):
        hdr = self.root / 'hdr.mkv'
        subprocess.run([
            'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=blue:s=64x96:r=12:d=0.5', '-vf',
            'format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc',
            '-c:v', 'ffv1', str(hdr),
        ], check=True, capture_output=True)
        output = video.telegram_playback(hdr)
        data = json.loads(subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(output),
        ]))['streams'][0]
        self.assertEqual((data['pix_fmt'], data['color_transfer'], data['color_primaries'],
                          data['color_space']), ('yuv420p10le', 'smpte2084', 'bt2020', 'bt2020nc'))

    def test_webm_and_incompatible_mp4_become_separate_playback_copies(self):
        for name, codec in [('sample.webm', 'libvpx-vp9'), ('sample.mp4', 'mpeg4')]:
            with self.subTest(name=name):
                source = self.root / name
                subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.source), '-an',
                                '-c:v', codec, str(source)], check=True, capture_output=True)
                original = source.read_bytes()
                result = {'code': 0, 'files': [
                    {'path': str(source), 'name': source.name, 'size': len(original)}]}
                video.prepare_playback_files(result)
                self.assertEqual(result['code'], 0)
                preview, document = result['files']
                self.assertEqual(preview['name'], source.name + '.h265.mp4')
                self.assertTrue(document['force_document'])
                self.assertEqual(Path(document['path']).read_bytes(), original)
                self.assertTrue(video.attributes(preview['path']))

    def test_accepted_mp4_is_sent_once_byte_for_byte(self):
        for codec in ('libx264', 'libx265'):
            with self.subTest(codec=codec):
                source = self.root / (codec + '.mp4')
                options = ['-x265-params', 'pools=1:log-level=error'] if codec == 'libx265' else []
                subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.source),
                                '-c:a', 'aac', '-c:v', codec, *options, str(source)],
                               check=True, capture_output=True)
                before = source.read_bytes()
                item = {'path': str(source), 'name': source.name, 'size': len(before)}
                result = {'code': 0, 'files': [item]}
                video.prepare_playback_files(result)
                self.assertEqual(result, {'code': 0, 'files': [item]})
                self.assertEqual(source.read_bytes(), before)

    def test_youtube_registers_preview_and_original_and_retains_original_on_failure(self):
        with patch.dict(os.environ, {'MEDIA_BOT_DATA': str(self.root)}):
            from media_bot import yt_runner
        source = self.root / 'Abc123_X-yZ_00_.mkv'
        shutil.copyfile(self.source, source)
        def extract(*args, **kwargs):
            return {'id': 'Abc123_X-yZ', 'requested_downloads': [{'filepath': str(source)}]}
        payload = dict(platform='youtube', root=str(self.root), url='https://youtu.be/Abc123_X-yZ')
        with patch.object(yt_runner.YoutubeDL, 'extract_info', extract):
            result = yt_runner.run(payload)
        self.assertEqual(result['code'], 0)
        self.assertEqual([item['name'] for item in result['files']], [source.name+'.h265.mp4', source.name])
        self.assertTrue(result['files'][0]['supports_streaming'])
        self.assertTrue(result['files'][1]['force_document'])
        with patch.object(yt_runner.YoutubeDL, 'extract_info', extract), \
                patch.object(video, 'telegram_playback', side_effect=ValueError('synthetic private error')):
            result = yt_runner.run(payload)
        self.assertEqual((result['code'], result['failure']), (1, 'conversion_failed'))
        self.assertEqual([f['name'] for f in result['files']], [source.name])
        self.assertTrue(result['files'][0]['force_document'])
        self.assertTrue(source.is_file())
        self.assertNotIn('synthetic private error', (self.root / 'result.json').read_text())
