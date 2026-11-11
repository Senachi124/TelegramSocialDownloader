"""Scoped YouTube Netscape Cookie import; values never belong in public state."""
from http.cookiejar import Cookie, DefaultCookiePolicy
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
    """Validate download payload again, then load only unexpired cookies in memory."""
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
        jar.set_cookie(Cookie(version=0,name=item['name'],value=item['value'],port=None,
            port_specified=False,domain=item['domain'],domain_specified=item['include_subdomains'],
            domain_initial_dot=item['domain'].startswith('.'),path=item['path'],path_specified=True,
            secure=item['secure'],expires=item['expires'] or None,discard=not item['expires'],
            comment=None,comment_url=None,rest={'HttpOnly':None} if item['http_only'] else {},rfc2109=False))
