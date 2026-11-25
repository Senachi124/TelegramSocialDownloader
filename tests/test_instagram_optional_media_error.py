"""Partial GraphQL responses must not discard usable posts or hide real errors."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TEST_ROOT = Path(__file__).resolve().parents[1] / '.test-work'
_TEST_ROOT.mkdir(exist_ok=True)
_BOOTSTRAP = tempfile.TemporaryDirectory(prefix='optional-media-import-', dir=_TEST_ROOT)
with patch.dict(os.environ, {'MEDIA_BOT_DATA': _BOOTSTRAP.name}):
    from media_bot import instagram_web_gallery as bridge, core as c
from gallery_dl import exception
from gallery_dl.extractor.instagram import InstagramAPI


FIELD = 'ai_interactive_embodiment_attachment_style_info'
ROOT = 'xdt_api__v1__media__shortcode__web_info'


def error(path):
    return {'message': 'A server error field_exception occured. Check server logs for details.',
            'severity': 'ERROR', 'path': path}


def image(pk='123'):
    return {'pk': pk, 'code': 'fixture', 'media_type': 1,
            'image_versions2': {'candidates': [{'url': 'https://cdn.example/image.jpg'}]}}


def payload(item=None):
    return {'data': {ROOT: {'items': [image() if item is None else item]}}, 'errors': [
        error(['_xdt_api__v1__media__shortcode__web_info_items', 0, 'node', FIELD]),
        error([ROOT, 'items', 0, FIELD])]}


class OptionalPostErrorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data = patch.object(c, 'DATA', Path(temp.name))
        data.start(); self.addCleanup(data.stop)
        self.result = {}
        self.addCleanup(bridge.install_web_graphql(self.result))
        extractor = Mock()
        extractor.config.return_value = 'memory'
        extractor.exc = exception
        self.api = InstagramAPI(extractor)
        self.client = self.api._bot_web_client = Mock()

    def extract(self, response):
        self.client.doc_id_graphql_query.return_value = response
        return list(self.api.media('fixture'))

    def rejects(self, response):
        with self.assertRaises(exception.AbortExtraction):
            self.extract(response)

    def test_optional_decorations_preserve_complete_image_payload(self):
        response = payload()
        before = copy.deepcopy(response)
        result = self.extract(response)
        self.assertEqual(result, before['data'][ROOT]['items'])
        self.assertEqual(response, before)
        self.client.doc_id_graphql_query.assert_called_once()
        self.api.extractor.log.info.assert_called_once()

    def test_both_individual_path_representations_are_supported(self):
        for index in (0, 1):
            response = payload(); response['errors'] = [response['errors'][index]]
            with self.subTest(index=index):
                self.assertEqual(len(self.extract(response)), 1)

    def test_complete_mixed_carousel_keeps_every_child(self):
        video = {'pk': '125', 'media_type': 2,
                 'video_versions': [{'url': 'https://cdn.example/video.mp4'}]}
        item = {'pk': '123', 'code': 'fixture', 'media_type': 8,
                'carousel_media_count': 2, 'carousel_media': [image('124'), video]}
        self.assertEqual(self.extract(payload(item)), [item])

    def test_unknown_or_critical_error_mixed_with_optional_error_aborts(self):
        for path in ([ROOT, 'items', 0, 'image_versions2', 'candidates'],
                     [ROOT, 'items', 0, 'video_versions'],
                     [ROOT, 'items', 0, 'carousel_media'],
                     [ROOT, 'items', 0, 'user'], ['unknown', FIELD],
                     [ROOT, 'items', 0, FIELD, 'nested'], []):
            response = payload(); response['errors'].append(error(path))
            with self.subTest(path=path): self.rejects(response)

    def test_invalid_index_is_never_optional(self):
        for index in (-1, 1, True, False, 0.0, '0', None):
            for path in ([ROOT, 'items', index, FIELD],
                         ['_xdt_api__v1__media__shortcode__web_info_items', index, 'node', FIELD]):
                response = payload(); response['errors'] = [error(path)]
                with self.subTest(path=path): self.rejects(response)

    def test_error_shape_and_message_must_match_known_field_failure(self):
        for bad in (None, 'error', {}, {'path': [ROOT, 'items', 0, FIELD]},
                    {**error([ROOT, 'items', 0, FIELD]), 'message': 'login_required'},
                    {**error([ROOT, 'items', 0, FIELD]), 'message': 'feedback_required'},
                    {**error([ROOT, 'items', 0, FIELD]), 'severity': 'CRITICAL'}):
            response = payload(); response['errors'] = [bad]
            with self.subTest(error=bad): self.rejects(response)
        response = payload(); response['errors'] = {'path': [ROOT, 'items', 0, FIELD]}
        self.rejects(response)

    def test_partial_response_must_identify_requested_post(self):
        for key, value in (('code', 'other'), ('code', None), ('pk', None), ('pk', 'bad'),
                           ('media_type', None), ('media_type', True), ('media_type', 99)):
            item = image(); item[key] = value
            with self.subTest(key=key, value=value): self.rejects(payload(item))
        response = payload(); response['status'] = 'fail'; self.rejects(response)

    def test_empty_multiple_or_malformed_items_are_not_success(self):
        for items in ([], None, {}, [None], [image(), image('456')]):
            response = payload(); response['data'][ROOT]['items'] = items
            with self.subTest(items=items): self.rejects(response)

    def test_missing_download_variants_do_not_pass_optional_error_gate(self):
        for variants in (None, {}, [], [None], [{}], [{'url': ''}]):
            item = image(); item['image_versions2']['candidates'] = variants
            with self.subTest(variants=variants): self.rejects(payload(item))
        item = image(); item['media_type'] = 2
        self.rejects(payload(item))

    def test_incomplete_carousel_does_not_pass_optional_error_gate(self):
        good = {'pk': '123', 'code': 'fixture', 'media_type': 8,
                'carousel_media_count': 2, 'carousel_media': [image('124'), image('125')]}
        for change in ({'carousel_media_count': 3}, {'carousel_media_count': None},
                       {'carousel_media': [image('124'), image('124')]},
                       {'carousel_media': [image('124'), None]}, {'carousel_media': []}):
            with self.subTest(change=change): self.rejects(payload({**good, **change}))

    def test_existing_no_error_extraction_remains_unchanged(self):
        response = {'data': {ROOT: {'items': [{'pk': '123'}]}}}
        self.assertEqual(self.extract(response), [{'pk': '123'}])
        self.api.extractor.log.info.assert_not_called()

    def test_transport_authentication_and_rate_stops_still_abort(self):
        for kind, message in (('auth_required', 'AuthRequired: session verification required'),
                              ('rate_limited', 'Instagram restriction; cooldown applied')):
            error = bridge.WebStop(message, endpoint='web_graphql', status=200, kind=kind)
            self.client.doc_id_graphql_query.side_effect = error
            with self.subTest(kind=kind), self.assertRaises(exception.AbortExtraction):
                list(self.api.media('fixture'))
            self.assertEqual(self.result['instagram_diagnostic']['kind'], kind)
        self.assertEqual(self.client.doc_id_graphql_query.call_count, 2)


if __name__ == '__main__':
    unittest.main()
