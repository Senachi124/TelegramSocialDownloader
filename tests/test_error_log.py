import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests

with tempfile.TemporaryDirectory() as bootstrap, patch.dict(os.environ, {'MEDIA_BOT_DATA': bootstrap}):
    from media_bot import admin_notifications as alerts, core, error_log, push
    from media_bot.processing import telegram_api as api

from takaneko_telegram import state, transport


class ErrorLogTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(core, 'DATA', root))
        core.initialize()

    def test_legacy_health_once_without_timestamp_or_owner_reminder(self):
        with core.db() as db:
            db.execute("DELETE FROM settings WHERE key='admin_errors_imported_health'")
            db.execute("INSERT INTO settings VALUES ('push_health.takaneko','\"uncertain\"')")
            db.execute("INSERT INTO settings VALUES ('push_health.fixture','\"ok\"')")
            db.execute("INSERT INTO settings VALUES ('push_health.invalid','PRIVATE')")
            db.execute("INSERT INTO settings VALUES ('push_health.secret','\"PRIVATE\"')")
            db.execute("INSERT INTO admin_alert_queue(reason,platform) VALUES ('uncertain','push')")
            db.execute("INSERT INTO admin_alert_queue(reason,platform) VALUES ('access','push')")
        alerts.initialize()
        alerts.initialize()
        push.report_health('takaneko', 'uncertain')
        only, = error_log.recent()
        self.assertEqual(only['source'], 'Takaneko → Telegram')
        self.assertIsNone(only['observed'])
        self.assertIn('without a saved delivery receipt', only['message'])
        with core.db() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT reason,platform FROM admin_alert_queue')],
                             [('access', 'push_provider')])

    def test_transitions_logged_separately_from_owner_alert_preference_and_provider(self):
        for slug, status in [('takaneko', 'uncertain'), ('takaneko', 'ok'),
                             ('takaneko', 'fanclub_poll_http_429'), ('takaneko', 'broker_unavailable'),
                             ('other-private-slug', 'uncertain')]:
            self.assertEqual(push.report_health(slug, status), {'ok': True})
        core.setting('failure_alerts', False)
        self.assertEqual(push.report_health('other-private-slug', 'network'), {'ok': True})
        self.assertEqual(push.report_health('other-private-slug', 'network'), {'ok': True})
        self.assertEqual(push.report_health('other-private-slug', 'fanclub_poll_http_403'), {'ok': False, 'code': 400})
        self.assertEqual(push.report_health('takaneko', 'fanclub_poll_http_429/secret'), {'ok': False, 'code': 400})
        with core.db() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT reason,platform FROM admin_alert_queue')],
                             [('uncertain', 'push_provider')])
        rows = error_log.recent()
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(time.time() - 60 < r['observed'] <= time.time() for r in rows))
        self.assertIn('429', rows[-2]['message'])
        self.assertEqual(rows[-2]['source'], 'Takaneko Fanclub')
        self.assertNotIn('private', json.dumps(rows))
        self.assertIn('credential broker', rows[-3]['message'])

    def test_outstanding_legacy_manual_reminder_is_unsendable_before_migration(self):
        with core.db() as db:
            db.execute("INSERT INTO bot_users(id,state,requested,last_seen) VALUES (42,'owner',0,0)")
            db.execute("INSERT INTO settings VALUES ('push_health.takaneko','\"uncertain\"')")
            db.execute("INSERT INTO admin_alert_queue(reason,platform) VALUES ('uncertain','push')")
            db.execute("INSERT INTO admin_alert_queue(reason,platform) VALUES ('network','push_provider')")
        send = Mock(return_value={'message_id': 12})
        alerts.tick(send)
        alerts.tick(send)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1]['text'], 'Push · ' + push.HEALTH['network'])

    def test_legacy_alert_migration_uses_historical_provider_evidence(self):
        for providers, surviving in [
                (['other'], ['uncertain', 'network']),
                (['takaneko'], []),
                (['takaneko', 'other'], ['network'])]:
            with self.subTest(providers=providers), core.db() as db:
                db.execute("DELETE FROM settings WHERE key GLOB 'push_health.*'")
                db.execute('DELETE FROM admin_alert_queue')
                for slug in providers:
                    db.execute("INSERT INTO settings VALUES (?,?)", ('push_health.' + slug, '"ok"'))
                for reason in ('uncertain', 'network'):
                    db.execute("INSERT INTO admin_alert_queue(reason,platform) VALUES (?,'push')", (reason,))
                db.execute("INSERT INTO admin_alert_queue(reason,platform,state) VALUES ('network','push','uncertain')")
                alerts._migrate_pending_push_alerts(db)
                self.assertEqual([r[0] for r in db.execute(
                    "SELECT reason FROM admin_alert_queue WHERE state='pending' ORDER BY id")], surviving)
                self.assertEqual(tuple(db.execute(
                    "SELECT platform,reason FROM admin_alert_queue WHERE state='uncertain'").fetchone()), ('push','network'))

    def test_download_transition_logged_with_alerts_off_and_no_private_data(self):
        core.setting('failure_alerts', False)
        with core.db() as db:
            db.execute("""INSERT INTO jobs(id,chat,platform,url,state,message)
                          VALUES ('PRIVATE-job',424242,'twitter','https://private.test','running','secret')""")
            db.execute("UPDATE jobs SET state='paused',message='Telegram 拒絕傳送，檔案保留待重試'")
            db.execute("UPDATE jobs SET state='failed',message='cookie https://private.test/token'")
            db.execute("UPDATE jobs SET message='https://private.test/other'")
            db.execute("""INSERT INTO jobs(id,platform,state,message)
                          VALUES ('PRIVATE-job2','https://private.test','failed','format secret')""")
            self.assertEqual(db.execute('SELECT COUNT(*) FROM admin_alert_queue').fetchone()[0], 0)
            self.assertNotIn('private', str([tuple(r) for r in db.execute('SELECT * FROM admin_errors')]).lower())
        rows = error_log.recent()
        self.assertEqual([r['source'] for r in rows], ['Bot downloads', 'X downloads', 'Downloads → Telegram'])
        self.assertIn('Telegram rejected', rows[2]['message'])
        self.assertIn('cookies', rows[1]['message'])

    def test_actual_repeated_sends_record_independently_with_safe_specific_reasons(self):
        req = {'op': 'send', 'user_id': 424242, 'kind': 'text', 'text': 'PRIVATE media'}
        access = self.enterContext(patch.object(push.access, 'allowed', return_value=True))
        self.enterContext(patch.object(push, 'control', return_value={'enabled': True}))
        core.setting('failure_alerts', False)
        push.report_health('takaneko', 'uncertain')
        for error in (requests.Timeout('PRIVATE'), requests.Timeout('PRIVATE'), requests.ConnectionError('PRIVATE'),
                      api.InvalidTelegramResponse(200), api.InvalidTelegramResponse(503), ValueError('PRIVATE')):
            with self.assertRaises(type(error)):
                push.dispatch({}, req, None, send=Mock(side_effect=error), slug='takaneko')
        denied = api.TelegramError(400, http_status=400)
        self.assertEqual(push.dispatch({}, req, None, send=Mock(side_effect=denied), slug='other-private'),
                         {'ok': False, 'code': 400, 'retry_after': 0})
        self.assertEqual(push.dispatch({}, req, None, send=Mock(return_value={}), slug='takaneko'),
                         {'ok': False, 'error': 'uncertain'})
        self.assertEqual(push.dispatch({}, req, None, send=Mock(return_value={'message_id': 777})),
                         {'ok': True, 'message_id': 777})
        access.return_value = False
        self.assertEqual(push.dispatch({}, req, None), {'ok': False, 'code': 403})
        rows = error_log.recent()
        self.assertEqual(len(rows), 9)
        self.assertEqual(sum('timed out' in r['message'] for r in rows), 2)
        self.assertEqual(rows[1]['source'], 'Push → Telegram')
        self.assertIn('HTTP status 400', rows[1]['message'])
        self.assertIn('HTTP status 503', rows[3]['message'])
        self.assertIn('unrecognized Telegram response', rows[4]['message'])
        self.assertIn('connection', rows[5]['message'])
        self.assertIn('valid Telegram message ID', rows[0]['message'])
        self.assertNotIn('PRIVATE', json.dumps(rows))
        self.assertNotIn('424242', json.dumps(rows))

    def test_log_sink_failure_does_not_change_delivery_or_owner_alert_result(self):
        req = {'op': 'send', 'user_id': 1, 'kind': 'text', 'text': 'PRIVATE'}
        self.enterContext(patch.object(push.access, 'allowed', return_value=True))
        self.enterContext(patch.object(push, 'control', return_value={'enabled': True}))
        with core.db() as db:
            db.execute('DROP TABLE admin_errors')
        with self.assertLogs(level='WARNING'):
            result = push.dispatch({}, req, None, send=Mock(side_effect=api.TelegramError(429, retry=12)))
            self.assertEqual(result, {'ok': False, 'code': 429, 'retry_after': 12})
            self.assertEqual(push.report_health('other', 'network'), {'ok': True})
        with core.db() as db:
            self.assertEqual(tuple(db.execute('SELECT reason,platform FROM admin_alert_queue').fetchone()),
                             ('network', 'push_provider'))

    def test_telegram_api_preserves_actual_status_from_json_or_invalid_body(self):
        session = Mock()
        local = Mock(token='fixture', token_until=time.monotonic() + 600, session=session)
        with patch.object(api, '_transport', local):
            for status, body, expected in [
                    (503, ValueError('PRIVATE'), api.InvalidTelegramResponse),
                    (200, {'malformed': 'PRIVATE'}, api.InvalidTelegramResponse),
                    (502, {'ok': True}, api.InvalidTelegramResponse),
                    (429, {'ok': False, 'error_code': 429, 'parameters': None}, api.InvalidTelegramResponse),
                    (502, {'ok': False, 'error_code': 429, 'description': 'PRIVATE'}, api.InvalidTelegramResponse),
                    (200, {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 9}}, api.TelegramError),
                    (429, {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 9}}, api.TelegramError)]:
                response = Mock(status_code=status)
                response.json.side_effect = body if isinstance(body, Exception) else None
                response.json.return_value = body
                session.post.return_value = response
                with self.assertRaises(expected) as caught:
                    api.telegram('sendMessage', {'chat_id': 1, 'text': 'fixture'})
                self.assertEqual(caught.exception.http_status, status)
                self.assertNotIn('PRIVATE', str(caught.exception))
                if expected is api.TelegramError:
                    self.assertEqual(caught.exception.code, 429)
                    self.assertEqual(caught.exception.retry, 9)
                response.close.assert_called_once()

    def test_log_limit_and_restarts_do_not_reimport_trimmed_legacy_observations(self):
        with core.db() as db:
            db.execute("DELETE FROM settings WHERE key='admin_errors_imported_health'")
            db.execute("INSERT INTO settings VALUES ('push_health.takaneko','\"uncertain\"')")
        alerts.initialize()
        with core.db() as db:
            for i in range(error_log.LIMIT + 5):
                db.execute('INSERT INTO admin_errors(observed,source,reason) VALUES (?, ?, ?)',
                           (i, 'push_telegram', 'send_timeout'))
        alerts.initialize()
        rows = error_log.recent()
        self.assertEqual(len(rows), error_log.LIMIT)
        self.assertEqual((rows[0]['observed'], rows[-1]['observed']), (error_log.LIMIT + 4, 5))

    def test_failure_reason_records_verified_http_status_without_exception_text(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(state, 'CONTROL', Path(root)))
        call = self.enterContext(patch.object(transport, '_call'))
        state.initialize()
        for message, expected in [('Fanclub HTTP 503', 'fanclub_poll_http_503'),
                                  ('private URL', 'fanclub_poll')]:
            state.setting('takaneko_poll_error', message)
            transport.report_health()
            call.assert_called_with({'op': 'health', 'status': expected})
        transport.report_health(credentials_unavailable=True)
        call.assert_called_with({'op': 'health', 'status': 'broker_unavailable'})
        with state.db() as db:
            db.execute("INSERT INTO tk_posts(id,summary,state,discovered,error) VALUES ('private','{}','pending',0,'Fanclub HTTP 429')")
            db.execute("INSERT INTO tk_deliveries(post_id,user_id,created) VALUES ('private',1,0)")
        transport.report_health()
        call.assert_called_with({'op': 'health', 'status': 'fanclub_read_http_429'})
        with state.db() as db:
            db.execute("UPDATE tk_posts SET error='來源拒絕存取（403）；已跳過，保留待重試'")
        transport.report_health()
        call.assert_called_with({'op': 'health', 'status': 'fanclub_forbidden'})
        with state.db() as db:
            db.execute("UPDATE tk_posts SET error='Media HTTP 429'")
        transport.report_health()
        call.assert_called_with({'op': 'health', 'status': 'media_read_http_429'})
        push.report_health('takaneko', 'media_read_http_429')
        row, = error_log.recent()
        self.assertEqual((row['source'], row['message']), ('Takaneko media', 'The media request failed (HTTP 429).'))


if __name__ == '__main__':
    unittest.main()
