import unittest
from unittest.mock import Mock, patch
import json
import requests

from media_bot.instagram_web_client import WebClient, WebStop


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.session, self.reserve, self.block = Mock(), Mock(), Mock()
        self.client = WebClient(self.session, self.reserve, self.block, max_requests=2)

    def response(self, status, headers=None, body=b'{}'):
        result = Mock(status_code=status, headers=headers or {})
        result.iter_content.return_value = [body]
        self.session.request.return_value = result
        return result

    def test_429_sets_shared_cooldown_without_retry(self):
        response = self.response(429, {'Retry-After':'1800'})
        with self.assertRaises(WebStop):
            self.client.request(self.session, 'GET', '')
        self.block.assert_called_once_with(1800)
        self.assertEqual(self.session.request.call_count, 1)
        response.close.assert_called_once()

    def test_redirects_are_not_followed_and_body_is_not_exposed(self):
        self.response(302, {'Location':'https://example.com/?token=SECRET'})
        with self.assertRaises(WebStop) as caught:
            self.client.request(self.session, 'GET', '')
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertFalse(self.session.request.call_args.kwargs['allow_redirects'])

    def test_redirect_classification_does_not_invalidate_cookie_without_auth_evidence(self):
        cases = (
            ('/?secret=SECRET', 'home', False),
            ('/api/graphql/?secret=SECRET', 'same_origin', False),
            ('https://example.com/accounts/login/?secret=SECRET', 'external', False),
            ('/accounts/login/?next=SECRET', 'login', True),
            ('/challenge/SECRET/', 'verification', True),
            ('/checkpoint/SECRET/', 'verification', True),
            ('https://[invalid/SECRET', 'invalid', False),
        )
        for location, redirect, authentication in cases:
            with self.subTest(redirect=redirect):
                self.client.count = 0
                response = self.response(302, {'Location': location}, b'SECRET')
                with self.assertRaises(WebStop) as caught:
                    self.client.request(self.session, 'POST', 'graphql/query')
                error = caught.exception
                self.assertEqual('AuthRequired' in str(error), authentication)
                self.assertEqual(error.diagnostic, {'endpoint': 'graphql_query', 'status': 302,
                    'redirect': redirect, 'kind': 'auth_required' if authentication else 'upstream_redirect'})
                self.assertNotIn('SECRET', str(error))
                self.assertNotIn('SECRET', json.dumps(error.diagnostic))
                response.iter_content.assert_not_called()
                response.close.assert_called_once()
        self.block.assert_not_called()
        self.assertEqual(self.session.request.call_count, len(cases))

    def test_http_auth_failure_records_endpoint_and_status(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.response(status, body=b'SECRET')
                with self.assertRaisesRegex(WebStop, 'AuthRequired') as caught:
                    self.client.request(self.session, 'POST', 'api/graphql')
                self.assertEqual(caught.exception.diagnostic,
                    {'endpoint': 'web_graphql', 'status': status, 'kind': 'auth_required'})
                self.assertNotIn('SECRET', str(caught.exception))
        self.block.assert_not_called()

    def test_request_budget_applies_before_network(self):
        for _ in range(2):
            self.response(200)
            self.client.request(self.session, 'GET', '')
        with self.assertRaises(WebStop):
            self.client.request(self.session, 'GET', '')
        self.assertEqual(self.reserve.call_count, 2)

    def test_missing_tokens_fail_without_leaking_homepage(self):
        self.response(200, body=b'SECRET')
        with self.assertRaises(WebStop) as caught:
            self.client.web_tokens()
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertIsNone(self.client.tokens)

    def test_web_endpoint_posts_current_session_tokens_without_legacy_fallback(self):
        html = ('"DTSGInitialData",[],{"token":"fixture-dtsg"}'
                '"LSD",[],{"token":"fixture-lsd"}'
                '"IG_USER_EIMU":"123"')
        with requests.Session() as session:
            session.cookies.update({'sessionid': 'fixture-session', 'csrftoken': 'fixture-csrf'})
            client = WebClient(session, Mock(), Mock())
            calls = []
            def request(actual_session, method, path, **kwargs):
                calls.append((method, path))
                if method == 'GET':
                    return html
                self.assertEqual(actual_session.headers['x-fb-lsd'], 'fixture-lsd')
                self.assertEqual(actual_session.headers['x-csrftoken'], 'fixture-csrf')
                self.assertEqual(actual_session.cookies.get('sessionid'), 'fixture-session')
                self.assertEqual(kwargs['data']['fb_dtsg'], 'fixture-dtsg')
                self.assertEqual(kwargs['data']['lsd'], 'fixture-lsd')
                self.assertEqual(kwargs['data']['av'], '123')
                self.assertEqual(kwargs['data']['doc_id'], '27128499623469141')
                return '{"data":{"items":[]}}'
            with patch.object(client, 'request', side_effect=request):
                client.doc_id_graphql_query('27128499623469141', {'shortcode': 'ABC'}, web_api=True)
            self.assertEqual(calls, [('GET', ''), ('POST', 'api/graphql')])

    def test_host_is_fixed_and_path_allowlisted(self):
        with self.assertRaises(WebStop):
            self.client.request(self.session, 'GET', 'https://example.com/')
        self.session.request.assert_not_called()

    def test_http_200_restrictions_stop_and_apply_cooldown(self):
        for payload in ({'message':'Please wait a few minutes before you try again. SECRET'},
                        {'errors':[{'message':'Too many requests SECRET'}]},
                        {'message':'feedback_required'}):
            with self.subTest(payload=payload), requests.Session() as session:
                block=Mock();client=WebClient(session,Mock(),block)
                with patch.object(client,'request',return_value=json.dumps(payload)):
                    with self.assertRaises(WebStop) as caught:
                        client.doc_id_graphql_query('1',{},web_api=False)
                block.assert_called_once_with(900)
                self.assertNotIn('SECRET',str(caught.exception))

    def test_caption_text_is_not_treated_as_rate_limit(self):
        payload={'data':{'caption':'too many requests'}}
        with requests.Session() as session:
            block=Mock();client=WebClient(session,Mock(),block)
            with patch.object(client,'request',return_value=json.dumps(payload)):
                self.assertEqual(client.doc_id_graphql_query('1',{},web_api=False),payload)
            block.assert_not_called()

    def test_authentication_challenge_pauses_instead_of_timed_retry(self):
        for payload in ({'message':'login_required'}, {'challenge':{'url':'SECRET'}},
                        {'errors':[{'message':'checkpoint_required SECRET'}]}):
            with self.subTest(payload=payload), requests.Session() as session:
                block=Mock();client=WebClient(session,Mock(),block)
                with patch.object(client,'request',return_value=json.dumps(payload)):
                    with self.assertRaisesRegex(WebStop,'AuthRequired') as caught:
                        client.doc_id_graphql_query('1',{},web_api=False)
                self.assertNotIn('SECRET',str(caught.exception))
                block.assert_not_called()


if __name__ == '__main__':
    unittest.main()
