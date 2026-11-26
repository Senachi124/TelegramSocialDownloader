"""Retain verified first-party cookie changes from one account-bound download."""
from email.message import Message
import logging

from . import core, youtube_cookies


def _cookie_headers(response):
    # yt-dlp's Requests and curl adapters flatten repeated headers in the
    # public mapping. Their underlying responses retain individual fields.
    native = getattr(response.fp, 'headers', None)
    if hasattr(native, 'getlist'):
        values = native.getlist('Set-Cookie')
    elif hasattr(native, 'get_all'):
        values = native.get_all('Set-Cookie', [])
    else:
        native = getattr(getattr(response.fp, '_response', None), 'headers', None)
        if not hasattr(native, 'get_list'):
            return None
        values = native.get_list('Set-Cookie')
    if not values:
        return None
    headers = Message()
    for value in values:
        headers.add_header('Set-Cookie', value)
    return headers


class DownloadSession:
    def __init__(self, binding, cookies):
        self.binding = binding
        self.original = cookies
        self.candidate = cookies

    def observe(self, request, response):
        if self.candidate is None or response.status != 200:
            return
        try:
            requested = request if isinstance(request, str) else getattr(request, 'url', None)
            if not all(youtube_cookies.trusted_response_url(url) for url in (requested, response.url)):
                return
            headers = _cookie_headers(response)
            if headers is not None:
                self.candidate = youtube_cookies.apply_response(self.candidate, response.url, headers)
        except Exception:
            # A partially understood rotation must not replace the stored session.
            self.candidate = None

    def save_if_verified(self):
        if self.candidate is None or self.candidate == self.original:
            return None
        from .youtube_maintenance import available, check_session
        try:
            current = core.rpc(core.SECRET_SOCKET, '/youtube-refresh/snapshot', {'id': self.binding['id']})
            if current.get('id') != self.binding['id'] or current.get('revision') != self.binding['revision']:
                return None
            status, verified = check_session(self.candidate)
            if status == 'valid' and available():
                reply = core.rpc(core.SECRET_SOCKET, '/youtube-refresh/result', {**self.binding, 'cookies': verified})
                if reply.get('status') == 'updated' and reply.get('revision') == self.binding['revision'] + 1:
                    return reply['revision']
        except Exception:
            logging.getLogger(__name__).warning('YouTube session update unavailable')
        return None
