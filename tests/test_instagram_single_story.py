"""Offline Story selection through gallery-dl's actual metadata and file routing."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_runtime import _TEST_ROOT
from gallery_dl import job
from gallery_dl.extractor.instagram import InstagramAPI
from media_bot import core, gallery_runner


def item(pk, taken_at, video=False):
    image = {'width': 100, 'height': 100, 'url': f'https://cdn.example/{pk}.jpg'}
    result = {'pk': pk, 'taken_at': taken_at, 'media_type': 2 if video else 1,
              'image_versions2': {'candidates': [image]}}
    if video:
        result['video_versions'] = [{**image, 'url': f'https://cdn.example/{pk}.mp4'}]
    return result


def reel(owner, *items):
    return {'id': str(owner), 'user': {'pk': owner, 'username': 'fixture.name'},
            'seen': 1, 'items': list(items)}


class StoryCaptureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=_TEST_ROOT)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        mocked_data = patch.object(core, 'DATA', self.root)
        mocked_data.start()
        self.addCleanup(mocked_data.stop)
        core.initialize()

    def run_capture(self, reels, url, scope='single', stale=False):
        folder = self.root / 'media' / 'fixture'
        folder.mkdir(exist_ok=True)
        if stale:
            (folder / 'discarded.jpg').write_bytes(b'previous attempt')
        preferences = {'story_scope': scope} if scope else {}
        payload = {'platform': 'instagram', 'url': url, 'mode': 'stories',
                   'cookies': {'sessionid': 'fixture', 'csrftoken': 'fixture'},
                   'preferences': preferences, 'root': str(folder)}
        transfers = []

        def media(download_job, url, data):
            transfers.append(url)
            pathfmt = download_job.pathfmt
            pathfmt.set_filename(data)
            pathfmt.build_path()
            Path(pathfmt.realpath).write_bytes(b'fixture')

        with patch('sys.stdin', io.StringIO(json.dumps(payload))), \
             patch.object(InstagramAPI, 'user', return_value={'id': '42'}), \
             patch.object(InstagramAPI, 'reels_media', return_value=iter(reels)), \
             patch.object(job.DownloadJob, 'handle_url', media), \
             patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected HTTP')):
            gallery_runner.main()
        result = json.loads((folder / 'result.json').read_text())
        return result, transfers

    def test_exact_large_integer_id_selects_video_in_later_page_and_ignores_stale_file(self):
        wanted = 4030000000000000001
        reels = [reel(42, item('222', 90)), reel(42, item(wanted, 80, True))]
        url = f'https://www.instagram.com/stories/fixture.name/{wanted}?utm_source=ig_story_item_share'
        result, transfers = self.run_capture(reels, url, stale=True)
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(transfers, [f'https://cdn.example/{wanted}.mp4'])
        self.assertEqual([f['name'] for f in result['files']], [f'{wanted}.mp4'])

    def test_missing_story_id_never_uses_other_authors_or_stale_files(self):
        reels = [reel(12, item('999', 50)), reel(42, item('222', 90))]
        result, transfers = self.run_capture(reels, 'https://www.instagram.com/stories/fixture.name/999/', stale=True)
        self.assertEqual((result['failure'], result['files'], transfers), ('story_unavailable', [], []))
        self.assertNotEqual(result['code'], 0)

    def test_exact_image(self):
        result, transfers = self.run_capture([reel(42, item('88', 90), item('89', 91))],
            'https://www.instagram.com/stories/fixture.name/88/')
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(transfers, ['https://cdn.example/88.jpg'])

    def test_video_without_video_rendition_never_sends_cover(self):
        video = item('88', 90, True)
        video.pop('video_versions')
        result, transfers = self.run_capture([reel(42, video)], 'https://instagram.com/stories/fixture.name/88/')
        self.assertEqual((result['failure'], transfers), ('story_unavailable', []))

    def test_no_id_selects_latest_timestamp_with_id_tiebreak_across_reels(self):
        reels = [reel(42, item('901', 10), item('203', 15)), reel(42, item('205', 15))]
        result, transfers = self.run_capture(reels, 'https://instagram.com/fixture.name/')
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(transfers, ['https://cdn.example/205.jpg'])

    def test_all_setting_and_legacy_queue_still_fetch_entire_reel(self):
        url = 'https://www.instagram.com/stories/fixture.name/123/'
        for scope in ('all', None):
            with self.subTest(scope=scope):
                result, transfers = self.run_capture([reel(42, item('123', 1), item('456', 2))], url, scope)
                self.assertEqual(result['code'], 0, result)
                self.assertEqual(transfers, ['https://cdn.example/123.jpg', 'https://cdn.example/456.jpg'])
