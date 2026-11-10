"""Web GraphQL transport independent of Instaloader's versioned query helpers."""
import copy
import json
import re
from contextlib import closing
from urllib.parse import urljoin, urlsplit
import requests


class WebStop(RuntimeError):
    def __init__(self, message, *, endpoint=None, status=None, redirect=None, kind=None):
        # Only fixed classifications cross the subprocess boundary. Never retain
        # Location, request headers, response bodies or session values.
        self.diagnostic = {}
        if endpoint in ('home', 'web_graphql', 'graphql_query'):
            self.diagnostic['endpoint'] = endpoint
        if type(status) is int and 100 <= status <= 599:
            self.diagnostic['status'] = status
        if redirect in ('login', 'verification', 'home', 'same_origin', 'external', 'invalid'):
            self.diagnostic['redirect'] = redirect
        if kind in ('auth_required', 'rate_limited', 'upstream_redirect', 'upstream_http_error'):
            self.diagnostic['kind'] = kind
        suffix = ' '.join(f'{key}={value}' for key, value in self.diagnostic.items())
        super().__init__(message + (' [' + suffix + ']' if suffix else ''))


def redirect_kind(location):
    """Classify a Location without returning any part of its untrusted value."""
    if not isinstance(location, str) or not location or any(ord(c) < 32 for c in location):
        return 'invalid'
    try:
        target = urlsplit(urljoin('https://www.instagram.com/', location))
        if (target.scheme != 'https' or target.hostname != 'www.instagram.com'
                or target.port not in (None, 443) or target.username or target.password):
            return 'external'
        if target.path.rstrip('/') == '/accounts/login':
            return 'login'
        if target.path.startswith(('/challenge/', '/checkpoint/')):
            return 'verification'
        return 'home' if target.path in ('', '/') else 'same_origin'
    except ValueError:
        return 'invalid'


class WebClient:
    def __init__(self, session, reserve, block, max_requests=120):
        self.session, self.reserve, self.block = session, reserve, block
        self.session.trust_env = False
        self.max_requests = max_requests
        self.count = 0
        self.tokens = None

    def request(self, session, method, path, **kwargs):
        if path not in ('', 'api/graphql', 'graphql/query'):
            raise WebStop('unexpected_endpoint')
        if self.count >= self.max_requests:
            raise WebStop('scan_request_budget_reached')
        self.reserve()
        self.count += 1
        endpoint = {'': 'home', 'api/graphql': 'web_graphql', 'graphql/query': 'graphql_query'}[path]
        try:
            with closing(session.request(method, 'https://www.instagram.com/'+path,
                    allow_redirects=False, timeout=(10, 30), stream=True, **kwargs)) as response:
                status = response.status_code
                retry = response.headers.get('Retry-After', '')
                if status == 429:
                    self.block(max(900, int(retry) if re.fullmatch(r'\d{1,9}', retry) else 900))
                    raise WebStop('HttpError: 429 Too Many Requests; stopped without fallback',
                                  endpoint=endpoint, status=status, kind='rate_limited')
                if status in (401, 403):
                    raise WebStop('AuthRequired: Instagram rejected the request; no fallback',
                                  endpoint=endpoint, status=status, kind='auth_required')
                if 300 <= status < 400:
                    redirect = redirect_kind(response.headers.get('Location'))
                    authentication = redirect in ('login', 'verification')
                    raise WebStop('AuthRequired: Instagram session verification required; no fallback'
                                  if authentication else 'upstream_redirect; stopped without fallback',
                                  endpoint=endpoint, status=status, redirect=redirect,
                                  kind='auth_required' if authentication else 'upstream_redirect')
                if status >= 400:
                    self.block(900)
                    raise WebStop('upstream_http_error; cooldown applied',
                                  endpoint=endpoint, status=status, kind='upstream_http_error')
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise WebStop('response_size_budget')
                    chunks.append(chunk)
                return b''.join(chunks).decode('utf-8')
        except requests.RequestException:
            raise WebStop('transport_error; stopped without retry') from None

    def web_tokens(self):
        if self.tokens is None:
            html = self.request(self.session, 'GET', '')
            self.tokens = {}
            for name, pattern in (
                ('fb_dtsg', r'"DTSGInitialData",\[\],\{[^{}]*"token":"([^"]+)"'),
                ('lsd', r'"LSD",\[\],\{[^{}]*"token":"([^"]+)"'),
                ('av', r'"IG_USER_EIMU":"?(\d+)'),
            ):
                match = re.search(pattern, html)
                if not match:
                    self.tokens = None
                    raise WebStop('missing_web_session_token')
                self.tokens[name] = match.group(1)
        return dict(self.tokens)

    def doc_id_graphql_query(self, doc_id, variables, referer=None, web_api=True):
        data = {'doc_id': doc_id, 'variables': json.dumps(variables, separators=(',', ':')),
                'server_timestamps': 'true'}
        if web_api:
            data.update(self.web_tokens())
        with requests.Session() as session:
            session.trust_env = False
            for cookie in self.session.cookies:
                session.cookies.set_cookie(copy.copy(cookie))
            session.headers.update(self.session.headers)
            for name in ('Host','Origin','X-Instagram-AJAX','X-Requested-With','Connection','Content-Length'):
                session.headers.pop(name, None)
            session.headers.update({'accept':'*/*','authority':'www.instagram.com','scheme':'https',
                'referer':referer or 'https://www.instagram.com/',
                'x-csrftoken':self.session.cookies.get_dict().get('csrftoken','')})
            if web_api:
                session.headers.update({'x-fb-lsd':data['lsd'], 'sec-fetch-dest':'empty',
                    'sec-fetch-mode':'cors', 'sec-fetch-site':'same-origin'})
            raw = self.request(session, 'POST', 'api/graphql' if web_api else 'graphql/query', data=data)
        try:
            payload = json.loads(raw)
        except ValueError:
            raise WebStop('invalid_json') from None
        if not isinstance(payload, dict):
            raise WebStop('invalid_payload')
        messages = [payload.get('message', '')]
        errors = payload.get('errors')
        if isinstance(errors, list):
            messages.extend(error.get('message', '') for error in errors if isinstance(error, dict))
        authentication = any(isinstance(message, str) and any(marker in message.lower() for marker in
            ('challenge_required', 'checkpoint_required', 'login_required')) for message in messages)
        if authentication or payload.get('challenge') or payload.get('checkpoint_url'):
            raise WebStop('AuthRequired: Instagram session verification required; no fallback',
                          endpoint='web_graphql' if web_api else 'graphql_query',
                          status=200, kind='auth_required')
        restricted = any(isinstance(message, str) and any(marker in message.lower() for marker in
            ('feedback_required', 'please wait a few minutes', 'too many requests', 'rate limit')) for message in messages)
        if restricted:
            self.block(900)
            raise WebStop('Instagram restriction; cooldown applied without fallback',
                          endpoint='web_graphql' if web_api else 'graphql_query',
                          status=200, kind='rate_limited')
        return payload
