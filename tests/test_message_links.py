"""Message parsing and durable intake with isolated control state and no network."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

_TEST_ROOT = Path(__file__).resolve().parents[1] / '.test-work'
_TEST_ROOT.mkdir(parents=True, exist_ok=True)
_BOOTSTRAP = tempfile.TemporaryDirectory(prefix='message-import-', dir=_TEST_ROOT)
with patch.dict(os.environ, {'MEDIA_BOT_DATA': _BOOTSTRAP.name}):
    from media_bot import core as c, worker

IG = 'https://instagram.com/p/ABC/'
X = 'https://x.com/example/status/123'
TT = 'https://vt.tiktok.com/ABC/'
YT = 'https://www.youtube.com/watch?v=Abc123_X-yZ'


def units(text):
    return len(text.encode('utf-16-le')) // 2


class MessageExtractionTests(unittest.TestCase):
    def urls(self, text, **extra):
        return c.extract_message_urls(dict(text=text, **extra))

    def test_share_text_punctuation_markdown_and_order(self):
        self.assertEqual(self.urls(f'下載（{IG}），再看 [{X}]；和 <{TT}>。'), [IG, X, TT])
        self.assertEqual(self.urls(f'first: ({IG}). next: [{X}](ignored)'), [IG, X])

    def test_colon_labels_and_adjacent_markdown(self):
        self.assertEqual(self.urls('link:' + IG), [IG])
        self.assertEqual(self.urls(f'[a]({IG})[b]({X})'), [IG, X])

    def test_comma_separated_links(self):
        self.assertEqual(self.urls(IG + ', ' + X + '; ' + TT), [IG, X, TT])

    def test_deduplication_after_canonicalization_preserves_order(self):
        self.assertEqual(self.urls(f'{IG} {X} {IG}#share {IG}'), [IG, X])
        self.assertEqual(self.urls(f'https://youtu.be/Abc123_X-yZ {YT}'), [YT])

    def test_supported_bare_shortlink_keeps_existing_boundary_policy(self):
        self.assertEqual(self.urls('分享 (vt.tiktok.com/ABC/).'), [TT])
        self.assertEqual(self.urls('evil.vt.tiktok.com/ABC/ //vt.tiktok.com/ABC/'), [])

    def test_invalid_links_do_not_block_valid_links(self):
        bad = ['https://example.test/file', 'https://x.com/example',
               'https://youtu.be/not-a-video-id', 'https://instagram.com:bad/p/ABC/',
               'https://user@instagram.com/p/ABC/', 'https://instagram.com:444/p/ABC/',
               'http://instagram.com/p/ABC/', 'https://[broken/path']
        self.assertEqual(self.urls(' '.join([IG, *bad, X])), [IG, X])

    def test_nested_lookalike_and_unsupported_scheme_not_promoted(self):
        for text in ('https://evil.test/?url=' + IG, 'http://evil.test/' + IG,
                     'ftp://evil.test/' + IG, 'evil.' + IG, 'user@' + IG,
                     'https://instagram.com.evil.test/p/ABC/',
                     'https://evil.test/#' + IG, 'javascript:' + IG,
                     'https://evil.test/a,' + IG, 'https://evil.test/?urls=one,' + IG):
            with self.subTest(text=text):
                self.assertEqual(self.urls(text), [])

    def test_query_and_balanced_parentheses_preserved(self):
        url = IG + '?img_index=3&source=(share)&tag=a%2Cb'
        self.assertEqual(self.urls(f'({url}).'), [url])

    def test_text_link_utf16_offsets_and_visual_order(self):
        text = '😀 ' + IG + ' then watch this and ' + X
        label = 'watch this'; start = text.index(label)
        entities = [{'type': 'text_link', 'offset': units(text[:start]),
                     'length': units(label), 'url': TT}]
        self.assertEqual(self.urls(text, entities=entities), [IG, TT, X])

    def test_url_entity_unicode_offsets_and_leading_spaces(self):
        text = '  😀 ' + IG + ' ❤️ ' + X
        entities = [{'type': 'url', 'offset': units(text[:text.index(IG)]), 'length': units(IG)}]
        self.assertEqual(self.urls(text, entities=entities), [IG, X])

    def test_entity_target_is_exact_and_label_not_a_second_request(self):
        entities = [{'type': 'text_link', 'offset': 0, 'length': units(IG), 'url': X}]
        self.assertEqual(self.urls(IG, entities=entities), [X])
        entities[0]['url'] = 'https://evil.test/'
        self.assertEqual(self.urls(IG, entities=entities), [])
        # Punctuation in a valid explicit target is data, not prose to strip.
        entities[0]['url'] = IG + '?tag=wow!'
        self.assertEqual(self.urls(IG, entities=entities), [IG + '?tag=wow!'])

    def test_malformed_entities_do_not_crash_or_hide_visible_link(self):
        for entity in (None, {}, {'type': 'url', 'offset': -1, 'length': 4},
                       {'type': 'url', 'offset': 1, 'length': 1},
                       {'type': 'url', 'offset': True, 'length': 5},
                       {'type': 'url', 'offset': 0, 'length': 9999}):
            self.assertEqual(self.urls('😀 ' + IG, entities=[entity]), [IG])
        self.assertEqual(self.urls(IG, entities='bad'), [IG])

    def test_caption_and_caption_entities(self):
        self.assertEqual(c.extract_message_urls({'caption': '😀 點這裡 ' + IG,
             'caption_entities': [{'type': 'text_link', 'offset': 3, 'length': 3, 'url': X}]}), [X, IG])

    def test_parsing_does_not_resolve_or_fetch_network(self):
        with patch.object(c, 'resolve', side_effect=AssertionError('network')), \
             patch.object(c.socket, 'getaddrinfo', side_effect=AssertionError('DNS')):
            self.assertEqual(self.urls(TT + ' and ' + IG), [TT, IG])

    def test_decorated_tiktok_share_text_and_url_entities(self):
        bare = 'vt.tiktok.com/TEST123/'
        text = '𝐓𝐢𝐤𝐓𝐨𝐤更新しました🪄︎︎◝✩\n是非いいねコメントまってましゅ！！\n\n👉🏻' + bare + '♡\n\n#sample'
        self.assertEqual(self.urls(text), ['https://' + bare])
        for length in (units(bare), units(bare + '♡')):
            entity = {'type': 'url', 'offset': units(text[:text.index(bare)]), 'length': length}
            self.assertEqual(self.urls(text, entities=[entity]), ['https://' + bare])
        self.assertEqual(c.extract_message_urls({'caption': text}), ['https://' + bare])
        self.assertEqual(c.extract_message_urls({'caption': text, 'caption_entities': [entity]}), ['https://' + bare])

    def test_shortlink_decorations_and_multiple_links(self):
        for host in ('vt', 'vm'):
            for scheme in ('', 'https://'):
                for slash in ('', '/'):
                    bare = host + '.tiktok.com/TEST123' + slash
                    for suffix in ('♡', '♥', '❤️', '🪄︎︎', '👩🏽‍💻', '✨♡'):
                        text = '👉🏻' + scheme + bare + suffix + ' ' + IG
                        with self.subTest(text=text):
                            self.assertEqual(self.urls(text), ['https://' + bare, IG])

    def test_shortlink_cleanup_preserves_url_data_and_explicit_targets(self):
        for url in (TT + '?label=♡', TT + '?label=❤️', IG + '?label=♡',
                    TT + '%E2%99%A1', TT + '名前', TT + '♡name'):
            self.assertEqual(c.extract_urls(url), [url])
        for text in ('https://evil.test/' + TT + '♡', 'user@vt.tiktok.com/TEST123/♡',
                     'vt.tiktok.com.evil/TEST123/♡', 'http://vt.tiktok.com/TEST123/♡'):
            self.assertEqual(self.urls(text), [])
        self.assertEqual(c.extract_urls(TT + '#♡'), [TT + '#♡'])
        target = TT + '♡'
        self.assertEqual(self.urls('watch', entities=[{'type': 'text_link', 'offset': 0,
                         'length': 5, 'url': target}]), [target])

    def test_no_links_or_missing_content(self):
        for message in ({}, {'text': ''}, {'text': 'hello'}, {'caption': None}):
            self.assertEqual(c.extract_message_urls(message), [])


class MessageIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='intake-', dir=_TEST_ROOT)
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(c, 'DATA', Path(self.tmp.name))
        patcher.start(); self.addCleanup(patcher.stop)
        c.initialize()
        patcher = patch.dict(os.environ, {'MEDIA_BOT_ALLOWED_USERS': '123'})
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(worker, 'telegram', return_value={'message_id': 99})
        self.send = patcher.start(); self.addCleanup(patcher.stop)

    def update(self, **content):
        return {'update_id': 100, 'message': {'message_id': 10,
                'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123}, **content}}

    def jobs(self):
        with c.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM jobs')]

    def test_prose_mixed_links_create_one_idempotent_ordered_batch(self):
        update = self.update(text=f'下載 ({IG}), https://example.test/no {X}! {IG}')
        worker.handle_update(update); worker.handle_update(update)
        jobs = self.jobs(); self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['mode'], 'batch')
        with c.db() as db:
            rows = db.execute('SELECT original_url FROM batch_items ORDER BY position').fetchall()
        self.assertEqual([row[0] for row in rows], [IG, X])

    def test_caption_hidden_links_create_batch(self):
        worker.handle_update(self.update(caption='😀 點這裡 ' + IG,
             caption_entities=[{'type': 'text_link', 'offset': 3, 'length': 3, 'url': X}]))
        self.assertEqual(self.jobs()[0]['mode'], 'batch')
        with c.db() as db:
            self.assertEqual([row[0] for row in db.execute('SELECT url FROM batch_items ORDER BY position')], [X, IG])

    def test_decorated_shortlink_enters_queue_without_heart(self):
        text = '𝐓𝐢𝐤𝐓𝐨𝐤更新しました🪄︎︎◝✩\n👉🏻vt.tiktok.com/TEST123/♡\n#sample'
        worker.handle_update(self.update(text=text))
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]['url'], jobs[0]['platform'], jobs[0]['mode']),
                         ('https://vt.tiktok.com/TEST123/', 'tiktok', 'resolve'))

    def test_duplicate_only_message_uses_single_job_and_preferences(self):
        worker.handle_update(self.update(text=f'{IG} and {IG}'))
        jobs = self.jobs(); self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]['url'], jobs[0]['mode']), (IG, 'post'))
        self.assertTrue(jobs[0]['preferences'])

    def test_invalid_only_message_reports_help_without_job(self):
        worker.handle_update(self.update(text='https://example.test/no'))
        self.assertEqual(self.jobs(), [])
        self.assertIn('HTTPS', self.send.call_args.args[1]['text'])

    def test_fifty_one_unique_links_rejected_without_partial_queue(self):
        worker.handle_update(self.update(text='please ' + ' '.join(
            f'https://instagram.com/p/POST{n}/' for n in range(51))))
        self.assertEqual(self.jobs(), [])
        self.assertIn('50', self.send.call_args.args[1]['text'])

    def test_commands_and_unapproved_chats_still_do_not_enqueue(self):
        worker.handle_update(self.update(text='/help ' + IG))
        update = self.update(text=IG); update['message']['chat']['id'] = 456
        update['message']['from']['id'] = 456
        worker.handle_update(update)
        self.assertEqual(self.jobs(), [])


if __name__ == '__main__':
    unittest.main()
