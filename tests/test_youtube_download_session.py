"""Account-bound download rotation with offline YouTube and broker fixtures."""
from contextlib import ExitStack
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import requests
from urllib3._collections import HTTPHeaderDict
from urllib3.response import HTTPResponse
from yt_dlp.networking._requests import RequestsResponseAdapter

with tempfile.TemporaryDirectory() as bootstrap, patch.dict(os.environ, {'MEDIA_BOT_DATA': bootstrap}):
    from media_bot import core, youtube_cookies, youtube_maintenance, yt_runner


class DownloadSessionTests(unittest.TestCase):
    url = 'https://www.youtube.com/watch?v=Abc123_X-yZ'

    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.cookies = youtube_cookies.parse('.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSAPISID\tinitial-fixture')
        self.final = self.root / 'Abc123_X-yZ_00_.mp4'
        self.final.write_bytes(b'synthetic download')
        self.rpc = stack.enter_context(patch.object(core, 'rpc', side_effect=self.broker))
        self.verify = stack.enter_context(patch.object(youtube_maintenance, 'check_session',
                                                      side_effect=lambda candidate: ('valid', candidate)))
        stack.enter_context(patch.object(youtube_maintenance, 'available', return_value=True))
        self.snapshot_revision = 4
        self.saved = None

    def broker(self, socket, path, payload):
        if path == '/youtube-refresh/snapshot':
            self.assertEqual(payload, {'id': 'yt-fixture'})
            return {'id': 'yt-fixture', 'revision': self.snapshot_revision, 'cookies': self.cookies}
        self.assertEqual(path, '/youtube-refresh/result')
        self.assertEqual((payload['id'], payload['revision']), ('yt-fixture', 4))
        self.saved = payload['cookies']
        return {'status': 'updated', 'revision': 5}

    def capture(self, *, source=None, requested=None, success=True, bound=True):
        fields = HTTPHeaderDict()
        fields.add('Set-Cookie', 'SAPISID=rotated-fixture; Domain=.youtube.com; Path=/; Secure; Expires=Tue, 01 Jan 2036 00:00:00 GMT')
        fields.add('Set-Cookie', 'SIDCC=companion-fixture; Domain=.youtube.com; Path=/; Secure; HttpOnly')
        wire = requests.Response()
        wire.url, wire.status_code = source or self.url, 200
        wire.raw = HTTPResponse(headers=fields, preload_content=False)
        wire.headers = requests.structures.CaseInsensitiveDict(fields)
        response = RequestsResponseAdapter(wire)
        self.addCleanup(response.close)

        def extract(downloader, url, **kwargs):
            downloader.urlopen(requested or url)
            if success:
                return {'id': 'Abc123_X-yZ', 'requested_downloads': [{'filepath': str(self.final)}]}
            return None

        payload = {'platform': 'youtube', 'url': self.url, 'root': str(self.root),
                   'cookies': self.cookies if bound else {}}
        if bound:
            payload['youtube_account'] = {'id': 'yt-fixture', 'revision': 4}
        with (patch.object(yt_runner.YoutubeDL, 'extract_info', extract),
              patch.object(yt_runner.YoutubeDL, 'urlopen', return_value=response)):
            result = yt_runner.run(payload)
        saved_result = (self.root / 'result.json').read_text()
        self.assertEqual(result, json.loads(saved_result))
        for value in ('initial-fixture', 'rotated-fixture', 'companion-fixture'):
            self.assertNotIn(value, saved_result)
        return result

    def test_verified_download_merges_separate_server_cookies_and_returns_only_revision(self):
        result = self.capture()
        self.assertEqual(result['code'], 0)
        self.assertEqual(result['youtube_updated_revision'], 5)
        self.verify.assert_called_once()
        self.assertEqual({c['name']: c['value'] for c in self.saved['items']},
                         {'SAPISID': 'rotated-fixture', 'SIDCC': 'companion-fixture'})

    def test_public_failed_and_foreign_responses_do_not_trigger_refresh(self):
        self.capture(bound=False)
        self.capture(success=False)
        self.capture(source='https://media.example.test/stream')
        self.capture(requested='https://media.example.test/redirect')
        self.rpc.assert_not_called()
        self.verify.assert_not_called()

    def test_superseded_and_signed_out_sessions_keep_current_download(self):
        self.snapshot_revision = 5
        self.assertNotIn('youtube_updated_revision', self.capture())
        self.verify.assert_not_called()
        self.snapshot_revision = 4
        self.verify.side_effect = lambda candidate: ('login_required', None)
        self.assertEqual(self.capture()['code'], 0)
        self.assertIsNone(self.saved)
        self.verify.side_effect = TimeoutError()
        with self.assertLogs('media_bot.youtube_session', level='WARNING') as logs:
            self.assertEqual(self.capture()['code'], 0)
        self.assertNotIn('fixture', str(logs.output))
        self.assertIsNone(self.saved)
