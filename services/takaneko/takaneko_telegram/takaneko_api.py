"""Private Fanclub API client; raw HTML is kept separate from attachments.

No credentials are written here. The caller persists ``session_snapshot`` using
its privileged secret broker if the upstream rotates a refresh token.

Notification field order was verified on 2026-11-03 in the official public
https://takanekofc.com/assets/components.js ``formatMessageNotification``:
videoId, image01, body01, image02, body02. Source HTML fields remain intact in
``source_fields``; account-specific {{nickname}} placeholders are not guessed.
Official article paths were verified in that site's legacy main.js and Nuxt
CivcpQNb.js routes. No membership content was fetched for this verification.
"""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
import hashlib
import http.client
import html
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import time
import uuid
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit
from media_bot import download_size

API_HOST = 'api.takanekofc.com'
SITE = 'https://takanekofc.com/'
MEDIA_HOSTS = frozenset({'takanekofc.com', 'www.takanekofc.com', API_HOST,
                         'player.vimeo.com', 'www.youtube.com', 'img.youtube.com', 'i.ytimg.com'})
MAX_JSON = 8 * 1024 * 1024
MAX_ITEMS = 100000
# The Fanclub message list uses 50 rows per page. Requests for 200 return HTTP 400.
PAGE_SIZE = 50


