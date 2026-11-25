"""Exercise translation feature changes between Telegram reception and dispatch."""
from pathlib import Path
from queue import Full, Queue
import tempfile
import unittest
from unittest.mock import patch

from test_runtime import _TEST_ROOT
from media_bot import access, core, features, inbox, worker
from media_bot.translations import albums, commands


class TranslationFeatureTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory(dir=_TEST_ROOT))
        self.enterContext(patch.object(core, 'DATA', Path(folder)))
        core.initialize()
        access.set_owner(123)
        self.queue = self.enterContext(patch.object(worker, '_TRANSLATIONS'))
        self.enterContext(patch.object(worker, '_TRANSLATION_BUSY', Queue()))
        self.telegram = self.enterContext(patch.object(worker, 'telegram', return_value={'message_id': 2}))
        self.keys = self.enterContext(patch.object(core, 'rpc', return_value={'keys': ['test-key']}))
        self.translate = self.enterContext(patch.object(commands, 'translate', return_value='譯文'))
        self.message = {'chat': {'type': 'private', 'id': 123}, 'from': {'id': 123},
                        'message_id': 1, 'text': '/translate synthetic phrase'}

    def dispatch(self, request=None):
        if request is None:
            request = self.queue.put_nowait.call_args.args[0]
        self.queue.get.side_effect = [request, StopIteration]
        with self.assertRaises(StopIteration):
            worker._translation_loop()
        self.queue.task_done.assert_called_once()

    def assert_disabled_notice(self):
        self.telegram.assert_called_once_with('sendMessage', {
            'chat_id': 123, 'reply_parameters': {'message_id': 1}, 'text': commands.DISABLED})
        self.keys.assert_not_called()
        self.translate.assert_not_called()

    def test_disabled_at_admission_never_replays_original_after_reenabling(self):
        features.set_enabled('translation', False)
        self.message['photo'] = [{'file_id': 'synthetic-photo-id', 'file_size': 8}]
        worker._admit_translation(self.message)
        buffered = self.queue.put_nowait.call_args.args[0][0]
        self.assertNotIn('photo', buffered)
        self.assertEqual(buffered['text'], '/translate')
        features.set_enabled('translation', True)
        self.dispatch()
        self.assert_disabled_notice()

    def test_disabling_before_dispatch_blocks_queued_text_and_ocr(self):
        self.message['reply_to_message'] = {'photo': [{'file_id': 'synthetic', 'file_size': 8}]}
        self.message['text'] = '/translate'
        worker._admit_translation(self.message)
        features.set_enabled('translation', False)
        self.dispatch()
        self.assert_disabled_notice()

    def test_reenabled_translation_accepts_fresh_requests(self):
        features.set_enabled('translation', False)
        features.set_enabled('translation', True)
        worker._admit_translation(self.message)
        self.dispatch()
        self.translate.assert_called_once_with(['test-key'], 'synthetic phrase', None)

    def test_disabled_prompt_reply_only_keeps_routing_for_notice(self):
        features.set_enabled('translation', False)
        self.message.update(text='synthetic follow-up', reply_to_message={
            'message_id': 8, 'text': commands.PROMPT, 'from': {'is_bot': True, 'username': 'fixture_bot'}})
        with patch.object(worker, 'bot_username', return_value='fixture_bot'):
            worker._admit_translation(self.message)
        buffered = self.queue.put_nowait.call_args.args[0][0]
        self.assertEqual(buffered['text'], '')
        self.assertNotIn('reply_to_message', buffered)
        self.dispatch()
        self.assert_disabled_notice()

    def test_unaddressed_group_keeps_existing_command_scope_while_disabled(self):
        features.set_enabled('translation', False)
        self.message.update(chat={'id': -123, 'type': 'group'}, entities=[
            {'type': 'bot_command', 'offset': 0, 'length': len('/translate')}])
        self.telegram.return_value = []
        worker._admit_translation(self.message)
        self.dispatch()
        self.telegram.assert_called_once_with('getMyCommands', {
            'scope': {'type': 'chat_member', 'user_id': 123, 'chat_id': -123}})
        self.keys.assert_not_called()

    def test_overflow_rechecks_bare_command_scope_for_disabled_and_busy_notices(self):
        self.message.update(chat={'id': -123, 'type': 'group'}, entities=[
            {'type': 'bot_command', 'offset': 0, 'length': len('/translate')}])
        self.telegram.return_value = []
        self.queue.put_nowait.side_effect = Full
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.queue.reset_mock()
                self.telegram.reset_mock()
                features.set_enabled('translation', enabled)
                worker._admit_translation(self.message)
                self.assertEqual(worker._TRANSLATION_BUSY.qsize(), 1)
                self.dispatch(({}, {}, True))
                self.telegram.assert_called_once_with('getMyCommands', {
                    'scope': {'type': 'chat_member', 'user_id': 123, 'chat_id': -123}})

    def test_revoked_user_receives_no_pending_overflow_notice(self):
        self.message['chat'] = {'id': 456, 'type': 'private'}
        self.message['from'] = {'id': 456}
        access.observe(self.message['from'])
        access.decide(456, 'approve')
        self.queue.put_nowait.side_effect = Full
        worker._admit_translation(self.message)
        self.assertEqual(worker._TRANSLATION_BUSY.qsize(), 1)
        access.decide(456, 'revoke')
        self.dispatch(({}, {}, True))
        self.telegram.assert_not_called()

    def test_album_with_any_paused_member_cannot_resolve_as_complete(self):
        offset = 1
        for paused_index in (0, 1, 2):
            group = f'pause-boundary-{paused_index}'
            messages = []
            for index in range(3):
                enabled = index != paused_index
                features.set_enabled('translation', enabled)
                message = dict(self.message, message_id=offset, media_group_id=group, text='',
                               photo=[{'file_id': f'photo-{offset}', 'width': 8, 'height': 8, 'file_size': 8}])
                messages.append(message)
                inbox.store([{'update_id': offset, 'message': message}])
                offset += 1
            features.set_enabled('translation', True)
            with self.subTest(paused_index=paused_index):
                self.assertEqual(core.get_setting('telegram_offset'), offset)
                count = albums._cache.execute('''SELECT count(*) FROM translation_album_members m
                    JOIN translation_albums a ON a.id=m.album_id WHERE a.album=?''', (group,)).fetchone()[0]
                self.assertEqual(count, 0)
                for parent in messages:
                    command = dict(self.message, text='/translate', reply_to_message=parent)
                    with self.assertRaises(albums.AlbumUnavailable):
                        albums.resolve(command, 'fixture_bot')

        messages = [dict(self.message, message_id=offset + index, media_group_id='fresh-complete', text='',
                         photo=[{'file_id': f'fresh-{index}', 'width': 8, 'height': 8, 'file_size': 8}])
                    for index in range(2)]
        inbox.store([{'update_id': message['message_id'], 'message': message} for message in messages])
        command = dict(self.message, text='/translate', reply_to_message=messages[0])
        with patch.object(albums, 'QUIET_SECONDS', 0):
            pages = albums.resolve(command, 'fixture_bot')
        self.assertEqual([page[0]['file_id'] for page in pages], ['fresh-0', 'fresh-1'])
