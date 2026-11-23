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

    def file(self, job, name='image.jpg', state='pending', size=None, **delivery):
        root = self.root / 'media' / job['id']; root.mkdir(exist_ok=True)
        path = root / name; path.write_bytes(b'synthetic fixture')
        w.add_files(job, [{'path': str(path), 'name': name, 'size': size or path.stat().st_size, **delivery}])
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
        self.assertEqual(names, {'settings', 'jobs', 'files', 'batch_requests', 'batch_items', 'telegram_inbox', 'media_budget', 'links'})

    def test_supported_routes(self):
        for url, expected in [('https://instagram.com/p/Abc/', ('instagram', 'choice')),
                              ('https://instagram.com/stories/example/123/', ('instagram', 'stories')),
                              ('https://www.tiktok.com/@example/video/123', ('tiktok', 'choice')),
                              ('https://x.com/example/status/123', ('twitter', 'post'))]:
            with self.subTest(url=url): self.assertEqual(c.classify(url), expected)

    def test_youtube_links_queue_a_single_canonical_video(self):
        base = 'https://www.youtube.com/watch?v=Abc123_X-yZ'
        urls = (base + '&list=PL123',
                'https://m.youtube.com/shorts/Abc123_X-yZ?si=share',
                'https://youtu.be/Abc123_X-yZ?t=30')
        for i, url in enumerate(urls):
            with self.subTest(url=url):
                job = self.job(url, key=f'youtube-{i}')
                self.assertEqual((job['url'], job['platform'], job['mode'], job['state']),
                                 (base, 'youtube', 'post', 'queued'))
        batch = c.enqueue_batch('mixed', 123, [urls[1], 'https://x.com/example/status/123'], 10)
        with c.db() as db:
            items = db.execute('SELECT platform, mode FROM batch_items WHERE job=? ORDER BY position',
                               (batch,)).fetchall()
        self.assertEqual(list(map(tuple, items)), [('youtube', 'post'), ('twitter', 'post')])

    def test_youtube_runner_delivers_only_final_file_and_reserves_youtube_quota(self):
        import json
        from io import StringIO
        from media_bot import gallery_runner, yt_runner
        url = 'https://www.youtube.com/watch?v=Abc123_X-yZ'
        final = self.root / 'Abc123_X-yZ_00_.mp4'
        def extract(downloader, url, **kwargs):
            self.assertEqual(kwargs['ie_key'], 'Youtube')
            self.assertTrue(downloader.params['noplaylist'])
            self.assertFalse(list(downloader.cookiejar))
            for name in ('Abc123_X-yZ_00_.f137.mp4', 'Abc123_X-yZ_00_.f140.m4a'):
                downloader.dl(str(self.root / name), {})
                (self.root / name).write_bytes(b'fixture-stream')
            final.write_bytes(b'fixture-video')
            (self.root / 'Abc123_X-yZ_00_.webm').write_bytes(b'older-download')
            return {'id': 'Abc123_X-yZ', 'requested_downloads': [{'filepath': str(final)}]}
        payload = {'platform': 'youtube', 'mode': 'post', 'root': str(self.root),
                   'url': url, 'cookies': {}}
        with patch('sys.stdin', StringIO(json.dumps(payload))), \
                patch.object(yt_runner.YoutubeDL, 'extract_info', extract), \
                patch.object(yt_runner.YoutubeDL, 'dl'):
            gallery_runner.main()
        result = json.loads((self.root / 'result.json').read_text())
        self.assertEqual((result['code'], result['media_attempts']), (0, 1))
        self.assertEqual([f['name'] for f in result['files']], [final.name])
        with c.db() as db:
            self.assertEqual([row['platform'] for row in db.execute('SELECT platform FROM media_budget')],
                             ['youtube'])

    def test_youtube_http_429_sets_only_youtube_cooldown(self):
        from media_bot import yt_runner
        response = Mock(status=429, headers={'Retry-After': '1200'})
        def extract(downloader, url, **kwargs):
            downloader.urlopen(url)
        with patch.object(yt_runner.YoutubeDL, 'extract_info', extract), \
                patch.object(yt_runner.YoutubeDL, 'urlopen', return_value=response):
            result = yt_runner.run({'platform': 'youtube', 'root': str(self.root),
                                    'url': 'https://youtu.be/Abc123_X-yZ'})
        self.assertEqual((result['code'], result['wait_reason'], result['files']), (1, 'platform', []))
        self.assertGreaterEqual(result['retry_at'], result['started'] + 1200)
        self.assertEqual(c.get_setting('youtube_cooldown'), result['retry_at'])
        self.assertIsNone(c.get_setting('tiktok_cooldown'))

    def test_youtube_non_video_links_are_rejected(self):
        for url in ('https://www.youtube.com/playlist?list=PL123',
                    'https://www.youtube.com/@example/shorts',
                    'https://youtu.be/Abc123_X-yZ/extra',
                    'https://www.youtube.com/watch?v=Abc123_X-yZ&v=Zyx123_X-yZ',
                    'https://www.youtube.com.evil.test/watch?v=Abc123_X-yZ'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.job(url)

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

    def test_original_is_delivered_as_a_separate_document_and_cleaned_after_confirmation(self):
        job = self.job()
        preview = self.file(job, 'preview.mp4', supports_streaming=True)
        original = self.file(job, 'original.mp4', force_document=True)
        for entry in (preview, original):
            w.add_files(job, [{k: entry[k] for k in ('path', 'name', 'size')}])
        sent = []
        def record(method, data, files=None):
            if files:
                name, stream, mime = next(iter(files.values()))
                sent.append((method, name, stream.read(), mime, data))
            return {'message_id': 100 + len(sent)}
        self.telegram.side_effect = record
        with patch.object(w.video, 'attributes', return_value={'width': 240, 'height': 320}) as probe:
            w.deliver(job)
        probe.assert_called_once_with(Path(preview['path']))
        self.assertEqual([it[:2] for it in sent],
                         [('sendVideo', preview['name']), ('sendDocument', original['name'])])
        self.assertIs(sent[0][4]['supports_streaming'], True)
        self.assertIs(sent[1][4]['disable_content_type_detection'], True)
        self.assertEqual(sent[1][2:4], (Path(original['path']).read_bytes(), 'application/octet-stream'))
        with c.db() as db:
            receipts = list(db.execute('SELECT telegram_id FROM files WHERE job=? ORDER BY id', (job['id'],)))
        self.assertEqual([r[0] for r in receipts], ['101', '102'])
        c.cleanup()
        self.assertTrue(all(Path(f['path']).exists() for f in (preview, original)))
        c.update(job['id'], delivered=time.time() - 90000)
        c.cleanup()
        self.assertEqual([self.file_state(f) for f in (preview, original)], ['expired', 'expired'])
        self.assertTrue(all(not Path(f['path']).exists() for f in (preview, original)))

    def test_uncertain_document_preserves_both_files_without_resending_preview(self):
        job = self.job()
        preview = self.file(job, 'preview.mp4', supports_streaming=True)
        original = self.file(job, '原片.webm', force_document=True)
        def send(method, *args):
            if method == 'sendDocument':
                raise TimeoutError()
            return {'message_id': 42}
        self.telegram.side_effect = send
        with patch.object(w.video, 'attributes', return_value={'width': 240, 'height': 320}):
            w.deliver(job)
            w.deliver(c.job_by_id(job['id']))
        attempts = [call.args[0] for call in self.telegram.call_args_list
                    if call.args[0] in ('sendVideo', 'sendDocument')]
        self.assertEqual(attempts, ['sendVideo', 'sendDocument'])
        self.assertEqual([self.file_state(f) for f in (preview, original)], ['sent', 'uncertain'])
        self.assertEqual(c.job_by_id(job['id'])['state'], 'partial')
        c.update(job['id'], delivered=time.time() - 90000)
        c.cleanup()
        self.assertTrue(all(Path(f['path']).exists() for f in (preview, original)))

    def test_startup_reaps_only_reserved_scratch_in_real_job_directories(self):
        media = self.root / 'media'
        job_dir = media / ('1' * 16)
        batch_dir = job_dir / 'item-1'
        batch_dir.mkdir(parents=True)
        other = self.root / 'unrelated'
        other.mkdir()
        (job_dir / 'item-2').symlink_to(other, target_is_directory=True)
        scratch = '.playback-' + '2' * 32 + '.encoding.mp4'
        for directory in (job_dir, batch_dir, other):
            (directory / scratch).write_bytes(b'partial')
            (directory / 'source.mkv').write_bytes(b'original')
        w._recover_playback_scratch()
        for directory in (job_dir, batch_dir, other):
            self.assertEqual((directory / scratch).exists(), directory == other)
            self.assertEqual((directory / 'source.mkv').read_bytes(), b'original')

    def test_batch_conversion_failure_delivers_original_and_retains_it(self):
        key = c.enqueue_batch('batch', 123, ['https://youtu.be/Abc123_X-yZ'], 10)
        job = c.job_by_id(key)
        root = self.root / 'media' / key / 'item-0'
        root.mkdir(parents=True)
        path = root / 'original.mp4'
        path.write_bytes(b'original content')
        result = {'files': [{'path': str(path), 'name': path.name, 'size': path.stat().st_size,
                             'force_document': True}],
                  'code': 1, 'retry_at': 0, 'failure': 'conversion_failed'}
        with patch.object(w, 'capture', return_value=result), patch.object(w, 'disk_ok'):
            w.process_batch(job)
            w.process_batch(c.job_by_id(key))
        calls = [call.args[0] for call in self.telegram.call_args_list]
        self.assertEqual(calls.count('sendDocument'), 1)
        self.assertNotIn('sendVideo', calls)
        self.assertEqual(c.job_by_id(key)['state'], 'partial')
        c.update(key, delivered=time.time() - 90000)
        c.cleanup()
        self.assertTrue(path.exists())

    def test_rejected_preview_is_not_sent_as_a_duplicate_document(self):
        job = self.job()
        preview = self.file(job, 'preview.mp4', supports_streaming=True)
        original = self.file(job, 'original.mov', force_document=True)
        def reject(method, *args):
            if method == 'sendVideo':
                raise w.TelegramError(400)
            return {'message_id': 45}
        self.telegram.side_effect = reject
        with patch.object(w.video, 'attributes', return_value={'width': 240, 'height': 320}):
            w.deliver(job)
            w.deliver(c.job_by_id(job['id']))
        sent = [call.args[2]['document'][0] for call in self.telegram.call_args_list
                if call.args[0] == 'sendDocument']
        self.assertEqual(sent, [original['name']])
        self.assertEqual([self.file_state(f) for f in (preview, original)], ['preview_failed', 'sent'])
        self.assertEqual(c.job_by_id(job['id'])['state'], 'partial')

    def test_album_delivery_records_all_receipts(self):
        job = self.job(); files = [self.file(job, name=f'image{i}.jpg') for i in range(2)]
        self.telegram.side_effect = lambda method, *args: [{'message_id': 1}, {'message_id': 2}] if method == 'sendMediaGroup' else {'message_id': 101}
        w.deliver(job)
        self.assertEqual([self.file_state(f) for f in files], ['sent', 'sent'])
        self.assertEqual(c.job_by_id(job['id'])['state'], 'completed')

    def record_media_calls(self, reject=None):
        calls = []
        def send(method, data, files=None):
            if not files:
                return {'message_id': 100}
            names = [attachment[0] for attachment in files.values()]
            calls.append((method, names))
            if reject:
                reject(method, names)
            if method == 'sendMediaGroup':
                return [{'message_id': 1000 + 10 * len(calls) + i} for i in range(len(names))]
            return {'message_id': 1000 + 10 * len(calls)}
        self.telegram.side_effect = send
        return calls

    def test_mixed_post_groups_large_local_video_and_photos_around_document(self):
        job = self.job()
        photos = [f'photo{i}.jpg' for i in range(4)]
        for name in photos[:2]:
            self.file(job, name)
        self.file(job, 'original.bin')
        for name in photos[2:]:
            self.file(job, name)
        self.file(job, 'video.mp4', size=220_000_000)
        calls = self.record_media_calls()
        with patch.dict(os.environ, {'MEDIA_BOT_API_BASE': 'http://127.0.0.1:8081'}), \
                patch.object(w.video, 'attributes', return_value={'width': 640, 'height': 480}):
            w.deliver(job)
        self.assertEqual(calls, [('sendMediaGroup', photos + ['video.mp4']),
                                 ('sendDocument', ['original.bin'])])
        self.assertEqual(c.job_by_id(job['id'])['state'], 'completed')

    def test_cloud_oversize_keeps_other_photos_in_album(self):
        job = self.job()
        self.file(job, 'a.jpg')
        video = self.file(job, 'large.mp4', size=220_000_000)
        self.file(job, 'b.jpg')
        calls = self.record_media_calls()
        with patch.object(w.links, 'enabled', return_value=False):
            w.deliver(job)
        self.assertEqual(calls, [('sendMediaGroup', ['a.jpg', 'b.jpg'])])
        self.assertEqual(self.file_state(video), 'too_large')

    def test_album_overflow_avoids_singleton(self):
        job = self.job()
        names = [f'{i:02}.jpg' for i in range(21)]
        for name in names:
            self.file(job, name)
        calls = self.record_media_calls()
        w.deliver(job)
        self.assertEqual(calls, [('sendMediaGroup', names[:10]), ('sendMediaGroup', names[10:19]),
                                 ('sendMediaGroup', names[19:])])

    def test_definite_album_rejection_salvages_accepted_halves(self):
        job = self.job()
        names = [f'{i}.jpg' for i in range(6)]
        files = [self.file(job, name) for name in names]
        def reject(method, members):
            if method == 'sendMediaGroup' and names[2] in members:
                raise w.TelegramError(400)
        calls = self.record_media_calls(reject)
        w.deliver(job)
        self.assertEqual(calls, [('sendMediaGroup', names), ('sendMediaGroup', names[:3]),
                                 ('sendMediaGroup', names[:2]), ('sendPhoto', [names[2]]),
                                 ('sendMediaGroup', names[3:])])
        self.assertEqual([self.file_state(f) for f in files], ['sent'] * 6)
        w.deliver(c.job_by_id(job['id']))
        self.assertEqual(len(calls), 5)

    def test_ambiguous_subgroup_never_replays(self):
        job = self.job()
        names = [f'{i}.jpg' for i in range(4)]
        files = [self.file(job, name) for name in names]
        def reject(method, members):
            if len(members) == 4:
                raise w.TelegramError(413)
            if names[2] in members:
                raise TimeoutError()
        calls = self.record_media_calls(reject)
        w.deliver(job)
        w.deliver(c.job_by_id(job['id']))
        self.assertEqual(calls, [('sendMediaGroup', names), ('sendMediaGroup', names[:2]),
                                 ('sendMediaGroup', names[2:])])
        self.assertEqual([self.file_state(f) for f in files], ['sent', 'sent', 'uncertain', 'uncertain'])
        self.assertEqual(c.job_by_id(job['id'])['state'], 'partial')

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
