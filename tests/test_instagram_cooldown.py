import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from media_bot.instagram.policy import RequestWindow, WindowBlocked
from media_bot.instagram.web_client import WebClient, WebStop


class InstagramCooldownTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        with patch.dict(os.environ, {'MEDIA_BOT_DATA': temp.name}):
            from media_bot import core
            from media_bot.instagram import limits
        self.core, self.limits = core, limits
        data = patch.object(core, 'DATA', self.root)
        data.start()
        self.addCleanup(data.stop)
        with core.db() as db:
            db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)')

    def test_global_provenance_follows_longest_deadline_and_legacy_overwrite(self):
        self.assertEqual(self.limits.active_cooldown(), (0, 'unknown'))
        self.core.setting('instagram_cooldown', 1000)
        self.assertEqual(self.limits.active_cooldown(), (1000, 'unknown'))
        self.limits.extend_cooldown(1500, 'http_429')
        self.limits.extend_cooldown(1200, 'http_500')
        self.assertEqual(self.limits.active_cooldown(), (1500, 'http_429'))
        self.limits.extend_cooldown(1800, 'upstream_restriction')
        self.assertEqual(self.limits.active_cooldown(), (1800, 'upstream_restriction'))
        self.core.setting('instagram_cooldown', 2000)
        self.assertEqual(self.limits.active_cooldown(), (2000, 'unknown'))

    def test_pacing_database_carries_maintenance_reason_across_processes(self):
        path = self.root / 'rate.sqlite3'
        first = RequestWindow(path, clock=lambda: 100)
        first.block(1800, 'upstream_restriction')
        second = RequestWindow(path, clock=lambda: 200)
        second.block(900, 'http_429')
        with self.assertRaises(WindowBlocked) as caught:
            second.reserve()
        self.assertEqual((caught.exception.until, caught.exception.reason), (1900, 'upstream_restriction'))
        with second.connect() as db:
            db.execute('UPDATE state SET blocked_until=2000')
        with self.assertRaises(WindowBlocked) as caught:
            first.reserve()
        self.assertEqual((caught.exception.until, caught.exception.reason), (2000, 'unknown'))

    def test_preexisting_maintenance_block_marks_capture_before_api_request(self):
        import requests
        from gallery_dl import exception
        from gallery_dl.extractor.instagram import InstagramAPI
        from media_bot.instagram.web_gallery import install_web_graphql

        until, reason = RequestWindow(self.root / 'instagram-api-rate.sqlite3').block(1800, 'http_429')
        result = {}
        self.addCleanup(install_web_graphql(result))
        extractor = Mock()
        extractor.config.return_value = 'memory'
        extractor.exc = exception
        extractor.cookies = requests.cookies.cookiejar_from_dict({'sessionid': 'fixture', 'csrftoken': 'fixture'})
        api = InstagramAPI(extractor)
        with patch.object(requests.Session, 'request') as request, self.assertRaises(exception.AbortExtraction):
            list(api.media('fixture'))
        request.assert_not_called()
        self.assertTrue(result['instagram_cooldown_active'])
        self.assertEqual(self.limits.result_reason(result), reason)
        self.assertEqual(self.limits.active_cooldown(), (until, reason))

    def test_gallery_http_429_reaches_result_and_persistent_gate(self):
        import requests
        from gallery_dl import job
        from media_bot import gallery_runner

        payload = {'platform': 'instagram', 'mode': 'post', 'root': str(self.root),
                   'url': 'https://www.instagram.com/p/fixture/', 'cookies': {}}
        response = Mock(status_code=429, headers={'Retry-After': '1800'})
        def download(_job):
            with requests.Session() as session:
                session.trust_env = False
                session.get('https://www.instagram.com/')
        with patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                patch.object(requests.adapters.HTTPAdapter, 'send', return_value=response), \
                patch.object(job.DownloadJob, 'run', download):
            gallery_runner.main()
        result = json.loads((self.root / 'result.json').read_text())
        self.assertEqual(self.limits.result_reason(result), 'http_429')
        self.assertGreaterEqual(result['retry_at'], result['started'] + 1800)
        self.assertEqual(self.limits.active_cooldown(), (result['retry_at'], 'http_429'))
        response.close.assert_called_once()

    def test_dash_transfer_preserves_observed_429(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.networking.exceptions import HTTPError
        from media_bot import download_size
        from media_bot.instagram import quality

        result = {'requests': 0}
        response = Mock(status=429, reason='Too Many Requests')
        with patch.object(YoutubeDL, 'urlopen', side_effect=HTTPError(response)):
            restore = quality.install(result)
            try:
                with self.assertRaises(download_size.DownloadStopped):
                    YoutubeDL.urlopen(None, 'https://fixture.cdninstagram.com/video')
            finally:
                restore()
        self.assertEqual(self.limits.result_reason(result), 'http_429')
        self.assertEqual(self.limits.active_cooldown(), (result['retry_at'], 'http_429'))
        response.close.assert_called_once()

    def test_redirect_429_identifies_limiting_platform(self):
        first, limited = Mock(status=302), Mock(status=429)
        first.getheader.return_value = 'https://www.instagram.com/share/example/link/'
        limited.getheader.return_value = '1500'
        with patch.object(self.core.socket, 'getaddrinfo', return_value=[(0, 0, 0, '', ('8.8.8.8', 443))]), \
                patch.object(self.core.http.client, 'HTTPSConnection') as connect:
            connect.return_value.getresponse.side_effect = [first, limited]
            with self.assertRaises(self.core.RateLimited) as caught:
                self.core.resolve('https://vm.tiktok.com/example/')
        self.assertEqual((caught.exception.platform, caught.exception.reason), ('instagram', 'http_429'))
        self.assertEqual(self.limits.active_cooldown(), (caught.exception.until, 'http_429'))
        self.assertIsNone(self.core.get_setting('tiktok_cooldown'))

    def test_graphql_cooldown_callback_receives_only_fixed_observed_reason(self):
        session, block = Mock(), Mock()
        client = WebClient(session, Mock(), block_with_reason=block, max_requests=2)
        for status, expected in ((429, 'http_429'), (503, 'http_503')):
            session.request.return_value = Mock(status_code=status, headers={})
            with self.assertRaises(WebStop):
                client.request(session, 'GET', '')
            block.assert_called_with(900, expected)
        with self.assertRaises(WebStop) as caught:
            client.request(session, 'GET', '')
        self.assertEqual(self.limits.result_reason({'instagram_diagnostic': caught.exception.diagnostic}),
                         'local_request_budget')
        self.assertEqual(session.request.call_count, 2)
        self.assertEqual(block.call_count, 2)

    def test_cookie_maintenance_observed_cause_and_legacy_status(self):
        from media_bot.instagram import cookie_refresh, maintenance

        cookies = maintenance.cookie_jar({'sessionid': '123%3Afixture', 'csrftoken': 'fixture'})
        for status, reason in ((429, 'http_429'), (200, 'upstream_restriction')):
            with self.subTest(status=status), patch.object(cookie_refresh.http.client, 'HTTPSConnection') as connect:
                response = connect.return_value.getresponse.return_value.__enter__.return_value
                response.status = status
                response.read.return_value = b'{"message":"feedback_required"}'
                self.assertEqual(cookie_refresh.check(cookies, '123', detailed_limit=True), (reason, None))
                self.assertEqual(cookie_refresh.check(cookies, '123'), ('rate_limited', None))
