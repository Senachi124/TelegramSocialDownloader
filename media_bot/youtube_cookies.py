"""Scoped YouTube Netscape Cookie import; values never belong in public state."""
from email.message import Message
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from urllib.parse import urlsplit
from urllib.request import Request
from types import SimpleNamespace
import re
import time

AUTH_NAMES = {'SID', '__Secure-1PSID', '__Secure-3PSID', 'SAPISID',
              '__Secure-1PAPISID', '__Secure-3PAPISID'}
ERROR = '請匯入含有效 YouTube 登入 Cookie 的 Netscape cookies.txt'


def _valid_cookie(item):
    domain = item['domain']
    host = domain.lstrip('.')
    if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*youtube\.com', host):
        raise ValueError(ERROR)
    if domain.startswith('..') or item['include_subdomains'] != domain.startswith('.'):
        raise ValueError(ERROR)
    if not re.fullmatch(r'[A-Za-z0-9_!#$%&\x27*+.^`|~-]+', item['name']):
        raise ValueError(ERROR)
    if not item['path'].startswith('/') or any(ord(c)<33 or ord(c)>126 or c in ';,' for c in item['path']):
        raise ValueError(ERROR)
    if not item['value'] or len(item['value'])>16384 or any(ord(c)<33 or ord(c)>126 or c in ';,\\"' for c in item['value']):
        raise ValueError(ERROR)
    if any(type(item[k]) is not bool for k in ('include_subdomains','secure','http_only')):
        raise ValueError(ERROR)
    if type(item['expires']) is not int or item['expires'] < 0:
        raise ValueError(ERROR)


def parse(text):
    """Keep only YouTube scopes, retaining Netscape domain/path/session semantics."""
    if not isinstance(text,str) or len(text)>131072 or '\x00' in text or '\t' not in text:
        raise ValueError(ERROR)
    items = {}
    for line in text.lstrip('\ufeff').splitlines():
        http_only = line.startswith('#HttpOnly_')
        if http_only:line = line[10:]
        elif line.startswith('#') or not line.strip():continue
        fields = line.split('\t')
        if len(fields)!=7:raise ValueError(ERROR)
        domain, include, path, secure, expiry, name, value = fields
        domain=domain.lower()
        host=domain.lstrip('.')
        # An export may include other sites; never retain or forward those values.
        if host!='youtube.com' and not host.endswith('.youtube.com'):continue
        if include not in ('TRUE','FALSE') or secure not in ('TRUE','FALSE') or not expiry.isascii() or not expiry.isdecimal():
            raise ValueError(ERROR)
        item=dict(domain=domain,include_subdomains=include=='TRUE',path=path,
                  secure=secure=='TRUE',expires=int(expiry),name=name,value=value,http_only=http_only)
        _valid_cookie(item)
        if item['expires'] and item['expires']<=time.time():continue
        key=(domain,path,name)
        if key in items and items[key]!=item:raise ValueError('YouTube Cookie 有重複而不同的值，請只匯出一個登入帳號')
        items[key]=item
        if len(items)>256:raise ValueError(ERROR)
    if not any(item['name'] in AUTH_NAMES for item in items.values()):raise ValueError(ERROR)
    return {'version':1,'items':list(items.values())}


