"""Telegram Bot API requests, local-file uploads and response classification."""

import contextlib
import json
import os
import threading
import time

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

from .. import core as c, limits


class TelegramError(Exception):
    def __init__(self, code, retry=0, *, http_status=None, diagnostic=None):
        self.code = code
        self.retry = retry
        self.http_status = http_status
        self.diagnostic = diagnostic


class InvalidTelegramResponse(ValueError):
    def __init__(self, status):
        super().__init__('Unrecognized Telegram response')
        self.http_status = status


_transport = threading.local()
_LARGE_SEND = threading.BoundedSemaphore(1)


def _upload_size(files):
    total = 0
    for item in (files or {}).values():
        try:
            size = os.fstat(item[1].fileno()).st_size
        except (AttributeError, OSError, TypeError):
            continue
        if size > limits.telegram_file_limit():
            raise ValueError('檔案超過目前 Telegram 傳送上限，請改用下載連結')
        total += size
    return total


def _verified_file_uri(stream):
    try:
        path = c.safe_file(stream.name).resolve(strict=True)
        stat = path.stat()
        opened = os.fstat(stream.fileno())
        if (stat.st_dev, stat.st_ino, stat.st_size) == (opened.st_dev, opened.st_ino, opened.st_size):
            return path.as_uri()
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return None


def _local_file_payload(data, files, method=None):
    """Use local paths only for opened, verified files inside Bot media storage."""
    if not limits.local_api_enabled() or os.environ.get('MEDIA_BOT_LOCAL_FILE_UPLOADS') != '1':
        return None
    if method == 'sendMediaGroup':
        media = json.loads(data['media'])
        for item in media:
            field = item['media'].removeprefix('attach://')
            if field not in files:
                return None
            uri = _verified_file_uri(files[field][1])
            if uri is None:
                return None
            item['media'] = uri
        return {**data, 'media': media}
    if len(files) != 1:
        return None
    key, item = next(iter(files.items()))
    if key not in ('document', 'video', 'audio', 'animation', 'photo', 'voice', 'video_note'):
        return None
    # Telegram honors disable_content_type_detection only on multipart uploads.
    if key == 'document' and data.get('disable_content_type_detection'):
        return None
    uri = _verified_file_uri(item[1])
    return {**data, key: uri} if uri else None


def telegram(method, data, files=None):
    now = time.monotonic()
    if now >= getattr(_transport, 'token_until', 0):
        _transport.token = c.rpc(c.SECRET_SOCKET, '/telegram')['token']
        _transport.token_until = now + 5
    token = _transport.token
    if not token:
        raise TelegramError(401, diagnostic='send_token')
    if not hasattr(_transport, 'session'):
        _transport.session = requests.Session()
    base = limits.telegram_api_base()
    _transport.session.trust_env = not limits.local_api_enabled()
    url = base + '/bot' + token + '/' + method
    size = _upload_size(files)
    # Long media transfers never block independent getUpdates/status sessions.
    with _LARGE_SEND if size >= limits.LARGE_UPLOAD_THRESHOLD else contextlib.nullcontext():
        if files:
            local = _local_file_payload(data, files, method)
            if local is not None:
                r = _transport.session.post(
                    url, json=local, timeout=(5, limits.MEDIA_SEND_TIMEOUT), allow_redirects=False
                )
            else:
                fields = {
                    k: json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v) for k, v in data.items()
                }
                fields.update(files)
                body = MultipartEncoder(fields=fields)
                r = _transport.session.post(
                    url,
                    data=body,
                    headers={'Content-Type': body.content_type},
                    timeout=(5, limits.MEDIA_SEND_TIMEOUT),
                    allow_redirects=False,
                )
        else:
            r = _transport.session.post(
                url, json=data, timeout=(5, 35 if method == 'getUpdates' else 10), allow_redirects=False
            )
        try:
            status = r.status_code
            try:
                p = r.json()
            except ValueError:
                raise InvalidTelegramResponse(status) from None
            if not isinstance(p, dict) or type(p.get('ok')) is not bool:
                raise InvalidTelegramResponse(status)
            if not p['ok']:
                code = p.get('error_code')
                parameters = p.get('parameters', {})
                if (type(code) is not int or not isinstance(parameters, dict)
                        or (type(status) is int and status >= 500 and code < 500)):
                    raise InvalidTelegramResponse(status)
                delay = parameters.get('retry_after', 0)
                if type(delay) is not int or delay < 0:
                    raise InvalidTelegramResponse(status)
                # Local Bot API can return a single error after collecting both
                # successful and failed per-message callbacks for one album.
                description = p.get('description')
                description = description.strip().lower() if isinstance(description, str) else ''
                if method == 'sendMediaGroup' and (
                    not description
                    or 'failed to send message #' in description
                    or 'group send failed' in description
                ):
                    raise ValueError('Album delivery could be partial')
                if p.get('error_code') == 401:
                    _transport.token_until = 0
                raise TelegramError(code, delay, http_status=status)
            if 'result' not in p or p['result'] is None or (type(status) is int and status >= 400):
                raise InvalidTelegramResponse(status)
            return p['result']
        finally:
            r.close()
