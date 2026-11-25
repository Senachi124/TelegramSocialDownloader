import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from takaneko_telegram import secrets, state, takaneko_feed as feed, transport


class FanclubFeatureTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(state, 'CONTROL', root))
        self.enterContext(patch.object(secrets, 'ROOT', root))
        self.enterContext(patch.object(secrets, 'FILE', root / 'credentials.json'))
        self.enterContext(patch.object(transport, 'secret', side_effect=self.secret))
        state.initialize()

    @staticmethod
    def secret(op, fields=None):
        return secrets.request(10, {'op': op, **(fields or {})}, None)

    @staticmethod
    def control(op, **fields):
        return transport.control(11, {'op': op, **fields}, None)

    def test_fresh_install_requires_session_and_explicit_enable(self):
        self.assertEqual(self.control('feature_status'), dict(ok=True, enabled=False, session_configured=False))
        self.assertEqual(self.control('set_enabled', enabled=True), dict(ok=False, error='session_required'))
        for invalid in (None, 1, 0, 'true', 'false'):
            self.assertEqual(self.control('set_enabled', enabled=invalid), dict(ok=False))
        self.assertEqual(self.control('set_session', session={'token': 'fixture'}), dict(ok=True))
        self.assertEqual(self.control('feature_status'), dict(ok=True, enabled=False, session_configured=True))
        self.assertEqual(self.control('set_enabled', enabled=True), dict(ok=True, enabled=True, session_configured=True))
        state.initialize()
        self.assertTrue(feed.is_enabled())
        self.assertEqual(self.control('set_enabled', enabled=False), dict(ok=True, enabled=False, session_configured=True))
        state.initialize()
        self.assertFalse(feed.is_enabled())
        self.assertEqual(json.loads(secrets.FILE.read_text())['session'], {'token': 'fixture'})

    def test_unknown_session_status_allows_pause_and_blocks_new_activation(self):
        feed.set_enabled(True)
        with patch.object(transport, 'secret', side_effect=ConnectionRefusedError):
            self.assertEqual(self.control('feature_status'), dict(ok=True, enabled=True, session_configured=None))
            self.assertEqual(self.control('set_enabled', enabled=False), dict(ok=True, enabled=False, session_configured=None))
            with self.assertRaises(RuntimeError):
                self.control('set_enabled', enabled=True)
        self.assertFalse(feed.is_enabled())

    def test_poll_and_content_errors_keep_the_fanclub_http_status(self):
        from takaneko_telegram import takaneko_api as api

        feed.set_enabled(True)
        response = Mock(status=429)
        response.read.side_effect = AssertionError('Error bodies must not be read')
        with patch.object(api, '_public_connection') as connect:
            connect.return_value.getresponse.return_value = response
            with self.assertRaises(api.FanclubHTTPError) as failure:
                api.Client({})._request('/private-fixture')
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch.object(feed, 'poll_once', side_effect=failure.exception), self.assertLogs(feed._LOG):
            feed.poll_loop(stop)
        self.assertEqual(state.get_setting('takaneko_poll_error'), 'Fanclub HTTP 429')

        feed.set_subscription(42, True)
        feed.record_inventory([])
        feed.record_inventory([{'id': 'fixture'}])
        with patch.object(feed, 'prepare_post', side_effect=api.FanclubHTTPError(503)):
            feed.process_once()
        with state.db() as db:
            self.assertEqual(db.execute('SELECT error FROM tk_posts').fetchone()[0], 'Fanclub HTTP 503')
            self.assertEqual(db.execute('SELECT state FROM tk_deliveries').fetchone()[0], 'pending')
            db.execute('UPDATE tk_posts SET retry_at=0')
        with patch.object(feed, 'prepare_post', side_effect=api.MediaHTTPError(403)):
            feed.process_once()
        with state.db() as db:
            self.assertEqual(db.execute('SELECT error FROM tk_posts').fetchone()[0], 'Media HTTP 403')

    def test_idle_process_never_contacts_upstream_or_reports_failure(self):
        with patch.object(feed, 'process_once') as process, patch.object(transport, 'report_health') as report:
            # A fresh install remains idle. An old enabled install with no session does too.
            for previous in (False, True):
                feed.set_enabled(previous)
                stop = Mock()
                stop.is_set.side_effect = [False, True]
                with patch('takaneko_telegram.takaneko_api.Client') as client:
                    feed.poll_loop(stop)
                    client.assert_not_called()
                self.assertEqual(feed.process_cycle(0), 0)
                process.assert_not_called()
                report.assert_not_called()

    def test_pause_during_delivery_preserves_receipts_and_subscription(self):
        feed.set_enabled(True)
        feed.set_subscription(42, True)
        feed.record_inventory([])
        feed.record_inventory([{'id': 'fixture'}])
        # Two pre-existing frozen parts with independent receipts.
        with state.db() as db:
            payload = json.dumps({'kind': 'text', 'html': 'fixture'})
            db.execute('UPDATE tk_posts SET plan=?', (json.dumps([json.loads(payload)]),))
            delivery = dict(db.execute('SELECT * FROM tk_deliveries').fetchone())
            for position in (0, 1):
                db.execute("INSERT INTO tk_parts VALUES (?,?,?,'pending',NULL)", (delivery['id'], position, payload))

        def first_send(*args):
            feed.set_enabled(False)
            return {'message_id': 123}

        first = Mock(side_effect=first_send)
        feed.deliver(delivery, first)
        first.assert_called_once()
        self.assertTrue(feed.subscribed(42))
        feed.record_inventory([{'id': 'arrived-while-pausing'}])
        with state.db() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM tk_posts WHERE id='arrived-while-pausing'").fetchone())
            self.assertEqual([tuple(row) for row in db.execute('SELECT state,telegram_id FROM tk_parts ORDER BY position')],
                             [('sent', '123'), ('pending', None)])
        feed.set_enabled(True)
        remaining = Mock(return_value={'message_id': 124})
        feed.deliver(delivery, remaining)
        remaining.assert_called_once()
        with state.db() as db:
            self.assertEqual(db.execute('SELECT state FROM tk_deliveries').fetchone()[0], 'sent')

    def test_pause_after_photo_rejection_completes_claim_and_holds_next_part(self):
        feed.set_enabled(True)
        feed.set_subscription(42, True)
        feed.record_inventory([])
        feed.record_inventory([{'id': 'article'}])
        media_root = state.CONTROL
        self.enterContext(patch.object(state, 'MEDIA', media_root))
        self.enterContext(patch.object(transport, 'file_limit', return_value=1024))
        self.enterContext(patch.object(transport, 'allowed', return_value=True))
        plan = []
        for name in ('first.jpg', 'second.jpg'):
            path = media_root / name
            path.write_bytes(b'fixture')
            plan.append(dict(kind='photo', path=str(path), name=name, size=7))
        with state.db() as db:
            db.execute('UPDATE tk_posts SET plan=?', (json.dumps(plan),))

        def send(method, data, files):
            if method == 'sendPhoto' and files['photo'][0] == 'first.jpg':
                files['photo'][1].read()
                feed.set_enabled(False)
                raise transport.TelegramError(400)
            attachment, = files.values()
            self.assertEqual(attachment[1].read(), b'fixture')
            return {'message_id': 123 if method == 'sendDocument' else 124}

        with patch.object(transport, 'telegram', side_effect=send) as outgoing:
            feed.process_once()
            self.assertEqual([call.args[0] for call in outgoing.call_args_list], ['sendPhoto', 'sendDocument'])
            with state.db() as db:
                self.assertEqual([tuple(r) for r in db.execute('SELECT state,telegram_id FROM tk_parts ORDER BY position')],
                                 [('sent', '123'), ('pending', None)])
                self.assertEqual(db.execute('SELECT state FROM tk_deliveries').fetchone()[0], 'ready')
            feed.set_enabled(True)
            feed.process_once()
            self.assertEqual([call.args[0] for call in outgoing.call_args_list], ['sendPhoto', 'sendDocument', 'sendPhoto'])
        with state.db() as db:
            self.assertEqual(db.execute('SELECT state FROM tk_deliveries').fetchone()[0], 'sent')
