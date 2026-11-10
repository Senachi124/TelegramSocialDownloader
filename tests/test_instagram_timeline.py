import unittest
from unittest.mock import Mock

from media_bot import instagram_web_timeline as timeline


def payload(ids, cursor=None):
    return {'data': {timeline.CONNECTION: {'edges': [{'node': {'pk': str(i), 'code': f'post{i}'}} for i in ids],
        'page_info': {'has_next_page': cursor is not None, 'end_cursor': cursor}}}}


class TimelineTests(unittest.TestCase):
    def test_username_scan_and_cursor_without_profile_lookup(self):
        context = Mock()
        context.doc_id_graphql_query.side_effect = [payload([1, 2], 'next'), payload([2, 3])]
        pages = list(timeline.pages(context, 'someone'))
        self.assertEqual([len(p['nodes']) for p in pages], [2, 1])
        self.assertTrue(pages[-1]['complete'])
        args, kwargs = context.doc_id_graphql_query.call_args
        self.assertEqual(args[1]['after'], 'next')
        self.assertEqual(args[1]['username'], 'someone')
        self.assertTrue(kwargs['web_api'])
        context.get_json.assert_not_called()

    def test_repeated_cursor_rejected_before_duplicate_page_commit(self):
        context = Mock()
        context.doc_id_graphql_query.side_effect = [payload([1], 'same'), payload([2], 'same')]
        pages = timeline.pages(context, 'someone')
        next(pages)
        with self.assertRaisesRegex(timeline.TimelineError, 'repeated_cursor'):
            next(pages)

    def test_repeated_page_rejected_even_with_new_cursor(self):
        context = Mock()
        context.doc_id_graphql_query.side_effect = [payload([1], 'one'), payload([1], 'two')]
        with self.assertRaisesRegex(timeline.TimelineError, 'repeated_page'):
            list(timeline.pages(context, 'someone'))

    def test_graphql_200_error_and_missing_data_are_not_success(self):
        for value in ({'errors': [{'message': 'PRIVATE'}]}, {'data': {}}, payload([],'next')):
            with self.assertRaises(timeline.TimelineError) as caught:
                timeline.parse_page(value)
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_empty_first_page_is_not_verified_empty_account(self):
        context = Mock()
        context.doc_id_graphql_query.return_value = payload([])
        with self.assertRaisesRegex(timeline.TimelineError, 'empty_first_page'):
            list(timeline.pages(context, 'someone'))

    def test_only_location_avatar_error_is_optional(self):
        value = payload([1])
        value['errors'] = [{'path': [timeline.CONNECTION, 'edges', 0, 'node', 'location', 'profile_pic_url']}]
        self.assertEqual(len(timeline.parse_page(value)[0]), 1)
        for field in ('video_versions', 'carousel_media', 'pk', 'user'):
            value['errors'][0]['path'] = [timeline.CONNECTION, 'edges', 0, 'node', field]
            with self.assertRaises(timeline.TimelineError):
                timeline.parse_page(value)

    def test_page_budget_is_not_reported_as_complete(self):
        context = Mock()
        context.doc_id_graphql_query.return_value = payload([1], 'next')
        pages = list(timeline.pages(context, 'someone', max_pages=1))
        self.assertEqual(context.doc_id_graphql_query.call_count, 1)
        self.assertFalse(pages[0]['complete'])

    def test_invalid_target_does_not_send_request(self):
        context = Mock()
        with self.assertRaises(timeline.TimelineError):
            list(timeline.pages(context, '../login'))
        context.doc_id_graphql_query.assert_not_called()

    def test_raw_carousel_ids_survive_upstream_normalizer_omission(self):
        node = {'edge_sidecar_to_children': {'edges': [{'node': {'is_video': False}}]},
            'carousel_media': [{'pk': '11', 'media_type': 1,
                'image_versions2': {'candidates': [{'url': 'https://media.cdninstagram.com/image', 'width': 10, 'height': 10}]}}]}
        tasks = timeline.media_tasks(node, ['11'])
        self.assertEqual(tasks[0][:2], ('11', 'image'))
        with self.assertRaises(timeline.TimelineError):
            timeline.media_tasks(node, ['12'])


if __name__ == '__main__':
    unittest.main()
