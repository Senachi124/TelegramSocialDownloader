import fcntl
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from media_bot import push_socket
from takaneko_telegram import secrets, session_probe, state, takaneko_api as api, takaneko_feed as feed, transport


class SessionProbeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(state, 'CONTROL', self.root))
        self.enterContext(patch.object(secrets, 'ROOT', self.root))
        self.enterContext(patch.object(secrets, 'FILE', self.root / 'credentials.json'))
        self.broker = self.enterContext(patch.object(transport, 'secret', side_effect=self.secret))
        state.initialize()

    def secret(self, op, fields=None):
        result = secrets.request(11, {'op': op, **(fields or {})}, None)
        if result['ok'] is not True:
            raise RuntimeError('Broker unavailable')
        return result

    def save(self, session):
        secrets.save({'session': session, 'openai_keys': [{'id': 'fixture', 'key': 'private', 'enabled': True}]})

    def test_paused_saved_login_uses_only_count_through_command_and_socket(self):
        self.save({'accessToken': 'fixture'})
        self.assertFalse(feed.is_enabled())
        runner = self.enterContext(patch.object(feed.subprocess, 'run'))
        def run(*args, **kwargs):
            outcome = session_probe.check()
            return Mock(returncode=next(code for code, name in session_probe.RESULTS.items() if name == outcome))
        runner.side_effect = run
        with patch.object(api.Client, '_request', return_value={'count': 3}) as call, patch.object(
                feed, 'record_inventory', side_effect=AssertionError):
            reply = transport.control(11, {'op': 'test_session'}, None)
        self.assertEqual(reply, {'ok': True, 'result': 'connected'})
        call.assert_called_once_with('/auth/notifications/count', token='fixture')
        self.assertEqual(runner.call_args.args[0], [sys.executable, '-IB', '-m', 'takaneko_telegram.session_probe'])
        self.assertEqual(runner.call_args.kwargs['timeout'], 9)
        self.assertIs(runner.call_args.kwargs['stdout'], subprocess.DEVNULL)
        self.assertIs(runner.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertFalse(feed.is_enabled())
        self.assertNotIn('openai_keys', secrets.request(11, {'op': 'get_session'}, None))
        self.assertIn('test_session', transport.control(11, {'op': 'list'}, None)['capabilities'])
        self.assertEqual(transport.control(11, {'op': 'test_session', 'session': {'token': 'unused'}}, None),
                         {'ok': False})
        self.assertEqual(transport.control(11, {'op': 'test_session'}, 9), {'ok': False})
        with state.db() as db:
            for table in ('tk_subscriptions', 'tk_deliveries', 'tk_posts'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
            self.assertFalse(db.execute("SELECT 1 FROM settings WHERE key IN ('takaneko_last_poll','takaneko_error')").fetchone())

    def test_rotation_survives_rejected_or_malformed_count(self):
        for error, expected in [(api.FanclubHTTPError(403), 'forbidden'), ({'count': True}, 'invalid'),
                                (api.FanclubConnectionError('fixture'), 'network')]:
            with self.subTest(expected=expected):
                self.save({'refreshToken': 'before'})
                responses = [{'accessToken': 'access', 'refreshToken': 'after'}, error]
                with patch.object(api.Client, '_request', side_effect=responses) as call:
                    self.assertEqual(session_probe.check(), expected)
                self.assertEqual([c.args[0] for c in call.call_args_list], ['/auth/refresh', '/auth/notifications/count'])
                persisted = json.loads(secrets.FILE.read_text())
                self.assertEqual(persisted['session'], {'accessToken': 'access', 'refreshToken': 'after'})
                self.assertEqual(persisted['openai_keys'][0]['key'], 'private')

    def test_newer_import_wins_before_rotation_and_before_result(self):
        for when in ('refresh', 'count'):
            with self.subTest(when=when):
                self.save({'refreshToken': 'before'})

                def request(path, **kwargs):
                    if path.endswith(when):
                        self.save({'token': 'new-import'})
                    return {'accessToken': 'access', 'refreshToken': 'rotated'} if path.endswith('refresh') else {'count': 0}

                with patch.object(api.Client, '_request', side_effect=request) as outbound:
                    self.assertEqual(session_probe.check(), 'changed')
                self.assertEqual(json.loads(secrets.FILE.read_text())['session'], {'token': 'new-import'})
                self.assertEqual(outbound.call_count, 1 if when == 'refresh' else 2)

    def test_working_saved_access_avoids_unnecessary_refresh(self):
        self.save({'refreshToken': 'expired-refresh', 'accessToken': 'fallback'})
        with patch.object(api.Client, '_request', return_value={'count': 0}) as req:
            self.assertEqual(session_probe.check(), 'connected')
        req.assert_called_once_with('/auth/notifications/count', token='fallback')
        self.assertEqual(self.broker.call_count, 2)  # Read and compare; zero credential writes.

    def test_expired_access_refreshes_once_and_checks_new_token(self):
        self.save({'accessToken': 'expired', 'refreshToken': 'before'})
        with patch.object(api.Client, '_request', side_effect=[
                api.FanclubHTTPError(401), {'accessToken': 'after', 'refreshToken': 'rotated'}, {'total': 0}]) as req:
            self.assertEqual(session_probe.check(), 'connected')
        self.assertEqual([c.args[0] for c in req.call_args_list], [
            '/auth/notifications/count', '/auth/refresh', '/auth/notifications/count'])
        self.assertEqual(req.call_args.kwargs, {'token': 'after'})
        with patch.object(api.Client, '_request', side_effect=[api.FanclubHTTPError(401), api.FanclubHTTPError(400)]) as req:
            self.assertEqual(session_probe.check(), 'unauthorized')
            self.assertEqual(req.call_count, 2)

    def test_oversized_rotations_never_replace_readable_credential_file(self):
        session = {'cookies': [{'domain': '.takanekofc.com', 'path': '/', 'name': f'fixture{i}', 'value': 'x' * 12000}
                               for i in range(3)]}
        self.save(session)
        saved = secrets.FILE.read_bytes()
        for key_size, token_size in [(0, 9000), (27000, 3500)]:
            with self.subTest(key_size=key_size):
                credentials = json.loads(saved)
                if key_size:
                    # Older operator-managed files may have a larger key pool.
                    credentials['openai_keys'][0]['key'] = 'k' * key_size
                secrets.save(credentials)
                before = secrets.FILE.read_bytes()
                push_socket._pack(self.secret('get'))  # Original session and keys fit.
                refreshed = {'accessToken': 'a' * token_size, 'refreshToken': 'r' * token_size}
                with patch.object(api.Client, '_request', return_value=refreshed) as req:
                    self.assertEqual(session_probe.check(), 'unavailable')
                req.assert_called_once()
                self.assertEqual(secrets.FILE.read_bytes(), before)
                push_socket._pack(self.secret('get_session'))
                push_socket._pack(self.secret('get'))

    def test_refresh_rejections_and_count_failure_reasons_are_content_free(self):
        with patch.object(api.Client, '_request', side_effect=AssertionError):
            self.assertEqual(session_probe.check(), 'no_session')
        for login, error, expected in [
                ({'refreshToken': 'x'}, api.FanclubHTTPError(400), 'unauthorized'),
                ({'token': 'x'}, api.FanclubHTTPError(400), 'upstream'),
                ({'token': 'x'}, api.FanclubHTTPError(401), 'unauthorized'),
                ({'token': 'x'}, api.FanclubHTTPError(403), 'forbidden'),
                ({'token': 'x'}, api.FanclubHTTPError(429), 'rate_limited'),
                ({'token': 'x'}, api.FanclubHTTPError(503), 'upstream'),
                ({'token': 'x'}, api.FanclubConnectionError('private URL'), 'network'),
                ({'token': 'x'}, {'count': 'private URL'}, 'invalid')]:
            with self.subTest(expected=expected):
                self.save(login)
                with patch.object(api.Client, '_request', side_effect=[error]):
                    self.assertEqual(session_probe.check(), expected)
        self.broker.side_effect = RuntimeError('private')
        self.assertEqual(session_probe.check(), 'unavailable')

    def test_busy_exits_immediately_and_failed_subprocess_releases_auth_lock(self):
        with feed._AUTH_LOCK, patch.object(feed.subprocess, 'run') as run:
            self.assertEqual(feed.test_session(), 'busy')
            run.assert_not_called()
        with patch.object(feed.subprocess, 'run', side_effect=subprocess.TimeoutExpired('test', 9)):
            self.assertEqual(feed.test_session(), 'unavailable')
        with patch.object(feed.subprocess, 'run', return_value=Mock(returncode=1)):
            self.assertEqual(feed.test_session(), 'unavailable')
        with patch.object(feed.subprocess, 'run', return_value=Mock(returncode=21)):
            self.assertEqual(feed.test_session(), 'no_session')

    def test_deployment_guard_blocks_probe_and_covers_entire_subprocess(self):
        path = self.root / 'deploy.lock'
        with path.open('w') as guard, patch.dict(os.environ, {'TAKANEKO_DEPLOY_LOCK': str(path)}):
            fcntl.flock(guard, fcntl.LOCK_EX)
            with patch.object(feed.subprocess, 'run') as run:
                self.assertEqual(feed.test_session(), 'busy')
                run.assert_not_called()
            fcntl.flock(guard, fcntl.LOCK_UN)
            def during_probe(*args, **kwargs):
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return Mock(returncode=20)
            with patch.object(feed.subprocess, 'run', side_effect=during_probe):
                self.assertEqual(feed.test_session(), 'connected')
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_probe_child_reads_broker_with_no_stdin_stdout_or_traceback(self):
        path = str(self.root / 'broker.sock')
        uid = os.getuid()
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
            server.bind(path)
            server.listen(1)
            server.settimeout(5)

            def serve():
                client, _ = server.accept()
                push_socket._serve_one(client, {uid}, secrets.request, threading.Semaphore())

            listener = threading.Thread(target=serve)
            listener.start()
            root = Path(__file__).resolve().parents[1]
            # Exercise the isolated interpreter against the checkout under test.
            script = ('import runpy,sys; sys.path[:0]=sys.argv[1:]; '
                      'runpy.run_module("takaneko_telegram.session_probe",run_name="__main__")')
            env = {**os.environ, 'TAKANEKO_SECRET_SOCKET': path, 'TAKANEKO_BROKER_USER': pwd.getpwuid(uid).pw_name}
            try:
                proc = subprocess.run([sys.executable, '-IB', '-c', script, str(root), str(root / 'services/takaneko')],
                                      env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=9)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (21, b'', b''))
            finally:
                listener.join(6)


if __name__ == '__main__':
    unittest.main()
