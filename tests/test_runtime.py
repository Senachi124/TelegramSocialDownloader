"""Offline regression coverage for the extracted queue and Telegram worker."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

# Never put test media or SQLite files into an implicit system temp directory.
_TEST_ROOT = Path(__file__).resolve().parents[1] / '.test-work'
_TEST_ROOT.mkdir(exist_ok=True)
_BOOTSTRAP = tempfile.TemporaryDirectory(prefix='import-', dir=_TEST_ROOT)
with patch.dict(os.environ, {'MEDIA_BOT_DATA': _BOOTSTRAP.name}):
    from media_bot import core as c, worker as w, inbox, config


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='runtime-', dir=_TEST_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'MEDIA_BOT_DATA': str(self.root), 'MEDIA_BOT_ALLOWED_USERS': '123,456'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.data = patch.object(c, 'DATA', self.root)
        self.data.start(); self.addCleanup(self.data.stop)
        self.network = patch.object(w, 'telegram', return_value={'message_id': 101})
        self.telegram = self.network.start(); self.addCleanup(self.network.stop)
        w.STOP.clear(); self.addCleanup(w.STOP.clear)
        c.initialize()

    def job(self, url='https://x.com/example/status/123', key='event', chat=123):
        return c.job_by_id(c.enqueue(key, chat, url))

    def file(self, job, name='image.jpg', state='pending', size=None):
        root = self.root / 'media' / job['id']; root.mkdir(exist_ok=True)
        path = root / name; path.write_bytes(b'synthetic fixture')
        w.add_files(job, [{'path': str(path), 'name': name, 'size': size or path.stat().st_size}])
        with c.db() as db:
            db.execute('UPDATE files SET state=? WHERE path=?', (state, str(path)))
            return dict(db.execute('SELECT * FROM files WHERE path=?', (str(path),)).fetchone())

    def file_state(self, file):
        with c.db() as db:
            return db.execute('SELECT state FROM files WHERE id=?', (file['id'],)).fetchone()[0]

    def update(self, uid=1, chat=123, text='/help', kind='private', actor=None):
        return {'update_id': uid, 'message': {'message_id': uid, 'chat': {'id': chat, 'type': kind},
                'from': {'id': chat if actor is None else actor}, 'text': text}}

    def test_schema_contains_only_download_tables(self):
        with c.db() as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {'settings', 'jobs', 'files', 'batch_requests', 'batch_items', 'telegram_inbox', 'media_budget'})

    def test_supported_routes(self):
        for url, expected in [('https://instagram.com/p/Abc/', ('instagram', 'choice')),
                              ('https://instagram.com/stories/example/123/', ('instagram', 'stories')),
                              ('https://www.tiktok.com/@example/video/123', ('tiktok', 'choice')),
                              ('https://x.com/example/status/123', ('twitter', 'post'))]:
            with self.subTest(url=url): self.assertEqual(c.classify(url), expected)

    def test_rejects_unrequested_and_unsafe_hosts(self):
        for url in ('https://threads.net/@example/post/abc', 'http://x.com/example/status/123',
                    'https://x.com.evil.test/example/status/123', 'https://user:password@x.com/example/status/123',
                    'https://127.0.0.1/', 'https://x.com:444/example/status/123'):
            with self.subTest(url=url), self.assertRaises(ValueError): c.validate_url(url)

    def test_static_access_fails_closed(self):
        self.assertTrue(w.authorized_update(self.update()))
        for update in (self.update(chat=999), self.update(kind='group'), self.update(actor=456)):
            self.assertFalse(w.authorized_update(update))
        with patch.dict(os.environ, {'MEDIA_BOT_ALLOWED_USERS': ''}):
            self.assertFalse(w.authorized_update(self.update()))
            with self.assertRaises(ValueError): self.job()

    def test_duplicate_update_creates_one_job(self):
        first = self.job()
        self.assertEqual(first['id'], self.job()['id'])
        with c.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_help_is_available_without_management_modules(self):
        w.handle_update(self.update())
        self.assertIn('Instagram', self.telegram.call_args.args[1]['text'])
        with c.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_choice_is_scoped_to_original_chat(self):
        job = self.job('https://instagram.com/p/Abc/')
        query = {'update_id': 2, 'callback_query': {'id': 'callback', 'from': {'id': 456},
                 'message': {'chat': {'id': 456, 'type': 'private'}}, 'data': job['id'] + ':post'}}
        w.handle_update(query)
        self.assertEqual(c.job_by_id(job['id'])['state'], 'choice')
        query['callback_query']['from']['id'] = 123
        query['callback_query']['message']['chat']['id'] = 123
        w.handle_update(query)
        self.assertEqual(c.job_by_id(job['id'])['state'], 'queued')

    def test_batch_preserves_order_and_independent_invalid_link(self):
        urls = ['https://x.com/example/status/1', 'https://invalid.test/', 'https://instagram.com/p/Abc/']
        key = c.enqueue_batch('batch', 123, urls, 10)
        with c.db() as db:
            rows = list(db.execute('SELECT * FROM batch_items WHERE job=? ORDER BY position', (key,)))
        self.assertEqual([r['state'] for r in rows], ['pending', 'failed', 'pending'])
        self.assertEqual([r['original_url'] for r in rows], urls)
        self.assertEqual(rows[2]['mode'], 'post')

    def test_batch_rejects_more_than_fifty(self):
        with self.assertRaises(ValueError): c.enqueue_batch('too-many', 123, ['https://x.com/example/status/1'] * 51, 10)

    def test_batch_handoff_downloads_each_platform(self):
        key = c.enqueue_batch('batch', 123, ['https://x.com/example/status/1', 'https://instagram.com/p/Abc/'], 10)
        result = {'retry_at': 0, 'auth_required': False, 'code': 0, 'files': [], 'no_media': True}
        with patch.object(config, 'cookies', return_value={'fixture': 'value'}), patch.object(w, 'disk_ok'), patch.object(w, 'capture', return_value=result):
            w.process_batch(c.job_by_id(key))
        self.assertEqual(c.job_by_id(key)['platform'], 'instagram')
        with c.db() as db: self.assertEqual(db.execute('SELECT state FROM batch_items WHERE job=? AND position=0', (key,)).fetchone()[0], 'failed')

    def test_download_uses_external_cookie_input(self):
        job = self.job()
        result = {'retry_at': 0, 'auth_required': False, 'code': 0, 'files': []}
        with patch.object(config, 'cookies', return_value={'fixture': 'value'}) as cookies, patch.object(w, 'disk_ok'), patch.object(w, 'capture', return_value=result) as capture:
            self.assertTrue(w.download(job))
        cookies.assert_called_once_with('twitter')
        self.assertEqual(capture.call_args.args[1], {'fixture': 'value'})

    def test_missing_required_cookie_pauses_without_download(self):
        job = self.job()
        with patch.object(config, 'cookies', return_value={}), patch.object(w, 'capture') as capture:
            self.assertFalse(w.download(job))
        capture.assert_not_called()
        self.assertEqual(c.job_by_id(job['id'])['state'], 'paused')

    def test_cooldown_is_durable_and_prevents_capture(self):
        job = self.job(); until = time.time() + 600; c.setting('twitter_cooldown', until)
        with patch.object(w, 'capture') as capture: self.assertFalse(w.download(job))
        capture.assert_not_called()
        self.assertEqual(c.job_by_id(job['id'])['retry_at'], until)

    def test_successful_single_delivery_records_receipt(self):
        job = self.job(); file = self.file(job)
        w.deliver(job)
        self.assertEqual(self.file_state(file), 'sent')
        self.assertEqual(c.job_by_id(job['id'])['state'], 'completed')
        self.assertTrue(any(call.args[0] == 'sendPhoto' for call in self.telegram.call_args_list))

    def test_album_delivery_records_all_receipts(self):
        job = self.job(); files = [self.file(job, name=f'image{i}.jpg') for i in range(2)]
        self.telegram.side_effect = lambda method, *args: [{'message_id': 1}, {'message_id': 2}] if method == 'sendMediaGroup' else {'message_id': 101}
        w.deliver(job)
        self.assertEqual([self.file_state(f) for f in files], ['sent', 'sent'])
        self.assertEqual(c.job_by_id(job['id'])['state'], 'completed')

    def test_ambiguous_delivery_is_never_resent(self):
        job = self.job(); file = self.file(job)
        def request(method, *args):
            if method == 'sendPhoto': raise TimeoutError('synthetic transport failure')
            return {'message_id': 101}
        self.telegram.side_effect = request
        w.deliver(job)
        self.assertEqual(self.file_state(file), 'uncertain')
        self.assertEqual(c.job_by_id(job['id'])['state'], 'partial')
        self.telegram.reset_mock()
        w.deliver(c.job_by_id(job['id']))
        self.assertFalse(any(call.args[0] == 'sendPhoto' for call in self.telegram.call_args_list))
        self.assertTrue(Path(file['path']).exists())

    def test_definite_rate_limit_preserves_pending_delivery(self):
        job = self.job(); file = self.file(job)
        def request(method, *args):
            if method == 'sendPhoto': raise w.TelegramError(429, 30)
            return {'message_id': 101}
        self.telegram.side_effect = request
        w.deliver(job)
        self.assertEqual(self.file_state(file), 'pending')
        self.assertEqual(c.job_by_id(job['id'])['state'], 'sending')
        self.assertGreater(c.job_by_id(job['id'])['retry_at'], time.time())

    def test_oversize_file_is_retained_without_upload_or_link(self):
        job = self.job(); file = self.file(job, name='large.mp4', size=49 * 1024**2)
        w.deliver(job)
        self.assertEqual(self.file_state(file), 'too_large')
        self.assertEqual(c.job_by_id(job['id'])['state'], 'partial')
        self.assertTrue(Path(file['path']).exists())
        self.assertFalse(any(call.args[0] in ('sendVideo', 'sendDocument') for call in self.telegram.call_args_list))
        self.assertIn('49 MiB', c.job_by_id(job['id'])['message'])
        self.telegram.reset_mock()
        w.deliver(c.job_by_id(job['id']))
        self.assertFalse(any(call.args[0] in ('sendVideo', 'sendDocument') for call in self.telegram.call_args_list))

    def test_cleanup_preserves_failed_uncertain_and_oversize(self):
        for i, state in enumerate(('sent', 'uncertain', 'too_large', 'pending')):
            job = self.job(key=str(i)); file = self.file(job, state=state)
            c.update(job['id'], state='completed' if state == 'sent' else 'partial', delivered=time.time() - 90000)
            c.cleanup()
            self.assertEqual(Path(file['path']).exists(), state != 'sent')

    def test_file_guard_rejects_outside_root_and_symlink(self):
        outside = self.root / 'outside'; outside.write_text('fixture')
        with self.assertRaises(ValueError): c.safe_file(outside)
        link = self.root / 'media' / 'link'; link.symlink_to(outside)
        with self.assertRaises(ValueError): c.safe_file(link)

    def test_quota_is_durable_and_idempotent(self):
        c.setting('twitter_media_limit', 1)
        self.assertTrue(c.reserve_media('twitter', 'one', at=100))
        self.assertFalse(c.reserve_media('twitter', 'one', at=101))
        with self.assertRaises(c.RateLimited): c.reserve_media('twitter', 'two', at=102)
        self.assertTrue(c.reserve_media('twitter', 'two', at=161))

    def test_inbox_rejects_disallowed_and_mismatched_sender(self):
        inbox.store([self.update(uid=1), self.update(uid=2, chat=999), self.update(uid=3, actor=456)])
        with c.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM telegram_inbox').fetchone()[0], 1)
        self.assertEqual(c.get_setting('telegram_offset'), 4)

    def test_inbox_keeps_chat_order_and_recovers_without_replay(self):
        inbox.store([self.update(uid=1), self.update(uid=2), self.update(uid=3, chat=456)])
        self.assertEqual(inbox.claim()['id'], 1)
        self.assertEqual(inbox.claim()['id'], 3)
        self.assertIsNone(inbox.claim())
        inbox.recover()
        self.assertEqual(inbox.claim()['id'], 2)
        with c.db() as db: self.assertEqual(db.execute("SELECT count(*) FROM telegram_inbox WHERE state='uncertain'").fetchone()[0], 2)

    def test_shutdown_terminates_active_capture_process(self):
        job = self.job()
        process = Mock(pid=321)
        process.poll.side_effect = [None, 0]
        w.STOP.set()
        with patch.object(w.subprocess, 'Popen', return_value=process), patch.object(w.os, 'killpg') as kill:
            self.assertIsNone(w.capture(job, {}))
        kill.assert_called_once_with(321, w.signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=10)

    def test_low_disk_reserve_refuses_new_download(self):
        with patch.object(w.shutil, 'disk_usage', return_value=Mock(free=1024**3)):
            with self.assertRaises(ValueError): w.disk_ok()


if __name__ == '__main__':
    unittest.main()