def load_into(jar, data):
    """Validate broker payload again, then load only unexpired cookies in memory."""
    try:
        if data.get('version')!=1 or not isinstance(data['items'],list) or not 0<len(data['items'])<=256:
            raise ValueError(ERROR)
        current=[]
        for item in data['items']:
            _valid_cookie(item)
            if not item['expires'] or item['expires']>time.time():current.append(item)
        if not any(item['name'] in AUTH_NAMES for item in current):raise ValueError(ERROR)
    except (KeyError,TypeError,AttributeError):raise ValueError(ERROR) from None
    # Python's default Netscape policy otherwise allows host-only cookies on subdomains.
    jar.set_policy(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    for item in current:
        jar.set_cookie(_as_cookie(item))


def _as_cookie(item):
    return Cookie(version=0, name=item['name'], value=item['value'], port=None,
        port_specified=False, domain=item['domain'], domain_specified=item['include_subdomains'],
        domain_initial_dot=item['domain'].startswith('.'), path=item['path'], path_specified=True,
        secure=item['secure'], expires=item['expires'] or None, discard=not item['expires'],
        comment=None, comment_url=None, rest={'HttpOnly': None} if item['http_only'] else {}, rfc2109=False)


_FIELDS = {'domain', 'include_subdomains', 'path', 'secure', 'expires', 'name', 'value', 'http_only'}
_ORIGIN_SCOPES = {'www.youtube.com', '.www.youtube.com', '.youtube.com'}
_LOGIN_COOKIES = AUTH_NAMES | {'HSID', 'SSID', 'APISID', 'SIDCC', 'LOGIN_INFO', 'LSID', 'OSID'}


def _records(data, *, require_live=True):
    """Validate the complete private payload before staging any individual change."""
    if (not isinstance(data, dict) or set(data) != {'version', 'items'} or
            type(data['version']) is not int or data['version'] != 1 or
            not isinstance(data['items'], list) or not 0 < len(data['items']) <= 256):
        raise ValueError(ERROR)
    records = {}
    size = 0
    for item in data['items']:
        if (not isinstance(item, dict) or set(item) != _FIELDS or
                not all(isinstance(item[k], str) for k in ('name', 'value', 'domain', 'path'))):
            raise ValueError(ERROR)
        _valid_cookie(item)
        if (len(item['domain']) > 253 or len(item['name']) > 256 or
                len(item['path']) > 4096 or item['expires'] > 2**63 - 1):
            raise ValueError(ERROR)
        size += sum(len(item[k]) for k in ('name', 'value', 'domain', 'path'))
        key = (item['domain'], item['path'], item['name'])
        if key in records or size > 131072:
            raise ValueError(ERROR)
        records[key] = item.copy()
    now = time.time()
    if not any(row['name'] in AUTH_NAMES and (not require_live or not row['expires'] or row['expires'] > now)
               for row in records.values()):
        raise ValueError(ERROR)
    return records


def _validate_changes(before, after):
    names = {key[2] for key in before}
    for key in before.keys() | after.keys():
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        domain, path, name = key
        if domain not in _ORIGIN_SCOPES:
            raise ValueError(ERROR)
        protected = name in _LOGIN_COOKIES or name.startswith(('__Secure-', '__Host-'))
        if new is None:
            # A first-party expiry may clear an auxiliary cookie, but losing a
            # session cookie requires another login from the administrator.
            if protected:
                raise ValueError(ERROR)
            continue
        if old is None and name in names:
            # A new scope with the same name can shadow or broaden a login.
            raise ValueError(ERROR)
        if old and ((old['secure'] and not new['secure']) or (old['http_only'] and not new['http_only'])):
            raise ValueError(ERROR)
        if protected:
            if old is None and not new['secure']:
                raise ValueError(ERROR)
            if old and old['expires'] and not new['expires']:
                raise ValueError(ERROR)
        if name.startswith(('__Secure-', '__Host-')) and not new['secure']:
            raise ValueError(ERROR)
        if name.startswith('__Host-') and (new['include_subdomains'] or path != '/'):
            raise ValueError(ERROR)
        if new['expires'] and new['expires'] <= time.time():
            raise ValueError(ERROR)
        names.add(name)


def trusted_response_url(url):
    if not isinstance(url, str) or not url or any(ord(char) <= 32 or ord(char) == 127 or char == '\\' for char in url):
        return False
    try:
        target = urlsplit(url)
        return target.scheme == 'https' and target.netloc.lower() in ('www.youtube.com', 'www.youtube.com:443')
    except ValueError:
        return False


def apply_response(cookies, url, headers):
    """Apply individual Set-Cookie headers from a trusted response to a detached jar.

    Expiry of an auxiliary cookie is honored. An auth deletion or unsafe scope
    or flag change rejects the entire renewal. Never save the extractor's jar.
    """
    before = _records(cookies)
    original = {'version': 1, 'items': list(before.values())}
    if not trusted_response_url(url):
        return original
    lines = headers.get_all('Set-Cookie', []) if hasattr(headers, 'get_all') else None
    if lines is None or len(lines) > 256:
        raise ValueError(ERROR)
    if not lines:
        return original
    safe = Message()
    total_size = 0
    for line in lines:
        if not isinstance(line, str):
            raise ValueError(ERROR)
        total_size += len(line)
        if (len(line) > 20000 or total_size > 131072 or any(ord(c) < 32 or ord(c) > 126 for c in line) or
                re.search(r",\s*[!#$%&'*+.^_`|~\w-]+=", line) or
                re.search(r';\s*(?:partitioned|sameparty|port)(?:\s*(?:;|=|$))', line, re.I)):
            raise ValueError(ERROR)
        attributes = [part.partition('=')[0].strip().lower()
                      for part in line.split(';')[1:] if part.strip()]
        if len(attributes) != len(set(attributes)):
            raise ValueError(ERROR)
        safe['Set-Cookie'] = line
    jar = CookieJar(policy=DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain,
                                               strict_ns_set_path=True))
    for item in before.values():
        jar.set_cookie(_as_cookie(item))
    jar.extract_cookies(SimpleNamespace(info=lambda: safe), Request(url))
    candidate = {'version': 1, 'items': [
        dict(domain=c.domain, include_subdomains=c.domain_specified, path=c.path, secure=c.secure,
             expires=c.expires or 0, name=c.name, value=c.value,
             http_only=any(k.lower() == 'httponly' for k in c._rest)) for c in jar
    ]}
    after = _records(candidate)
    _validate_changes(before, after)
    return original if before == after else candidate


def _account(data, key):
    from . import accounts
    if not isinstance(key, str) or not 0 < len(key) <= 128:
        raise ValueError(ERROR)
    return next((a for a in accounts.pool(data, 'youtube')['items']
                 if a['id'] == key and a['enabled'] and a['cookies'] and a['last_result'] != 'auth_required'), None)


def refresh_snapshot(data, request):
    if not isinstance(request, dict) or set(request) != {'id'}:
        raise ValueError(ERROR)
    account = _account(data, request['id'])
    if not account:
        return {}
    # The maintenance caller reports expired sessions separately from invalid broker data.
    items = _records(account['cookies'], require_live=False)
    return {'id': account['id'], 'revision': account['revision'],
            'cookies': {'version': 1, 'items': list(items.values())}}


def apply_refresh(data, request):
    """Compare and swap a broker-owned account. Only 'updated' requires a vault write."""
    if (not isinstance(request, dict) or set(request) != {'id', 'revision', 'cookies'} or
            type(request['revision']) is not int or request['revision'] <= 0):
        raise ValueError(ERROR)
    account = _account(data, request['id'])
    if account is None or account['revision'] != request['revision']:
        return {'status': 'superseded'}
    before, after = _records(account['cookies']), _records(request['cookies'])
    _validate_changes(before, after)
    if before == after:
        return {'status': 'unchanged', 'revision': account['revision']}
    account.update(cookies={'version': 1, 'items': list(after.values())},
                   revision=account['revision'] + 1, updated=time.time())
    return {'status': 'updated', 'revision': account['revision']}
