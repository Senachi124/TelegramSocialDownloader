import fcntl
import io
import json
import os
from pathlib import Path
import pwd
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from media_bot import push_socket
from takaneko_telegram import __main__ as cli, state, takaneko_feed as feed, transport


class DeliveryReviewTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(state, 'CONTROL', root))
        self.allowed = self.enterContext(patch.object(transport, 'review_allowed', return_value=True))
        state.initialize()
        feed.set_enabled(True)
        feed.set_subscription(42, True)
        feed.record_inventory([])

    def delivery(self, post='fixture', kinds=('text', 'text')):
        feed.record_inventory([{'id': post}])
        payloads = [json.dumps({'kind': kind, 'html': 'private article text https://private.invalid'})
                    for kind in kinds]
        with state.db() as db:
            db.execute('UPDATE tk_posts SET plan=? WHERE id=?', ('[]', post))
            row = dict(db.execute('SELECT * FROM tk_deliveries WHERE post_id=?', (post,)).fetchone())
            for position, payload in enumerate(payloads):
                db.execute("INSERT INTO tk_parts VALUES (?,?,?,'pending',NULL)", (row['id'], position, payload))
        return row

    @staticmethod
    def control(op, **kwargs):
        return transport.control(11, dict(op=op, **kwargs), None)

    def items(self, **kwargs):
        return self.control('review_deliveries', **kwargs)['items']

    def decide(self, item, decision='sent'):
        return self.control('resolve_delivery', **{k: item[k] for k in ('delivery_id', 'position', 'attempt')},
                            decision=decision)

    def states(self, delivery):
        with state.db() as db:
            return [tuple(p) for p in db.execute('SELECT state,telegram_id FROM tk_parts WHERE delivery_id=? '
                                                 'ORDER BY position', (delivery['id'],))]

    def test_confirm_one_part_preserves_receipt_and_sends_only_remaining_part(self):
        delivery = self.delivery(kinds=('text', 'text', 'text'))
        first_send = Mock(side_effect=[{'message_id': 77}, TimeoutError()])
        feed.deliver(delivery, first_send)
        item, = self.items()
        self.assertEqual(set(item), {'delivery_id', 'user_id', 'created', 'position', 'total', 'kind', 'attempt'})
        self.assertEqual((item['user_id'], item['created'], item['position'], item['total'], item['kind']),
                         (42, delivery['created'], 1, 3, 'text'))
        self.assertRegex(item['attempt'], '^[0-9a-f]{32}$')
        self.assertEqual(self.decide(item), {'ok': True, 'decision': 'sent'})
        self.assertEqual(self.decide(item, 'retry'), {'ok': False, 'error': 'stale'})
        self.assertEqual(self.items(), [])
        self.assertEqual(self.states(delivery), [('sent', '77'), ('sent', None), ('pending', None)])
        remaining = Mock(return_value={'message_id': 79})
        feed.deliver(delivery, remaining)
        remaining.assert_called_once()
        self.assertEqual(self.states(delivery), [('sent', '77'), ('sent', None), ('sent', '79')])
        with state.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tk_review_attempts').fetchone()[0], 0)

    def test_same_part_retry_gets_new_attempt_and_stale_browser_submission_fails(self):
        delivery = self.delivery()
        failed = Mock(side_effect=transport.TelegramError(502))
        feed.deliver(delivery, failed)
        first, = self.items()
        self.assertEqual(self.decide(first, 'retry'), {'ok': True, 'decision': 'retry'})
        self.assertEqual(failed.call_count, 1)
        feed.deliver(delivery, failed)
        second, = self.items()
        self.assertEqual(second['position'], first['position'])
        self.assertNotEqual(second['attempt'], first['attempt'])
        self.assertEqual(self.decide(first), {'ok': False, 'error': 'stale'})
        self.assertEqual(self.decide(dict(second, position=1)), {'ok': False, 'error': 'stale'})
        self.assertEqual(self.states(delivery), [('uncertain', None), ('pending', None)])
        args = ['takaneko', 'resolve', str(delivery['id']), 'sent', '--position', '0', '--attempt', second['attempt']]
        with patch('sys.argv', args):
            cli.main()
        self.assertEqual(self.states(delivery), [('sent', None), ('pending', None)])

    def test_inflight_network_and_receipt_checkpoint_cannot_be_resolved(self):
        delivery = self.delivery(kinds=('text',))
        save = feed._save_receipt
        checked = []

        def check(point):
            with state.db() as db:
                attempt = db.execute('SELECT attempt FROM tk_review_attempts').fetchone()[0]
            # A second process can initialize before it learns it isn't the sender.
            state.initialize()
            self.assertEqual(self.items(), [])
            self.assertEqual(self.control('resolve_delivery', delivery_id=delivery['id'], position=0,
                                          attempt=attempt, decision='retry'), {'ok': False, 'error': 'stale'})
            with self.assertRaises(ValueError):
                feed.current_review(delivery['id'])
            checked.append(point)

        def send(*args):
            check('send')
            return {'message_id': 88}

        def save_after_check(*args):
            check('receipt')
            return save(*args)

        with patch.object(feed, '_save_receipt', side_effect=save_after_check):
            feed.deliver(delivery, send)
        self.assertEqual(checked, ['send', 'receipt'])
        self.assertEqual(self.states(delivery), [('sent', '88')])

    def test_recover_unfinished_sender_only_under_singleton(self):
        delivery = self.delivery()
        feed.deliver(delivery, Mock(side_effect=TimeoutError))
        original, = self.items()
        with state.db() as db:
            db.execute('UPDATE tk_review_attempts SET active=1')
        stop = threading.Event()
        stop.set()
        with (state.CONTROL / 'takaneko.lock').open('a') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            feed.run(stop)
            self.assertEqual(self.items(), [])
        feed.run(stop)
        self.assertEqual(self.items(), [original])
        self.assertEqual(self.states(delivery), [('uncertain', None), ('pending', None)])

    def test_multiple_ambiguous_parts_excluded_posts_and_revoked_access_fail_closed(self):
        delivery = self.delivery()
        feed.deliver(delivery, Mock(side_effect=TimeoutError))
        item, = self.items()
        with state.db() as db:
            db.execute("UPDATE tk_parts SET state='uncertain' WHERE position=1")
        self.assertEqual(self.items(), [])
        self.assertEqual(self.decide(item)['error'], 'stale')
        with self.assertRaises(ValueError):
            feed.current_review(delivery['id'])
        with state.db() as db:
            db.execute("UPDATE tk_parts SET state='pending' WHERE position=1")
        with patch.object(feed, 'EXCLUDED_POSTS', {delivery['post_id']}):
            self.assertEqual(self.items(), [])
            self.assertEqual(self.decide(item)['error'], 'stale')
        self.allowed.return_value = False
        self.assertEqual(self.decide(item, 'retry')['error'], 'stale')
        self.allowed.side_effect = RuntimeError('private detail')
        self.assertEqual(self.decide(item, 'retry'), {'ok': False, 'error': 'unavailable'})
        feed.set_subscription(42, False)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.decide(item)['error'], 'stale')
        self.assertEqual(self.states(delivery), [('uncertain', None), ('pending', None)])

    def test_cli_requires_snapshot_or_new_interactive_confirmation(self):
        delivery = self.delivery()
        feed.deliver(delivery, Mock(side_effect=TimeoutError))
        item, = self.items()
        with patch('sys.argv', ['takaneko', 'pending']), patch('sys.stdout', new_callable=io.StringIO) as output:
            cli.main()
        self.assertIn(f"position=0 attempt={item['attempt']}", output.getvalue())
        self.assertIn('recipient 42, part 1/2', output.getvalue())
        args = ['takaneko', 'resolve', str(delivery['id']), 'retry']
        with patch('sys.argv', args), patch('sys.stdin.isatty', return_value=False), patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main()
        self.assertEqual(self.items(), [item])
        with patch('sys.argv', args), patch('sys.stdin.isatty', return_value=True), patch('sys.stdout', io.StringIO()):
            with patch('builtins.input', return_value='cancel'), self.assertRaises(SystemExit):
                cli.main()
            self.assertEqual(self.items(), [item])
            with patch('builtins.input', return_value='retry'):
                cli.main()
        self.assertEqual(self.states(delivery), [('pending', None), ('pending', None)])

    def test_timed_out_socket_stays_fenced_while_queued_and_during_bot_dispatch(self):
        delivery = self.delivery(kinds=('text',))
        path = str(state.CONTROL / 'push.sock')
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
            server.bind(path)
            server.listen(1)
            env = {'TAKANEKO_PUSH_SOCKET': path, 'TAKANEKO_BOT_USER': pwd.getpwuid(os.getuid()).pw_name}

            def short_exchange(path, uid, payload, **kwargs):
                return push_socket.exchange(path, uid, payload, **{**kwargs, 'timeout': 0.1})

            with patch.dict(os.environ, env), patch.object(transport, 'allowed', return_value=True), patch.object(
                    transport, 'exchange', side_effect=short_exchange):
                feed.deliver(delivery)
            # The producer timed out before the Bot even accepted this socket.
            with state.db() as db:
                active, attempt = db.execute('SELECT active,attempt FROM tk_review_attempts').fetchone()
            self.assertEqual(active, 0)
            stop = threading.Event()
            stop.set()
            feed.run(stop)
            self.assertEqual(self.items(), [])
            self.assertEqual(self.control('resolve_delivery', delivery_id=delivery['id'], position=0,
                                          attempt=attempt, decision='retry')['error'], 'stale')
            received = threading.Event()
            finish = threading.Event()

            def dispatch(uid, request, fd):
                self.assertIsNone(fd)
                self.assertEqual(request['op'], 'send')
                self.assertNotIn('_send_guard', request)
                received.set()
                finish.wait(5)
                return {'ok': True, 'message_id': 7}

            connection, _ = server.accept()
            worker = threading.Thread(target=push_socket._serve_one,
                                      args=(connection, {os.getuid()}, dispatch, threading.Semaphore()))
            worker.start()
            try:
                self.assertTrue(received.wait(5))
                self.assertEqual(self.items(), [])
            finally:
                finish.set()
                worker.join(5)
            item, = self.items()
            self.assertEqual(item['attempt'], attempt)
            self.assertEqual(self.decide(item), {'ok': True, 'decision': 'sent'})
            self.assertEqual(list(state.CONTROL.glob('.sending-*')), [])

    def test_two_descriptor_photo_request_preserves_media_and_releases_guard_before_reply(self):
        path = str(state.CONTROL / 'photo.sock')
        photo = state.CONTROL / 'photo.jpg'
        photo.write_bytes(b'fixture')
        attempt = 'a' * 32
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
            server.bind(path)
            server.listen(1)

            def dispatch(uid, request, fd):
                self.assertFalse(transport.send_finished(attempt))
                return {'ok': os.read(fd, 7) == b'fixture'}

            def receive():
                client, _ = server.accept()
                push_socket._serve_one(client, {os.getuid()}, dispatch, threading.Semaphore())

            worker = threading.Thread(target=receive)
            worker.start()
            with photo.open('rb') as stream, transport.send_guard(attempt):
                env = {'TAKANEKO_PUSH_SOCKET': path, 'TAKANEKO_BOT_USER': pwd.getpwuid(os.getuid()).pw_name}
                with patch.dict(os.environ, env):
                    reply = transport._call({'op': 'send', 'kind': 'photo'}, stream=stream)
                self.assertEqual(reply, {'ok': True})
            self.assertTrue(transport.send_finished(attempt))
            worker.join(5)

    def test_subscription_rechecked_after_access_probe_and_parallel_decisions(self):
        delivery = self.delivery()
        feed.deliver(delivery, Mock(side_effect=TimeoutError))
        item, = self.items()

        def revoke(uid):
            feed.set_subscription(uid, False)
            return True

        self.allowed.side_effect = revoke
        self.assertEqual(self.decide(item, 'retry')['error'], 'stale')
        feed.set_subscription(42, True)
        gate = threading.Barrier(2)
        self.allowed.side_effect = lambda uid: gate.wait(timeout=5) >= 0
        replies = []
        workers = [threading.Thread(target=lambda decision: replies.append(self.decide(item, decision)),
                                    args=(decision,)) for decision in ('sent', 'retry')]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        self.assertEqual(sorted(reply['ok'] for reply in replies), [False, True])
        self.assertEqual(self.items(), [])

    def test_legacy_migration_returns_safe_metadata_in_bounded_pages(self):
        for i in range(23):
            self.delivery(post=f'private-post-{i}', kinds=('https://private.invalid/secret',))
        with state.db() as db:
            db.execute("UPDATE tk_deliveries SET state='uncertain'")
            db.execute("UPDATE tk_parts SET state='uncertain'")
        state.initialize()
        with patch.object(transport, 'send_finished', wraps=transport.send_finished) as guard:
            first = self.control('review_deliveries')
            self.assertEqual(guard.call_count, feed.REVIEW_PAGE_SIZE + 1)
        self.assertEqual(len(first['items']), 20)
        self.assertEqual(first['next_after'], first['items'][-1]['delivery_id'])
        with patch.object(transport, 'send_finished', wraps=transport.send_finished) as guard:
            last = self.control('review_deliveries', after=first['next_after'])
            self.assertEqual(guard.call_count, 3)
        self.assertEqual(len(last['items']), 3)
        self.assertIsNone(last['next_after'])
        ids = [item['delivery_id'] for item in first['items'] + last['items']]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertTrue(all(item['kind'] == 'attachment' for item in first['items']))
        self.assertNotIn('private', json.dumps(first))
        state.initialize()
        self.assertEqual(first, self.control('review_deliveries'))
        for invalid in (-1, None, '2', False, 2**63):
            self.assertEqual(self.control('review_deliveries', after=invalid), {'ok': False, 'error': 'invalid'})
        item = first['items'][0]
        for fields in ({'delivery_id': True}, {'position': -1}, {'attempt': ''}, {'decision': None}):
            args = {k: item[k] for k in ('delivery_id', 'position', 'attempt')}
            self.assertEqual(self.control('resolve_delivery', **{**args, 'decision': 'sent', **fields}),
                             {'ok': False, 'error': 'invalid'})


if __name__ == '__main__':
    unittest.main()
