"""Local conversion regressions with generated media and no Telegram calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from media_bot import video


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class MkvConversionTests(unittest.TestCase):
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
        output = video.mkv_to_hevc(self.source)
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
            video.mkv_to_hevc(self.source)
        self.assertEqual(list(self.root.glob('*.mp4')), [])
        self.assertEqual(self.source.read_bytes(), original)


    def test_hdr_remains_ten_bit_with_pq_color_tags(self):
        hdr = self.root / 'hdr.mkv'
        subprocess.run([
            'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=blue:s=64x96:r=12:d=0.5', '-vf',
            'format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc',
            '-c:v', 'ffv1', str(hdr),
        ], check=True, capture_output=True)
        output = video.mkv_to_hevc(hdr)
        data = json.loads(subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(output),
        ]))['streams'][0]
        self.assertEqual((data['pix_fmt'], data['color_transfer'], data['color_primaries'],
                          data['color_space']), ('yuv420p10le', 'smpte2084', 'bt2020', 'bt2020nc'))

    def test_youtube_runner_registers_converted_video_and_suppresses_failed_conversion(self):
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
        self.assertEqual([item['name'] for item in result['files']], ['Abc123_X-yZ_00_.h265.mp4'])
        with patch.object(yt_runner.YoutubeDL, 'extract_info', extract), \
                patch.object(video, 'mkv_to_hevc', side_effect=ValueError('synthetic private error')):
            result = yt_runner.run(payload)
        self.assertEqual((result['code'], result['failure'], result['files']),
                         (1, 'conversion_failed', []))
        self.assertTrue(source.is_file())
        self.assertNotIn('synthetic private error', (self.root / 'result.json').read_text())