class FanclubError(RuntimeError):
    """Intentionally contains no response content, token, or signed URL."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _epoch(value):
    if value is None or value == '':
        return 0
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return 0
        return number / 1000 if number > 100000000000 else number
    except (ValueError, TypeError):
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return dt.timestamp() if dt.tzinfo else 0
        except ValueError:
            return 0


def _identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise FanclubError('Invalid source identifier')
    result = str(value)
    if not result or len(result) > 256 or any(ord(c) < 32 for c in result):
        raise FanclubError('Invalid source identifier')
    return result


def _public_connection(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise FanclubError('Source destination is not public')
    address = addresses[0][4][0]
    conn = http.client.HTTPSConnection(host, timeout=30)
    # Pin the checked address while retaining hostname TLS certificate validation.
    conn._create_connection = lambda destination, timeout, source_address=None: socket.create_connection((address, 443), timeout)
    return conn


def media_url(value, allowed_hosts=MEDIA_HOSTS):
    if not isinstance(value, str) or not value or any(ord(c) < 33 for c in value):
        raise FanclubError('Invalid media URL')
    url = urljoin(SITE, value)
    parsed = urlsplit(url)
    if parsed.scheme == 'http' and parsed.hostname in ('img.youtube.com', 'i.ytimg.com'):
        url = 'https:' + url[5:]
        parsed = urlsplit(url)
    try:
        valid = (parsed.scheme == 'https' and parsed.hostname in allowed_hosts and
                 parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise FanclubError('Media host is not allowed')
    return url


def canonical_video_url(value):
    """Return a single-video Vimeo/YouTube URL, rejecting arbitrary provider paths."""
    if not isinstance(value, str) or not value or len(value) > 8192 or any(ord(c) < 33 for c in value):
        raise FanclubError('Invalid video URL')
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise ValueError()
        query = parse_qs(parsed.query, strict_parsing=False, max_num_fields=50)
        host, path = parsed.hostname, parsed.path
        if host in ('vimeo.com', 'www.vimeo.com', 'player.vimeo.com'):
            pattern = r'/video/(\d+)/?' if host == 'player.vimeo.com' else r'/(\d+)(?:/([a-fA-F0-9]+))?/?'
            match = re.fullmatch(pattern, path)
            if not match:
                raise ValueError()
            hashes = query.get('h', [])
            if len(hashes) > 1 or (hashes and not re.fullmatch(r'[a-fA-F0-9]+', hashes[0])):
                raise ValueError()
            unlisted = match[2] if match.lastindex == 2 else None
            if unlisted and hashes and unlisted != hashes[0]:
                raise ValueError()
            unlisted = unlisted or (hashes[0] if hashes else None)
            return 'https://player.vimeo.com/video/' + match[1] + ('?h=' + unlisted if unlisted else '')
        if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'www.youtube-nocookie.com', 'youtube-nocookie.com', 'youtu.be'):
            if host == 'youtu.be':
                identifier = path.strip('/') if re.fullmatch(r'/[A-Za-z0-9_-]{11}/?', path) else ''
            elif path == '/watch' and host not in ('www.youtube-nocookie.com', 'youtube-nocookie.com'):
                values = query.get('v', [])
                identifier = values[0] if len(values) == 1 else ''
            else:
                match = re.fullmatch(r'/(?:embed|shorts|v)/([A-Za-z0-9_-]{11})/?', path)
                identifier = match[1] if match else ''
            if re.fullmatch(r'[A-Za-z0-9_-]{11}', identifier):
                return 'https://www.youtube.com/watch?v=' + identifier
        raise ValueError()
    except (ValueError, TypeError):
        raise FanclubError('Unsupported video URL') from None


def _published_digest(target, guard, limit):
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise FanclubError('Invalid existing media destination')
        digest = hashlib.sha256()
        while True:
            guard()
            chunk = stream.read(256 * 1024)
            if not chunk:
                return digest.digest()
            digest.update(chunk)


class Client:
    def __init__(self, session_dict, *, media_hosts=()):
        if not isinstance(session_dict, dict):
            raise FanclubError('Fanclub login is not configured')
        self._session = deepcopy(session_dict)
        self.media_hosts = MEDIA_HOSTS | frozenset(media_hosts)
        self._token = None
        self._until = 0

    @property
    def session_snapshot(self):
        return deepcopy(self._session)

    def _cookie(self, path):
        cookies = self._session.get('cookies', [])
        if not isinstance(cookies, list) or len(cookies) > 1000:
            raise FanclubError('Invalid Fanclub login')
        result = []
        for item in cookies:
            if not isinstance(item, dict):
                raise FanclubError('Invalid Fanclub login')
            if item.get('domain') not in ('.takanekofc.com', API_HOST, '.' + API_HOST):
                continue
            cookie_path = str(item.get('path', '/'))
            if not cookie_path.startswith('/') or any(ord(c) < 32 for c in cookie_path):
                raise FanclubError('Invalid Fanclub login')
            if not (path == cookie_path or path.startswith(cookie_path.rstrip('/') + '/')):
                continue
            name, value = str(item.get('name', '')), str(item.get('value', ''))
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or len(value) > 16384 or any(c in value for c in '\r\n;'):
                raise FanclubError('Invalid Fanclub login')
            result.append(name + '=' + value)
            if sum(map(len, result)) > 256 * 1024:
                raise FanclubError('Fanclub cookie header exceeds limit')
        return '; '.join(result)

    def _request(self, path, *, body=None, token=None):
        if not path.startswith('/') or path.startswith('//') or any(ord(c) < 32 for c in path):
            raise FanclubError('Invalid API path')
        headers = {'Accept': 'application/json', 'Origin': SITE.rstrip('/'), 'Referer': SITE,
                   'User-Agent': 'TelegramSocialDownloader/Takaneko'}
        cookie = self._cookie(path)
        if cookie:
            headers['Cookie'] = cookie
        if token:
            if not isinstance(token, str) or any(ord(c) < 32 for c in token):
                raise FanclubError('Invalid Fanclub login')
            headers['Authorization'] = token if token.startswith('Bearer ') else 'Bearer ' + token
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        conn = None
        try:
            conn = _public_connection(API_HOST)
            conn.request('POST' if body is not None else 'GET', path, body=payload, headers=headers)
            response = conn.getresponse()
            if response.status != 200:
                raise FanclubError('Fanclub HTTP ' + str(response.status), status=response.status)
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise FanclubError('Fanclub response exceeds limit')
            return json.loads(raw)
        except FanclubError:
            raise
        except Exception:
            raise FanclubError('Fanclub request failed') from None
        finally:
            if conn:
                conn.close()

    def _authenticate(self):
        if self._token and time.monotonic() < self._until:
            return self._token
        refresh = self._session.get('refreshToken')
        if refresh is not None and (not isinstance(refresh, str) or not refresh or len(refresh) > 16384):
            raise FanclubError('Invalid Fanclub login')
        if refresh or self._session.get('cookies'):
            try:
                result = self._request('/auth/refresh', body={'refreshToken': refresh} if refresh else {})
            except FanclubError as error:
                # Match the original import behavior only for a definitive invalid
                # refresh; a rate limit/network failure must not trigger more calls.
                fallback = self._session.get('accessToken')
                if error.status != 400 or not isinstance(fallback, str) or not fallback:
                    raise
                self._request('/auth/notifications/count', token=fallback)
                result = {'accessToken': fallback}
            if not isinstance(result, dict) or not isinstance(result.get('accessToken'), str) or not result['accessToken']:
                raise FanclubError('Fanclub refresh returned no token')
            token = result['accessToken']
            rotated = result.get('refreshToken')
            if rotated is not None:
                if not isinstance(rotated, str) or not rotated or len(rotated) > 16384:
                    raise FanclubError('Invalid rotated Fanclub login')
                self._session['refreshToken'] = rotated
            self._session['accessToken'] = token
        else:
            token = self._session.get('accessToken') or self._session.get('token')
            if not isinstance(token, str) or not token:
                raise FanclubError('Fanclub login is not configured')
        if not isinstance(token, str) or not token or len(token) > 16384 or any(ord(c) < 32 for c in token):
            raise FanclubError('Invalid Fanclub login')
        self._token = token
        self._until = time.monotonic() + 240
        return token

    def _get(self, path):
        return self._request(path, token=self._authenticate())

    def _count(self):
        result = self._get('/auth/notifications/count')
        if not isinstance(result, dict):
            raise FanclubError('Invalid notification count')
        count = result.get('count', result.get('total'))
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_ITEMS:
            raise FanclubError('Invalid notification count')
        return count

    @staticmethod
    def _summary(kind, row):
        if not isinstance(row, dict):
            raise FanclubError('Invalid listing item')
        identifier = _identifier(row.get('notificationReservationId') if kind == 'notification' else row.get('id'))
        published = next((_epoch(row.get(k)) for k in ('releaseDate', 'displayDate', 'publishedAt', 'createdAt', 'sentAt') if row.get(k)), 0)
        routes = {'notification': 'notification/', 'blog': 'blogs/topics/', 'gallery': 'galleries/', 'movie': 'movies/'}
        return {'id': kind + ':' + identifier, 'title': str(row.get('title') or row.get('subject') or ''),
                'published': published, 'url': SITE + routes[kind] + quote(identifier, safe='')}

    def list_posts(self):
        """Enumerate all four source feeds or raise; never return partial results.

        Count drift, duplicate pages, page-count drift, malformed data and any
        unauthorized feed abort the snapshot so historical baselines stay closed.
        """
        deadline = time.monotonic() + 240
        def check_budget():
            if time.monotonic() >= deadline:
                raise FanclubError('Fanclub listing exceeded time limit')
        expected = self._count()
        result, seen, notification_ids, offset = [], set(), set(), 0
        while True:
            check_budget()
            payload = self._get('/auth/notifications?' + urlencode({'offset': offset, 'limit': PAGE_SIZE, 'orderType': 2, 'readType': 'all'}))
            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict):
                rows = payload.get('items', payload.get('data'))
            else:
                rows = None
            if not isinstance(rows, list):
                raise FanclubError('Invalid notification listing')
            if not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    raise FanclubError('Invalid listing item')
                # The count includes billing/system notices, as does the listing.
                # Count and deduplicate all rows, but only archive article notices.
                notification_id = _identifier(row.get('notificationId') or row.get('notificationReservationId'))
                if notification_id in notification_ids:
                    raise FanclubError('Notification pagination repeated an item')
                notification_ids.add(notification_id)
                if not row.get('notificationReservationId') and row.get('notificationType') not in (None, '', 'message'):
                    continue
                summary = self._summary('notification', row)
                if summary['id'] in seen:
                    raise FanclubError('Notification pagination repeated an item')
                seen.add(summary['id'])
                result.append(summary)
            offset += len(rows)
            if offset > MAX_ITEMS or offset > expected:
                raise FanclubError('Notification listing changed during scan')
        if offset != expected or self._count() != expected:
            raise FanclubError('Incomplete or changing notification listing')
        feeds = [('blog', '/blog/queries/getArticleList', 'articleList', {'blogId': 'topics', 'categories': 'nuzufcwpxr5s3iip'}),
                 ('gallery', '/gallery/queries/getGalleryAlbumList', 'galleryAlbumList', {'pageSize': 20}),
                 ('movie', '/movie/queries/getMovieList', 'movieList', {'pageSize': 20})]
        for kind, endpoint, key, params in feeds:
            page, expected_pages = 1, None
            while expected_pages is None or page <= expected_pages:
                check_budget()
                payload = self._get(endpoint + '?' + urlencode({**params, 'page': page}))
                if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
                    raise FanclubError('Invalid ' + kind + ' listing')
                pages = payload.get('totalPages')
                if isinstance(pages, bool) or not isinstance(pages, int) or not 0 <= pages <= 10000:
                    raise FanclubError('Invalid ' + kind + ' pagination')
                if expected_pages is not None and pages != expected_pages:
                    raise FanclubError('Source pagination changed during scan')
                expected_pages = pages
                rows = payload[key]
                if (not rows and pages > 0) or (rows and pages == 0):
                    raise FanclubError('Incomplete source page')
                for row in rows:
                    summary = self._summary(kind, row)
                    if summary['id'] in seen:
                        raise FanclubError('Source pagination repeated an item')
                    seen.add(summary['id'])
                    result.append(summary)
                    if len(result) > MAX_ITEMS:
                        raise FanclubError('Source listing exceeds limit')
                page += 1
        check_budget()
        return result

    def detail(self, summary):
        try:
            kind, identifier = summary['id'].split(':', 1)
        except (KeyError, ValueError, TypeError):
            raise FanclubError('Invalid source item') from None
        identifier = quote(_identifier(identifier), safe='')
        endpoints = {'notification': '/auth/notifications/', 'blog': '/blog/queries/getArticleDetail/',
                     'gallery': '/gallery/queries/getGalleryAlbumDetail/', 'movie': '/movie/queries/getMovieDetail/'}
        if kind not in endpoints:
            raise FanclubError('Unsupported source item')
        data = self._get(endpoints[kind] + identifier)
        if not isinstance(data, dict):
            raise FanclubError('Invalid source detail')
        if data.get('isPermissionDenied') is True:
            raise FanclubError('Fanclub content permission denied', status=403)
        attachments = []
        def attach(media_kind, value, **extra):
            attachments.append({'kind': media_kind, 'url': media_url(value, self.media_hosts), **extra})
        source_fields = {}
        if kind == 'notification':
            # Verified against the official notification page's public
            # assets/components.js formatMessageNotification (2026-11-03):
            # Vimeo first, image01/body01, then image02/body02. Keep body HTML
            # intact rather than collecting all images at the end.
            source_fields = {key: value for key, value in data.items()
                             if re.fullmatch(r'(?:body|image)\d*|videoId', key)}
            sections = []
            if data.get('videoId'):
                video_id = str(data['videoId'])
                match = re.fullmatch(r'(\d+)(?:/([a-fA-F0-9]+)|\?h=([a-fA-F0-9]+))?', video_id)
                if not match:
                    raise FanclubError('Invalid notification Vimeo source')
                source = 'https://player.vimeo.com/video/' + match[1]
                if match[2] or match[3]:
                    source += '?h=' + (match[2] or match[3])
                sections.append('<div class="body-section"><iframe src="' + html.escape(source, quote=True) + '"></iframe></div>')
            suffixes = sorted({key[4:] for key in data if re.fullmatch(r'body\d+', key)} |
                              {key[5:] for key in data if re.fullmatch(r'image\d+', key)}, key=lambda value: (int(value), value))
            for suffix in suffixes:
                image, content = data.get('image' + suffix), data.get('body' + suffix)
                if image:
                    source = media_url(image, self.media_hosts)
                    sections.append('<div class="body-section"><img src="' + html.escape(source, quote=True) + '" /></div>')
                if content:
                    if not isinstance(content, str):
                        raise FanclubError('Invalid source HTML')
                    sections.append('<div class="body-section body-wysiwyg">' + content + '</div>')
            if not suffixes:
                content = data.get('body') or data.get('message') or data.get('content') or data.get('text') or ''
                if not isinstance(content, str):
                    raise FanclubError('Invalid source HTML')
                sections.append(content)
                if data.get('image'):
                    source = media_url(data['image'], self.media_hosts)
                    sections.append('<img src="' + html.escape(source, quote=True) + '" />')
            body = ''.join(sections)
        elif kind == 'blog':
            body = data.get('body') or ''
            if data.get('thumbnail'):
                attach('photo', data['thumbnail'], role='cover')
        else:
            body = data.get('description') or ''
            if data.get('thumbnail'):
                attach('photo', data['thumbnail'], role='cover')
            if kind == 'gallery':
                if not isinstance(data.get('galleryAlbumItems'), list):
                    raise FanclubError('Invalid gallery detail')
                for item in data['galleryAlbumItems']:
                    if not isinstance(item, dict) or not item.get('file'):
                        raise FanclubError('Invalid gallery attachment')
                    attach('photo', item['file'])
            else:
                movie_type = str(data.get('movieType') or 'vimeo').lower()
                video_id = str(data.get('videoId') or '')
                if movie_type == 'youtube' and re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
                    attach('video', 'https://www.youtube.com/watch?v=' + video_id, provider='youtube')
                elif movie_type in ('video', 'vimeo'):
                    match = re.fullmatch(r'(\d+)(?:/([a-fA-F0-9]+)|\?h=([a-fA-F0-9]+))?', video_id)
                    if not match:
                        raise FanclubError('Invalid Vimeo source')
                    suffix = '?h=' + (match[2] or match[3]) if match[2] or match[3] else ''
                    attach('video', 'https://player.vimeo.com/video/' + match[1] + suffix, provider='vimeo')
                else:
                    raise FanclubError('Unsupported movie source')
        if not isinstance(body, str):
            raise FanclubError('Invalid source HTML')
        return {'id': summary['id'], 'title': str(data.get('title') or data.get('subject') or summary.get('title') or ''),
                'url': self._summary(kind, {('notificationReservationId' if kind == 'notification' else 'id'): summary['id'].split(':', 1)[1]})['url'],
                'html': body, 'attachments': attachments, 'source_fields': source_fields,
                'published': self._summary(kind, {**data, ('notificationReservationId' if kind == 'notification' else 'id'): summary['id'].split(':', 1)[1]})['published'] or summary.get('published', 0),
                'author_name': str(data.get('sendingOfficialUserName') or ''),
                'author_id': str(data.get('sendingOfficialUserId') or '')}

    def download(self, url, target, guard, *, max_bytes=None):
        """Stream a direct media file; caller guard verifies HDD and reserve.

        Video provider pages are intentionally not downloaded as video files.
        No login headers/cookies are ever forwarded to media or redirect hosts.
        """
        target = Path(target)
        max_bytes=min(max_bytes or download_size.limits.MAX_DOWNLOAD_BYTES,download_size.limits.MAX_DOWNLOAD_BYTES)
        partial = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
        existing = _published_digest(target, guard, max_bytes)
        started = time.monotonic()
        for _ in range(6):
            url = media_url(url, self.media_hosts)
            parsed = urlsplit(url)
            if parsed.hostname in ('player.vimeo.com', 'www.youtube.com'):
                raise FanclubError('Video provider requires media extractor')
            conn = None
            try:
                guard()
                conn = _public_connection(parsed.hostname)
                conn.request('GET', parsed.path + ('?' + parsed.query if parsed.query else ''),
                             headers={'Referer': SITE, 'User-Agent': 'Mozilla/5.0'})
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location:
                        raise FanclubError('Invalid media redirect')
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise FanclubError('Media HTTP ' + str(response.status))
                length = response.getheader('Content-Length')
                download_size.declared({'Content-Length':length})
                if length is not None and (not length.isdigit() or int(length) > max_bytes):
                    raise FanclubError('Media exceeds size limit')
                mime = (response.getheader('Content-Type') or '').split(';', 1)[0].lower()
                if mime in ('text/html', 'application/json'):
                    raise FanclubError('Media returned a document instead of a file')
                size = 0
                # After a crash the file can exist before its size receipt. Verify the
                # entire new response without accumulating another copy on every retry.
                received = hashlib.sha256()
                with (partial.open('xb') if existing is None else nullcontext(None)) as stream:
                    while True:
                        guard()
                        if time.monotonic() - started > 300:
                            raise FanclubError('Media download exceeded time limit')
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        download_size.check(size)
                        if size > max_bytes:
                            raise FanclubError('Media exceeds size limit')
                        received.update(chunk)
                        if existing is None:
                            stream.write(chunk)
                    if existing is None:
                        stream.flush()
                        os.fsync(stream.fileno())
                if not size or (length is not None and size != int(length)):
                    raise FanclubError('Incomplete media response')
                guard()
                if existing is None:
                    try:
                        os.link(partial, target)
                    except FileExistsError:
                        existing = _published_digest(target, guard, max_bytes)
                        if existing is None or existing != received.digest():
                            raise FanclubError('Media destination differs; inspect the saved copies')
                    partial.unlink()
                elif received.digest() != existing:
                    raise FanclubError('Existing media differs from the complete response')
                return {'path': str(target), 'size': size, 'mime': mime}
            except FanclubError:
                raise
            except Exception:
                raise FanclubError('Media download failed') from None
            finally:
                if conn:
                    conn.close()
        raise FanclubError('Too many media redirects')
