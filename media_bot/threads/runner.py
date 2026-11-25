"""Threads post capture with optional isolated browser-session read access.

Only the exact requested shortcode is eligible. Replies/recommendations and
video cover images are never substitutes for a missing target or video stream.
"""
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
import uuid

from .. import core as c, threads, media_preferences
from .. import download_size, video

PAGE_LIMIT = 8 * 1024**2
MEDIA_LIMIT = download_size.limits.MAX_DOWNLOAD_BYTES
CRAWLER_UA = 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'


class CaptureError(Exception):
    pass


def checked_url(url, media=False):
    try:
        p = urlsplit(url)
        host = p.hostname or ''
        allowed = host.endswith(('.cdninstagram.com', '.fbcdn.net')) if media else host in threads.HOSTS
        if (not allowed or p.scheme != 'https' or p.username or p.password or p.port not in (None, 443)
                or len(url) > 16384 or any(ord(ch) < 33 or ord(ch) == 127 for ch in url)):
            raise ValueError
        return p
    except (TypeError, ValueError):
        raise CaptureError('extractor_error') from None


def connection(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise CaptureError('extractor_error')
    address = addresses[0][4][0]
    conn = http.client.HTTPSConnection(host, timeout=30)
    conn._create_connection = lambda destination, timeout, source_address=None: socket.create_connection((address, 443), timeout)
    return conn


def cookie_headers(cookies):
    if not isinstance(cookies, dict) or not cookies.get('sessionid') or not cookies.get('csrftoken'):
        raise CaptureError('login_required')
    selected = {}
    for name in ('sessionid', 'csrftoken', 'ds_user_id', 'mid', 'ig_did'):
        value = cookies.get(name)
        if value:
            if not isinstance(value, str) or len(value) > 16384 or any(ord(ch) < 33 or ord(ch) > 126 or ch == ';' for ch in value):
                raise CaptureError('login_required')
            selected[name] = value
    return {'Cookie': '; '.join(k+'='+v for k,v in selected.items()),
            'X-CSRFToken': selected['csrftoken'], 'X-IG-App-ID': '238260118697367',
            'X-IG-WWW-Claim': '0', 'X-ASBD-ID': '129477',
            'Accept': 'application/json, text/plain, */*'}


def open_response(url, result, media=False, cookies=None):
    authenticated_path = urlsplit(url).path if cookies else None
    for _ in range(6):
        p = checked_url(url, media)
        if cookies and (media or p.hostname != 'www.threads.com' or p.query or p.fragment
                        or p.path != authenticated_path or not re.fullmatch(r'/api/v1/text_feed/[0-9]+/replies/', p.path)):
            raise CaptureError('extractor_error')
        conn = connection(p.hostname)
        try:
            result['requests'] += 1
            headers={'User-Agent': 'Mozilla/5.0' if media else CRAWLER_UA,
                     'Accept-Encoding': 'identity', 'Referer': 'https://www.threads.com/'}
            if cookies:
                headers.update(cookie_headers(cookies))
                headers['User-Agent'] = 'Barcelona 289.0.0.14.109 Android'
            conn.request('GET', urlunsplit(('', '', p.path or '/', p.query, '')), headers=headers)
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                target = response.getheader('Location')
                if not target:
                    raise CaptureError('extractor_error')
                url = urljoin(url, target)
                if cookies:
                    redirected = checked_url(url)
                    if redirected.hostname != 'www.threads.com':
                        raise CaptureError('extractor_error')
                    if redirected.path != authenticated_path or redirected.query or redirected.fragment:
                        raise CaptureError('login_required' if '/login' in redirected.path else 'extractor_error')
                conn.close()
                continue
            if response.status == 429:
                try:
                    delay = max(900, min(86400, float(response.getheader('Retry-After', '900'))))
                except (ValueError, TypeError):
                    delay = 900
                result.update(retry_at=time.time()+delay, wait_reason='platform')
                c.setting('threads_cooldown', result['retry_at'])
                raise CaptureError('rate_limited')
            if response.status != 200:
                raise CaptureError({401: 'forbidden' if media else 'login_required', 403: 'forbidden', 404: 'unavailable'}.get(response.status, 'network_error'))
            return conn, response
        except BaseException:
            conn.close()
            raise
    raise CaptureError('extractor_error')


class JSONScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.chunks = []
        self.values = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.active = dict(attrs).get('type') == 'application/json'
            self.chunks = []

    def handle_data(self, value):
        if self.active:
            self.chunks.append(value)

    def handle_endtag(self, tag):
        if tag == 'script' and self.active:
            try:
                self.values.append(json.loads(''.join(self.chunks)))
            except (ValueError, RecursionError):
                pass
            self.active = False


def extract_assets(html, code):
    parser = JSONScripts()
    parser.feed(html)
    return assets_from_values(parser.values, code)


def assets_from_values(values, code):
    stack = list(values)
    matches = []
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if value.get('code') == code and 'media_type' in value:
                matches.append(value)
                continue  # Carousel children belong to this post, not new matches.
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    if not matches:
        raise CaptureError('unavailable')
    # Duplicate payloads must agree; never guess another post or incomplete set.
    parsed = [post_assets(post) for post in matches]
    if any(items != parsed[0] for items in parsed[1:]):
        raise CaptureError('extractor_error')
    return parsed[0]


def authenticated_assets(code, cookies, result):
    # Threads shortcodes encode the numeric post ID, like Instagram media IDs.
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    number = 0
    for character in code:
        number = number * 64 + alphabet.index(character)
    url = 'https://www.threads.com/api/v1/text_feed/' + str(number) + '/replies/'
    conn, response = open_response(url, result, cookies=cookies)
    try:
        raw = response.read(PAGE_LIMIT+1)
        if len(raw) > PAGE_LIMIT:
            raise CaptureError('extractor_error')
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise CaptureError('extractor_error') from None
    finally:
        conn.close()
    if not isinstance(data, dict):
        raise CaptureError('extractor_error')
    message = str(data.get('message', '')).lower()
    if data.get('require_login') or message in ('login_required', 'not logged in'):
        raise CaptureError('login_required')
    if data.get('challenge') or message in ('challenge_required', 'checkpoint_required'):
        raise CaptureError('login_required')
    if data.get('status') not in (None, 'ok'):
        raise CaptureError('forbidden')
    return assets_from_values([data], code)


def inline_reel(post):
    info = post.get('text_post_app_info') or {}
    media = info.get('linked_inline_media')
    if not isinstance(media, dict) or media.get('media_type') != 2:
        return None

    code = media.get('code')
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{5,64}', code):
        raise CaptureError('unsupported_media')
    preview = info.get('link_preview_attachment') or {}
    links = [url for url in (media.get('permalink'), preview.get('url')) if url]
    reel = False
    for url in links:
        try:
            parsed = urlsplit(c.validate_url(url))
        except (ValueError, TypeError):
            return None
        if parsed.hostname not in ('instagram.com', 'www.instagram.com', 'm.instagram.com'):
            return None
        route = parsed.path.strip('/').split('/')
        if len(route) != 2 or route[0] not in ('p', 'reel', 'reels') or route[1] != code:
            return None
        reel = reel or route[0] in ('reel', 'reels')
    return media if reel else None


def post_assets(post):
    carousel = post.get('carousel_media')
    if post.get('media_type') == 8:
        if not isinstance(carousel, list) or not 1 <= len(carousel) <= 100:
            raise CaptureError('unsupported_media')
        items = carousel
    elif post.get('media_type') == 19 and not carousel:
        reel = inline_reel(post)
        if reel is None:
            return []
        items = [reel]
    else:
        items = [post]
    result = []
    for item in items:
        kind = item.get('media_type')
        if kind == 2:
            variants = item.get('video_versions')
            ext = '.mp4'
        elif kind == 1:
            variants = (item.get('image_versions2') or {}).get('candidates')
            ext = '.jpg'
        else:
            raise CaptureError('unsupported_media')
        if not isinstance(variants, list) or not variants:
            raise CaptureError('unsupported_media')
        def area(v):
            try:
                return max(0, int(v.get('width') or 0)) * max(0, int(v.get('height') or 0))
            except (ValueError, TypeError):
                return 0
        candidates = [v for v in variants if isinstance(v, dict) and v.get('url')]
        if not candidates:
            raise CaptureError('unsupported_media')
        best = max(candidates, key=lambda v: (kind == 2 and media_preferences.native_mp4_variant(v), area(v)))
        p = checked_url(best['url'], media=True)
        suffix = Path(p.path).suffix.lower()
        if kind == 2 and suffix in video.VIDEO_DEMUXERS:
            ext = suffix
        if kind == 1:
            if suffix in ('.png', '.webp', '.gif', '.jpeg', '.jpg'):
                ext = suffix
        result.append({'url': best['url'], 'ext': ext, 'kind': 'video' if kind == 2 else 'photo'})
    return result


def storage_guard():
    from .. import storage
    now = time.monotonic()
    refresh = now >= getattr(storage_guard, 'until', 0)
    try:
        storage.check(download=True, data=c.DATA, verify_identity=refresh)
    except storage.StorageError:
        raise CaptureError('storage_error') from None
    if refresh:
        storage_guard.until = now + 3


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(asset, path, result):
    receipt = path.with_suffix(path.suffix+'.receipt.json')
    # Stable URL path identifies the same media even if CDN signatures rotate.
    parsed = checked_url(asset['url'], media=True)
    source = hashlib.sha256((parsed.hostname+parsed.path).encode()).hexdigest()
    if path.is_symlink() or receipt.is_symlink():
        raise CaptureError('extractor_error')
    if path.is_file() and receipt.is_file():
        download_size.check(path.stat().st_size)
        try:
            saved = json.loads(receipt.read_text())
            if saved['source'] == source and path.stat().st_size == saved['size'] and digest(path) == saved['sha256']:
                return
        except (ValueError, KeyError, OSError):
            pass
        raise CaptureError('extractor_error')  # preserve a conflicting original
    storage_guard()
    if c.reserve_media('threads', str(path.resolve())):
        result['media_attempts'] += 1
    conn, response = open_response(asset['url'], result, media=True)
    part = path.with_name(path.name+'.'+uuid.uuid4().hex+'.part')
    try:
        download_size.declared({'Content-Length':response.getheader('Content-Length')})
        kind = response.getheader('Content-Type', '').split(';')[0].lower()
        if not (kind.startswith('video/' if asset['kind'] == 'video' else 'image/') or kind == 'application/octet-stream'):
            raise CaptureError('extractor_error')
        size = 0
        with part.open('xb') as stream:
            while True:
                chunk = response.read(256*1024)
                if not chunk:
                    break
                storage_guard()
                size += len(chunk)
                download_size.check(size)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        expected = response.getheader('Content-Length')
        if not size or (expected and size != int(expected)):
            raise CaptureError('network_error')
        checksum = digest(part)
        try:
            os.link(part, path)  # no overwrite; same verified service filesystem
        except FileExistsError:
            # Recover a crash after publishing media but before its receipt.
            # Accept only byte-identical data; preserve conflicting originals.
            if path.is_symlink() or path.stat().st_size != size or digest(path) != checksum:
                raise CaptureError('extractor_error') from None
        part.unlink()
        record = {'source': source, 'size': size, 'sha256': checksum}
        pending = receipt.with_suffix('.pending')
        pending.write_text(json.dumps(record))
        pending.replace(receipt)
    finally:
        conn.close()


def run(payload):
    root = Path(payload['root']).resolve()
    if not root.is_relative_to((c.DATA/'media').resolve()):
        raise ValueError('Invalid capture root')
    result = dict(code=1, retry_at=0, auth_required=False, files=[], failure='', cookie_used=False,
                  started=time.time(), requests=0, media_attempts=0, wait_reason='', engine='threads-public')
    try:
        storage_guard()
        root.mkdir(parents=True, exist_ok=True)
        code = threads.post_id(payload['url'])
        cookies = payload.get('cookies') or {}
        if cookies:
            cookie_headers(cookies)
            result['cookie_used'] = True
            result['engine'] = 'threads-session'
            assets = authenticated_assets(code, cookies, result)
        else:
            conn, response = open_response(payload['url'], result)
            try:
                raw = response.read(PAGE_LIMIT+1)
                if len(raw) > PAGE_LIMIT:
                    raise CaptureError('extractor_error')
            finally:
                conn.close()
            assets = extract_assets(raw.decode('utf-8'), code)
        if not assets:
            result.update(code=0, no_media=True)
        else:
            for index, asset in enumerate(assets, 1):
                path = root/(code+'_'+str(index).zfill(2)+asset['ext'])
                # Earlier captures used .mp4 for every video. Keep a receipted
                # original under its prior name so an in-flight retry can resume.
                previous = path.with_suffix('.mp4')
                if (asset['kind'] == 'video' and path != previous and not path.exists()
                        and previous.is_file() and previous.with_suffix('.mp4.receipt.json').is_file()):
                    path = previous
                download(asset, path, result)
                result['files'].append({'path': str(path), 'name': path.name, 'size': path.stat().st_size})
            result['code'] = 0
    except download_size.DownloadTooLarge:
        download_size.mark(result)
    except c.RateLimited as error:
        result.update(retry_at=error.until, wait_reason='quota')
    except CaptureError as error:
        result['failure'] = str(error)
        result['auth_required'] = bool(payload.get('cookies')) and str(error) == 'login_required'
    except (OSError, http.client.HTTPException, TimeoutError):
        result['failure'] = 'network_error'
    except Exception:
        result['failure'] = 'extractor_error'
    finally:
        if root.is_dir():
            storage_guard()
            result['finished'] = time.time()
            pending = root/'result.tmp'
            pending.write_text(json.dumps(result))
            pending.replace(root/'result.json')
    return result


def main():
    run(json.load(sys.stdin))


if __name__ == '__main__':
    main()
