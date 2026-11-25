"""Read bounded image bytes from the Bot's existing root-only Telegram cache."""
from contextlib import ExitStack
import os
from pathlib import Path
import re
import stat

from .. import core as c


MAX_IMAGE_BYTES = 5_000_000
CACHE = c.DATA / 'local-api-files'
_TOKEN = re.compile(r'[0-9]+:[A-Za-z0-9_-]+')


def image_type(raw):
    if raw.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
        return 'image/webp'
    if raw.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    return None


def read_image(token, path, kind, size):
    if (not _TOKEN.fullmatch(token) or kind not in ('photos', 'documents')
            or type(size) is not int or not 0 < size <= MAX_IMAGE_BYTES
            or not isinstance(path, str)):
        raise ValueError('Invalid Telegram file')
    relative = Path(path).relative_to(CACHE)
    parts = relative.parts
    if (len(parts) != 3 or parts[0] not in (token, token.replace(':', '~'))
            or parts[1] != kind or parts[2] == '..'):
        raise ValueError('Invalid Telegram file')

    # Open each component relative to a pinned parent. Symlinks and writable
    # directories never grant the unprivileged socket caller a root file read.
    with ExitStack() as stack:
        parent = None
        device = CACHE.parent.stat().st_dev
        for directory in (CACHE, *parts[:2]):
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, parent)
            info = os.fstat(parent)
            if info.st_uid != 0 or info.st_mode & 0o022 or info.st_dev != device:
                raise ValueError('Invalid Telegram cache directory')
        fd = os.open(parts[2], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                    or info.st_dev != device or info.st_size != size or info.st_nlink != 1):
                raise ValueError('Invalid Telegram file')
            data = stream.read(size + 1)
    if len(data) != size:
        raise ValueError('Invalid Telegram file size')
    return data
