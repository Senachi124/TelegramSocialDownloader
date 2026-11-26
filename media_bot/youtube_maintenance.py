"""Renew an existing YouTube session only after a signed-in first-party response."""
import codecs
from collections import Counter
from contextlib import suppress
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import http.client
from http.cookiejar import CookieJar
import json
import re
import socket
import threading
import time
from urllib.request import Request
from zoneinfo import ZoneInfo

from . import core, features, youtube_cookies
from .pool_checks import frozen

HOST = 'www.youtube.com'
URL = 'https://' + HOST + '/'
MAX_HEAD = 4 * 1024 * 1024
MAX_RETRY_AT = datetime.max.replace(tzinfo=ZoneInfo('Asia/Hong_Kong'), microsecond=0).timestamp()
TIMEOUT = 15
_CONFIG_CALL = re.compile(
    r'\s*(?:(?P<bootstrap>\(function(?: ytBootstrapConfig)?\(\)\s*\{\s*window\.ytplayer\s*=\s*\{\}\s*;)\s*)?'
    r'(?:window\.)?ytcfg\.set\s*\(\s*')
_BOOTSTRAP_END = re.compile(
    r'\}\)\(\);(?:\s*ytcfg\.set\(["\'][A-Za-z0-9_]+["\'],\s*window\.inner(?:Width|Height)\);)*'
    r'(?:\s*//# sourceURL=yt-desktop-config\.js)?\s*\Z')
_INERT = {'noscript', 'template', 'textarea', 'title', 'iframe', 'style'}


