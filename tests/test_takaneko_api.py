import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from takaneko_telegram import takaneko_api as api


class ClientTests(unittest.TestCase):
    def client(self):
        return api.Client({'accessToken': 'fixture'})

    def feed(self, path):
        parsed = urlsplit(path)
        if parsed.path == '/auth/notifications/count':
            return {'count': 3}
        if parsed.path == '/auth/notifications':
            offset = int(parse_qs(parsed.query)['offset'][0])
            return [{'notificationReservationId': str(i), 'displayDate': 1700000000000} for i in range(offset, min(offset + 2, 3))]
        field = {'/blog/queries/getArticleList': 'articleList', '/gallery/queries/getGalleryAlbumList': 'galleryAlbumList',
                 '/movie/queries/getMovieList': 'movieList'}[parsed.path]
        return {field: [{'id': 'one'}], 'totalPages': 1}

    def test_all_feeds_short_notification_pages_and_namespace(self):
        c = self.client()
        c._get = Mock(side_effect=self.feed)
        rows = c.list_posts()
        self.assertEqual([r['id'] for r in rows], ['notification:0', 'notification:1', 'notification:2', 'blog:one', 'gallery:one', 'movie:one'])
        self.assertEqual(rows[0]['published'], 1700000000)
        self.assertIn('offset=3', c._get.call_args_list[3].args[0])

    def test_unfiltered_notification_pages_of_50_count_system_notices_in_the_offset(self):
        c = self.client()
        notice = {'notificationId': 'billing', 'notificationType': 'subscriptionPaymentFailure'}
        messages = [{'notificationReservationId': str(i)} for i in range(50)]
        c._get = Mock(side_effect=[{'count': 51}, [notice, *messages[:49]], messages[49:],
                                  [], {'count': 51}, {'articleList': [], 'totalPages': 0},
                                  {'galleryAlbumList': [], 'totalPages': 0}, {'movieList': [], 'totalPages': 0}])
        self.assertEqual([row['id'] for row in c.list_posts()], [f'notification:{i}' for i in range(50)])
        requests = [urlsplit(call.args[0]) for call in c._get.call_args_list]
        self.assertEqual([url.query for url in requests if url.path == '/auth/notifications/count'], ['', ''])
        self.assertEqual(
            [parse_qs(url.query, keep_blank_values=True) for url in requests if url.path == '/auth/notifications'],
            [{'offset': [str(offset)], 'limit': ['50'], 'orderType': ['2'], 'readType': ['all']}
             for offset in (0, 50, 51)],
        )

    def test_repeated_system_notices_and_malformed_messages_fail(self):
        c = self.client()
        notice = {'notificationId': 'billing', 'notificationType': 'subscriptionPaymentFailure'}
        c._get = Mock(side_effect=[{'count': 2}, [notice], [notice]])
        with self.assertRaisesRegex(api.FanclubError, 'repeated'):
            c.list_posts()
        c._get = Mock(side_effect=[{'count': 1}, [{'notificationId': 'broken', 'notificationType': 'message'}]])
        with self.assertRaises(api.FanclubError):
            c.list_posts()

    def test_empty_feeds_are_explicit(self):
        c = self.client()
        c._get = Mock(side_effect=[{'count': 0}, [], {'count': 0}, {'articleList': [], 'totalPages': 0},
                                  {'galleryAlbumList': [], 'totalPages': 0}, {'movieList': [], 'totalPages': 0}])
        self.assertEqual(c.list_posts(), [])

    def test_no_partial_baseline_on_unauthorized_feed(self):
        c = self.client()
        def get(path):
            if path.startswith('/movie/'):
                raise api.FanclubError('Fanclub HTTP 403')
            return self.feed(path)
        c._get = Mock(side_effect=get)
        with self.assertRaises(api.FanclubError):
            c.list_posts()

    def test_repeated_page_is_not_complete(self):
        c = self.client()
        c._get = Mock(side_effect=[{'count': 2}, [{'notificationReservationId': 'x'}], [{'notificationReservationId': 'x'}]])
        with self.assertRaisesRegex(api.FanclubError, 'repeated'):
            c.list_posts()

    def test_truncated_notifications_and_changed_count_fail(self):
        for replies in [[{'count': 1}, []], [{'count': 1}, [{'notificationReservationId': 'x'}], [], {'count': 2}]]:
            c = self.client()
            c._get = Mock(side_effect=replies)
            with self.assertRaises(api.FanclubError):
                c.list_posts()

    def test_pagination_metadata_required(self):
        c = self.client()
        c._get = Mock(side_effect=[{'count': 0}, [], {'count': 0}, {'articleList': []}])
        with self.assertRaises(api.FanclubError):
            c.list_posts()

    def test_blog_pages_drift_or_duplicate_fail(self):
        for second in [{'totalPages': 3, 'articleList': [{'id': '2'}]}, {'totalPages': 2, 'articleList': [{'id': '1'}]}]:
            c = self.client()
            c._get = Mock(side_effect=[{'count': 0}, [], {'count': 0}, {'totalPages': 2, 'articleList': [{'id': '1'}]}, second])
            with self.assertRaises(api.FanclubError):
                c.list_posts()

    def test_raw_html_order_and_unlisted_video_hash_retained(self):
        c = self.client()
        raw = '<p>A<img src="/one.jpg">B<iframe src="https://player.vimeo.com/video/123?h=abc"></iframe>C</p>'
        c._get = Mock(return_value={'body02': '<p>last</p>', 'body01': raw, 'image01': '/cover.jpg'})
        detail = c.detail({'id': 'notification:test'})
        self.assertEqual(detail['html'], '<div class="body-section"><img src="https://takanekofc.com/cover.jpg" /></div><div class="body-section body-wysiwyg">' + raw + '</div><div class="body-section body-wysiwyg"><p>last</p></div>')
        self.assertEqual(detail['attachments'], [])
        self.assertEqual(detail['source_fields']['body01'], raw)
        self.assertEqual(detail['url'], 'https://takanekofc.com/notification/test')

    def test_http200_permission_denied_never_becomes_empty_article(self):
        c=self.client()
        c._get=Mock(return_value={'id':'denied','title':'title','isPermissionDenied':True})
        for kind in ('notification','blog','gallery','movie'):
            with self.assertRaises(api.FanclubError) as raised:
                c.detail({'id':kind+':denied'})
            self.assertEqual(raised.exception.status,403)
            self.assertEqual(str(raised.exception),'Fanclub content permission denied')

    def test_gallery_array_order_preserved(self):
        c = self.client()
        c._get = Mock(return_value={'galleryAlbumItems': [{'file': '/z.jpg', 'displayOrder': 1}, {'file': '/a.jpg', 'displayOrder': 1}]})
        self.assertEqual([a['url'] for a in c.detail({'id': 'gallery:1'})['attachments']], [api.SITE + 'z.jpg', api.SITE + 'a.jpg'])

    def test_movie_sources_preserve_hash(self):
        c = self.client()
        for kind, video, expected in [('video', '123/abcd', 'https://player.vimeo.com/video/123?h=abcd'),
                                       ('vimeo', '123?h=abcd', 'https://player.vimeo.com/video/123?h=abcd'),
                                       ('youtube', 'AbC_dE-1234', 'https://www.youtube.com/watch?v=AbC_dE-1234')]:
            c._get = Mock(return_value={'movieType': kind, 'videoId': video})
            self.assertEqual(c.detail({'id': 'movie:1'})['attachments'][0]['url'], expected)

    def test_refresh_rotation_in_memory_only(self):
        original = {'refreshToken': 'old', 'cookies': []}
        c = api.Client(original)
        c._request = Mock(return_value={'accessToken': 'access', 'refreshToken': 'rotated'})
        self.assertEqual(c._authenticate(), 'access')
        self.assertEqual(c.session_snapshot['refreshToken'], 'rotated')
        self.assertEqual(original['refreshToken'], 'old')
        self.assertEqual(c._authenticate(), 'access')
        c._request.assert_called_once()

    def test_cookie_header_scope_and_injection(self):
        c = api.Client({'cookies': [{'domain': '.takanekofc.com', 'path': '/auth', 'name': 'session', 'value': 'fixture'},
                                    {'domain': 'evil.test', 'path': '/', 'name': 'bad', 'value': 'secret'}]})
        self.assertEqual(c._cookie('/auth/refresh'), 'session=fixture')
        self.assertEqual(c._cookie('/blog/queries'), '')
        c._session['cookies'][0]['value'] = 'bad\r\nheader'
        with self.assertRaises(api.FanclubError):
            c._cookie('/auth/refresh')

    def test_unsafe_media_urls_rejected(self):
        for url in ['https://127.0.0.1/a', 'https://takanekofc.com.evil.test/a', 'https://u:p@takanekofc.com/a',
                    'http://takanekofc.com/a', 'https://takanekofc.com:444/a', 'https://takanekofc.com/a\n']:
            with self.subTest(url=url), self.assertRaises(api.FanclubError):
                api.media_url(url)

    def test_private_dns_rejected(self):
        with patch.object(api.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(api.FanclubError):
                api._public_connection('takanekofc.com')

    def test_api_redirect_is_never_followed(self):
        response = Mock(status=302)
        conn = Mock()
        conn.getresponse.return_value = response
        with patch.object(api, '_public_connection', return_value=conn):
            with self.assertRaisesRegex(api.FanclubError, 'HTTP 302'):
                self.client()._get('/auth/notifications/count')
        response.read.assert_not_called()
        conn.close.assert_called_once()

    def test_download_redirect_validation_and_no_auth(self):
        response = Mock(status=302)
        response.getheader.return_value = 'https://evil.test/file'
        conn = Mock()
        conn.getresponse.return_value = response
        with tempfile.TemporaryDirectory() as directory, patch.object(api, '_public_connection', return_value=conn):
            with self.assertRaisesRegex(api.FanclubError, 'not allowed'):
                self.client().download(api.SITE + 'file', Path(directory) / 'photo.jpg', Mock())
        headers = conn.request.call_args.kwargs['headers']
        self.assertNotIn('Authorization', headers)
        self.assertNotIn('Cookie', headers)

    def test_stream_download_and_guard(self):
        response = Mock(status=200)
        response.getheader.side_effect = lambda key: {'Content-Length': '3', 'Content-Type': 'image/jpeg'}.get(key)
        response.read.side_effect = [b'abc', b'']
        conn = Mock()
        conn.getresponse.return_value = response
        guard = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(api, '_public_connection', return_value=conn):
            target = Path(directory) / 'photo.jpg'
            result = self.client().download(api.SITE + 'photo.jpg', target, guard)
            self.assertEqual(target.read_bytes(), b'abc')
            self.assertEqual(result['size'], 3)
            self.assertGreaterEqual(guard.call_count, 3)

    def test_interrupted_original_is_preserved(self):
        response = Mock(status=200)
        response.getheader.side_effect = lambda key: {'Content-Type': 'image/jpeg'}.get(key)
        response.read.side_effect = [b'abc', OSError('private URL with secret')]
        conn = Mock()
        conn.getresponse.return_value = response
        with tempfile.TemporaryDirectory() as directory, patch.object(api, '_public_connection', return_value=conn):
            target = Path(directory) / 'photo.jpg'
            with self.assertRaisesRegex(api.FanclubError, '^Media download failed$'):
                self.client().download(api.SITE + 'photo.jpg', target, Mock())
            self.assertFalse(target.exists())
            self.assertEqual(next(Path(directory).glob('photo.jpg.*.part')).read_bytes(), b'abc')

    def test_canonical_video_urls(self):
        fixtures = {
            'https://player.vimeo.com/video/123?h=abc&autoplay=1': 'https://player.vimeo.com/video/123?h=abc',
            'https://vimeo.com/123/abc': 'https://player.vimeo.com/video/123?h=abc',
            'https://www.youtube-nocookie.com/embed/AbC_dE-1234?autoplay=1': 'https://www.youtube.com/watch?v=AbC_dE-1234',
            'https://youtu.be/AbC_dE-1234': 'https://www.youtube.com/watch?v=AbC_dE-1234',
            'https://www.youtube.com/watch?v=AbC_dE-1234&list=ignored': 'https://www.youtube.com/watch?v=AbC_dE-1234',
        }
        for original, canonical in fixtures.items():
            self.assertEqual(api.canonical_video_url(original), canonical)

    def test_video_provider_paths_and_credentials_rejected(self):
        for url in ['https://youtube.com/redirect?url=https://evil.test',
                    'https://youtube.com/playlist?list=123', 'https://youtube.com/watch?v=AbC_dE-1234&v=AbC_dE-1234',
                    'https://vimeo.com/channels/123', 'https://player.vimeo.com/video/123?h=abc&h=def',
                    'https://u:p@player.vimeo.com/video/123', 'https://youtube.com.evil.test/watch?v=AbC_dE-1234',
                    'http://www.youtube.com/watch?v=AbC_dE-1234', 'https://youtube.com:444/watch?v=AbC_dE-1234',
                    'https://www.youtube-nocookie.com/watch?v=AbC_dE-1234']:
            with self.subTest(url=url), self.assertRaises(api.FanclubError):
                api.canonical_video_url(url)

    def test_refresh400_validates_saved_access_token(self):
        c = api.Client({'refreshToken': 'bad', 'accessToken': 'saved'})
        c._request = Mock(side_effect=[api.FanclubError('Fanclub HTTP 400', status=400), {'count': 0}])
        self.assertEqual(c._authenticate(), 'saved')
        self.assertEqual(c._request.call_args.args, ('/auth/notifications/count',))
        self.assertEqual(c._request.call_args.kwargs, {'token': 'saved'})

    def test_refresh_transient_never_falls_back_and_bad_saved_token_fails(self):
        for status in (429, 500, None):
            c = api.Client({'refreshToken': 'bad', 'accessToken': 'saved'})
            c._request = Mock(side_effect=api.FanclubError('request failed', status=status))
            with self.assertRaises(api.FanclubError):
                c._authenticate()
            c._request.assert_called_once()
        c = api.Client({'refreshToken': 'bad', 'accessToken': 'expired'})
        c._request = Mock(side_effect=[api.FanclubError('Fanclub HTTP 400', status=400), api.FanclubError('Fanclub HTTP 401', status=401)])
        with self.assertRaises(api.FanclubError):
            c._authenticate()
        self.assertIsNone(c._token)

    def test_source_timestamps_never_use_local_timezone(self):
        self.assertEqual(api._epoch('2026-11-03T09:00:00+09:00'), api._epoch('2026-11-03T00:00:00Z'))
        for invalid in ('nan', 'inf', '-1', '2026-11-03T09:00:00'):
            self.assertEqual(api._epoch(invalid), 0)

    def test_listing_time_budget_fails_closed(self):
        c = self.client()
        c._get = Mock(side_effect=self.feed)
        with patch.object(api.time, 'monotonic', side_effect=[0, 241]):
            with self.assertRaisesRegex(api.FanclubError, 'time limit'):
                c.list_posts()

    def test_official_notification_layout_video_image_body_pairs(self):
        c = self.client()
        c._get = Mock(return_value={'videoId': '123/abc', 'image02': '/second.jpg', 'body02': '<p>second</p>',
                                   'image01': '/first.jpg', 'body01': '<p>first</p>'})
        detail = c.detail({'id': 'notification:one'})
        markers = ['video/123?h=abc', 'first.jpg', '<p>first</p>', 'second.jpg', '<p>second</p>']
        positions = [detail['html'].index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(detail['attachments'], [])
        self.assertEqual(detail['source_fields']['videoId'], '123/abc')


if __name__ == '__main__':
    unittest.main()
