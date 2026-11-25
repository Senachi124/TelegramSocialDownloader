"""Validate operator-supplied fanclub credentials without contacting upstream services."""
import json
import re

_API_DOMAINS = {'.takanekofc.com', 'api.takanekofc.com', '.api.takanekofc.com'}


def _token(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 16384 or
            any(ord(ch) < 32 or ord(ch) >= 127 for ch in value)):
        raise ValueError('Invalid session token')
    return value


def normalize_session(value):
    if isinstance(value, list):
        value = {'cookies': value}
    if not isinstance(value, dict):
        raise ValueError('Expected a fanclub session object or browser cookie array')
    session = {key: _token(value[key]) for key in ('accessToken', 'refreshToken', 'token') if key in value}
    if 'cookies' in value:
        cookies = value['cookies']
        if not isinstance(cookies, list) or len(cookies) > 1000:
            raise ValueError('Invalid fanclub cookies')
        normalized = []
        for item in cookies:
            if not isinstance(item, dict):
                raise ValueError('Invalid fanclub cookie')
            domain = item.get('domain')
            if not isinstance(domain, str) or domain.lower() not in _API_DOMAINS:
                continue
            name = item.get('name')
            path = item.get('path', '/')
            cookie = item.get('value')
            if (not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}", name)
                    or not isinstance(path, str) or not path.startswith('/') or len(path) > 2048
                    or any(ord(char) < 32 or ord(char) >= 127 for char in path)
                    or not isinstance(cookie, str) or len(cookie) > 16384 or
                    any(ord(char) < 32 or ord(char) >= 127 or char == ';' for char in cookie)):
                raise ValueError('Invalid fanclub cookie')
            normalized.append(dict(name=name, value=cookie, domain=domain.lower(), path=path))
        if normalized:
            session['cookies'] = normalized
    if not session:
        raise ValueError('Supply an API session token or cookies valid for the fanclub API')
    if len(json.dumps(session).encode()) > 44 * 1024:
        raise ValueError('Fanclub session exceeds size limit')
    return session


def normalize_credentials(value):
    if not isinstance(value, dict) or 'session' not in value:
        raise ValueError('Expected a session and optional translation keys')
    session = normalize_session(value['session'])
    keys = value.get('openai_keys', [])
    if (not isinstance(keys, list) or len(keys) > 32 or any(
            not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']
            or not isinstance(item.get('key'), str) or not item['key']
            or type(item.get('enabled')) is not bool for item in keys)):
        raise ValueError('Invalid translation keys')
    if len(json.dumps(keys).encode()) > 12 * 1024:
        raise ValueError('Translation keys exceed size limit')
    primary = value.get('primary_key', '')
    if not isinstance(primary, str) or len(primary) > 256:
        raise ValueError('Invalid primary key ID')
    return dict(session=session, openai_keys=keys, primary_key=primary)