class _ConfigParser(HTMLParser):
    """Read only directly executed configuration scripts in the page head."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.head = self.done = self.script = False
        self.inert = []
        self.signed_in = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag in _INERT or self.inert:
            if tag in _INERT:
                self.inert.append(tag)
            return
        if tag == 'head':
            self.head = True
        elif tag == 'body':
            self.done = True
        elif tag == 'script' and self.head:
            attrs = dict(attrs)
            self.script = ('src' not in attrs and 'nomodule' not in attrs and (attrs.get('type') or '').lower()
                           in ('', 'text/javascript', 'application/javascript'))
        elif self.head and tag not in ('base', 'link', 'meta'):
            # A body element implicitly ends the HTML head.
            self.done = True

    def handle_endtag(self, tag):
        if self.inert:
            if tag == self.inert[-1]:
                self.inert.pop()
        elif tag == 'script' and self.script:
            self._read_config(''.join(self.parts))
            self.parts.clear()
            self.script = False
        elif tag == 'head':
            self.done = True

    def handle_data(self, data):
        if self.script and not self.inert and not self.done:
            self.parts.append(data)

    def _read_config(self, data):
        start = _CONFIG_CALL.match(data)
        if not start:
            return
        try:
            config, end = json.JSONDecoder().raw_decode(data, start.end())
        except (ValueError, RecursionError):
            return
        if not re.match(r'\s*\)\s*;', data[end:]) or not isinstance(config, dict):
            return
        if start.group('bootstrap') and not _BOOTSTRAP_END.search(data, end):
            return
        if type(config.get('LOGGED_IN')) is bool:
            self.signed_in = config['LOGGED_IN']
            self.done = True


def _signed_in(response, deadline):
    config = _ConfigParser()
    decoder = codecs.getincrementaldecoder('utf-8')()
    remaining = MAX_HEAD
    while remaining and not config.done and time.monotonic() < deadline:
        block = response.read1(min(65536, remaining))
        if not block:
            break
        config.feed(decoder.decode(block))
        remaining -= len(block)
    # HTMLParser can buffer short trailing chunks after a large script. Process
    # those bytes at EOF or the cap, still requiring a complete closing script tag.
    config.close()
    return config.signed_in


def extend_cooldown(retry_after):
    """Extend the YouTube cooldown atomically and return the effective deadline."""
    now = time.time()
    try:
        if retry_after and retry_after.isascii() and retry_after.isdecimal():
            until = now + int(retry_after)
        else:
            until = parsedate_to_datetime(retry_after).timestamp()
        # The job status date must remain representable in Hong Kong time.
        if not 0 < until <= MAX_RETRY_AT:
            raise ValueError
    except (ValueError, OverflowError, TypeError, AttributeError, OSError):
        until = now + 900
    until = max(now + 900, until)

    def extend(db):
        db.execute("""INSERT INTO settings(key,value) VALUES ('youtube_cooldown',?)
                      ON CONFLICT(key) DO UPDATE SET value=excluded.value
                      WHERE CAST(settings.value AS REAL) < CAST(excluded.value AS REAL)""", (json.dumps(until),))
        value = db.execute("SELECT value FROM settings WHERE key='youtube_cooldown'").fetchone()[0]
        return json.loads(value)
    return core.retry_write(extend)


def available():
    return (not frozen() and core.get_setting('enabled', False) and features.download_enabled('youtube')
            and core.get_setting('youtube_cooldown', 0) <= time.time())


def _end_request(sock):
    with suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


def check_session(cookies):
    """Return ('valid', scoped cookies) or a fixed failure code and None. Never follow redirects."""
    if not available():
        return 'deferred', None
    jar = CookieJar()
    request = Request(URL, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        'Accept': 'text/html', 'Accept-Encoding': 'identity', 'Accept-Language': 'en',
    })
    try:
        youtube_cookies.load_into(jar, cookies)
        jar.add_cookie_header(request)
    except (TypeError, ValueError):
        return 'invalid_cookies', None
    names = {pair.partition('=')[0].strip() for pair in request.get_header('Cookie', '').split(';')}
    if not names.intersection(youtube_cookies.AUTH_NAMES):
        return 'invalid_cookies', None
    deadline = time.monotonic() + TIMEOUT
    connection = http.client.HTTPSConnection(HOST, timeout=TIMEOUT)
    timer = None
    try:
        connection.connect()
        # A socket inactivity timeout alone can be kept alive by trickling headers/body.
        timer = threading.Timer(max(0, deadline - time.monotonic()), _end_request, args=(connection.sock,))
        timer.daemon = True
        timer.start()
        connection.request('GET', '/', headers=dict(request.header_items()))
        with connection.getresponse() as response:
            if response.status == 429:
                extend_cooldown(response.getheader('Retry-After'))
                return 'rate_limited', None
            if response.status == 401:
                return 'login_required', None
            if response.status != 200:
                return 'unverified', None
            content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type != 'text/html' or response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                return 'unverified', None
            signed_in = _signed_in(response, deadline)
            if signed_in is False:
                return 'login_required', None
            if signed_in is not True:
                return 'unverified', None
            try:
                return 'valid', youtube_cookies.apply_response(cookies, URL, response.headers)
            except ValueError:
                return 'invalid_cookies', None
    except (OSError, http.client.HTTPException, UnicodeError, ValueError, RecursionError):
        return 'unverified', None
    finally:
        if timer:
            timer.cancel()
        connection.close()


def run():
    if not available():
        return {'deferred': 1}
    counts = Counter()
    pool = core.rpc(core.SECRET_SOCKET, '/accounts')['youtube']
    for item in pool['items']:
        if not item['enabled'] or not item['cookie_set']:
            continue
        if not available():
            counts['deferred'] += 1
            break
        snapshot = core.rpc(core.SECRET_SOCKET, '/youtube-refresh/snapshot', {'id': item['id']})
        if not snapshot:
            counts['superseded'] += 1
            continue
        status, fresh = check_session(snapshot['cookies'])
        if status == 'valid':
            if not available():
                counts['deferred'] += 1
                break
            result = core.rpc(core.SECRET_SOCKET, '/youtube-refresh/result',
                              {'id': snapshot['id'], 'revision': snapshot['revision'], 'cookies': fresh})
            status = result.get('status')
            if status not in ('updated', 'unchanged', 'superseded'):
                status = 'broker_error'
        counts[status] += 1
        if status in ('deferred', 'rate_limited'):
            break
    return dict(counts)
