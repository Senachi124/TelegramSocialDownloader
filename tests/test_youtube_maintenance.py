import io
import json
import os
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

with tempfile.TemporaryDirectory() as bootstrap, patch.dict(os.environ, {'MEDIA_BOT_DATA': bootstrap}):
    from media_bot import cookie_maintenance, core, features, youtube_cookies, youtube_maintenance as maintenance


COOKIE = '.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsynthetic-session'
TRUE_CONFIG = b'<html><head><script nonce="test">ytcfg.set({"lang":"en","LOGGED_IN":true});</script></head>'


class Response(io.BytesIO):
    def __init__(self, body=TRUE_CONFIG, status=200, **headers):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        self.headers['Content-Type'] = 'text/html; charset=utf-8'
        for key, value in headers.items():
            self.headers[key] = value

    def getheader(self, key, default=None):
        return self.headers.get(key, default)


class SessionMaintenance(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(core, 'DATA', Path(root)))
        core.initialize()
        self.cookies = youtube_cookies.parse(COOKIE)
        self.https = self.enterContext(patch.object(maintenance.http.client, 'HTTPSConnection'))
        self.connection = self.https.return_value
        self.enterContext(patch.object(maintenance, 'frozen', return_value=False))

    def probe(self, response):
        self.connection.getresponse.return_value = response
        return maintenance.check_session(self.cookies)

    def test_fixed_origin_scoped_probe_uses_validated_response_only_after_verified_login(self):
        self.cookies = youtube_cookies.parse(COOKIE + '\nwww.youtube.com\tFALSE\t/private\tTRUE\t0\tNARROW\tprivate')
        response = Response()
        self.assertEqual(self.probe(response)[0], 'valid')
        self.https.assert_called_once_with('www.youtube.com', timeout=maintenance.TIMEOUT)
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ('GET', '/'))
        self.assertEqual(kwargs['headers']['Cookie'], 'SID=synthetic-session')
        self.assertNotIn('private', str(self.connection.request.call_args))
        self.connection.close.assert_called_once()
        with patch.object(youtube_cookies, 'apply_response', side_effect=ValueError('secret')):
            self.assertEqual(self.probe(Response()), ('invalid_cookies', None))

    def test_named_youtube_bootstrap_script_in_large_head(self):
        head = b'<head><script nonce="fixture">(function ytBootstrapConfig() {window.ytplayer={};\n'
        head += b'ytcfg.set({"long":"' + b'x' * (1024 * 1024) + b'","LOGGED_IN":true});})();</script></head>'
        self.assertEqual(self.probe(Response(head))[0], 'valid')
        anonymous = head.replace(b'function ytBootstrapConfig()', b'function()')
        self.assertEqual(self.probe(Response(anonymous))[0], 'valid')
        uninvoked = anonymous.replace(b'})();</script>', b'});</script>')
        self.assertEqual(self.probe(Response(uninvoked)), ('unverified', None))
        dimensions = b"ytcfg.set('WIDTH',window.innerWidth);ytcfg.set('HEIGHT',window.innerHeight);\n"
        footer = b'//# sourceURL=yt-desktop-config.js'
        complete = head.replace(b'})();</script>', b'})();' + dimensions + footer + b'</script>')
        self.assertEqual(self.probe(Response(complete))[0], 'valid')

    def test_inert_nested_nonboolean_and_missing_signals_never_apply_cookies(self):
        fixtures = [
            b'<head><script>ytcfg.set({"LOGGED_IN":false});</script>', b'<head><script>ytcfg.set({"LOGGED_IN":1});</script>',
            b'<head><script>ytcfg.set({"value":{"LOGGED_IN":true}});</script>',
            b'<head><script>"ytcfg.set({\\"LOGGED_IN\\":true});";</script>',
            b'<head><script type="application/json">ytcfg.set({"LOGGED_IN":true});</script>',
            b'<head><script nomodule>ytcfg.set({"LOGGED_IN":true});</script>',
            b'<head><div><script>ytcfg.set({"LOGGED_IN":true});</script></div>',
            b'<head><script src="/lib">ytcfg.set({"LOGGED_IN":true});</script>',
            b'<head><template><script>ytcfg.set({"LOGGED_IN":true});</script></template>',
            b'<head><noscript><script>ytcfg.set({"LOGGED_IN":true});</script></noscript>',
            b'<head></head><body><script>ytcfg.set({"LOGGED_IN":true});</script>',
            b'<head><script>ytcfg.set({"LOGGED_IN":"true"});</script>',
            b'<head><script>ytcfg.set({"LOGGED_IN":true}</script>',
            b'<head><!-- <script>ytcfg.set({"LOGGED_IN":true});</script> -->',
            b'<head><script>ytcfg.set({"LOGGED_IN":true});' + b'x' * maintenance.MAX_HEAD + b'</script>',
        ]
        with patch.object(youtube_cookies, 'apply_response') as apply:
            for n, html in enumerate(fixtures):
                with self.subTest(fixture=n):
                    status = 'login_required' if n == 0 else 'unverified'
                    self.assertEqual(self.probe(Response(html)), (status, None))
            self.assertEqual(self.probe(Response(status=302, Location='https://accounts.google.com/')), ('unverified', None))
            self.assertEqual(self.probe(Response(status=403)), ('unverified', None))
            self.assertEqual(self.probe(Response(status=401)), ('login_required', None))
            apply.assert_not_called()
        self.assertEqual(self.connection.request.call_count, len(fixtures) + 3)

    def test_missing_applicable_auth_or_disabled_never_sends(self):
        for text in (COOKIE.replace('.youtube.com\tTRUE', 'music.youtube.com\tFALSE'), COOKIE.replace('\t/\t', '\t/private\t')):
            self.assertEqual(maintenance.check_session(youtube_cookies.parse(text)), ('invalid_cookies', None))
        features.set_enabled('download_youtube', False)
        self.assertEqual(maintenance.check_session(self.cookies), ('deferred', None))
        self.https.assert_not_called()

    def test_rate_limit_preserves_later_shared_deadline_and_prevents_next_request(self):
        before = time.time()
        self.assertEqual(self.probe(Response(status=429, **{'Retry-After': '1300'})), ('rate_limited', None))
        self.assertGreaterEqual(core.get_setting('youtube_cooldown'), before + 1300)
        self.assertEqual(maintenance.check_session(self.cookies), ('deferred', None))
        self.connection.request.assert_called_once()
        later = before + 4000
        core.setting('youtube_cooldown', later)
        self.assertEqual(maintenance.extend_cooldown('900'), later)
        self.assertEqual(core.get_setting('youtube_cooldown'), later)
        core.setting('youtube_cooldown', 0)
        self.assertEqual(maintenance.extend_cooldown('Fri, 15 Jan 2027 08:00:00 GMT'), core.get_setting('youtube_cooldown'))
        self.assertGreaterEqual(core.get_setting('youtube_cooldown'), 1800000000)

    def test_scheduled_refresh_binds_verified_result_to_snapshot_and_skips_other_accounts(self):
        def item(key, enabled=True, configured=True):
            return {'id': key, 'enabled': enabled, 'cookie_set': configured}
        items = [item('off', False), item('empty', configured=False), item('deleted'), item('stale'), item('valid')]
        payloads = []
        def rpc(sock, path, data=None):
            if path == '/accounts':
                return {'youtube': {'items': items}}
            if path == '/youtube-refresh/snapshot':
                return {} if data['id'] == 'deleted' else {**data, 'revision': 23, 'cookies': self.cookies}
            self.assertEqual(path, '/youtube-refresh/result')
            payloads.append(data)
            return {'status': 'superseded'}
        fresh = {'synthetic': True}
        with patch.object(core, 'rpc', side_effect=rpc), patch.object(
                maintenance, 'check_session', side_effect=[('login_required', None), ('valid', fresh)]):
            self.assertEqual(maintenance.run(), {'superseded': 2, 'login_required': 1})
        self.assertEqual(payloads, [{'id': 'valid', 'revision': 23, 'cookies': fresh}])

    def test_combined_command_runs_platforms_independently_and_redacts_failures(self):
        with patch.object(cookie_maintenance.instagram_maintenance, 'run', side_effect=RuntimeError('SECRET')), patch.object(
                maintenance, 'run', return_value={'unchanged': 1}) as yt, redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                cookie_maintenance.main()
        self.assertEqual(json.loads(out.getvalue()), {'instagram': {'maintenance_error': 1}, 'youtube': {'unchanged': 1}})
        yt.assert_called_once_with()
        core.setting('enabled', False)
        with patch.object(cookie_maintenance.instagram_maintenance, 'run', return_value={'unchanged': 1}) as ig, patch.object(
                core, 'rpc') as rpc, redirect_stdout(io.StringIO()) as out:
            cookie_maintenance.main()
        ig.assert_called_once_with()
        rpc.assert_not_called()
        self.assertEqual(json.loads(out.getvalue()), {'instagram': {'unchanged': 1}, 'youtube': {'deferred': 1}})
