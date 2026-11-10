"""Explicit runtime inputs; no bundled credentials or machine-specific defaults."""
import json
import os
from pathlib import Path
import re
import stat
import tempfile

PLATFORMS = ('instagram', 'tiktok', 'twitter', 'youtube')
LABELS = {'instagram': 'Instagram', 'tiktok': 'TikTok', 'twitter': 'Twitter / X', 'youtube': 'YouTube'}
_FILES = {p: p.upper() + '_COOKIES_FILE' for p in PLATFORMS if p != 'youtube'}


class ConfigurationError(ValueError):
    """Messages contain setting names only, never configured paths or values."""


def data_dir():
    value = os.environ.get('MEDIA_BOT_DATA', '')
    path = Path(value)
    if not value or not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ConfigurationError('MEDIA_BOT_DATA must name an existing absolute directory')
    return path.resolve()


def _allowed_users():
    raw = os.environ.get('MEDIA_BOT_ALLOWED_USERS', '')
    parts = raw.split(',')
    if not parts or any(not re.fullmatch(r'[1-9][0-9]{0,18}', p.strip()) for p in parts):
        raise ConfigurationError('MEDIA_BOT_ALLOWED_USERS requires positive numeric user IDs')
    return {int(p.strip()) for p in parts}


def allowed(chat):
    try:
        return type(chat) is int and chat in _allowed_users()
    except ConfigurationError:
        return False


def _read_external(name, optional=False):
    value = os.environ.get(name, '')
    if not value and optional:
        return None
    path = Path(value)
    if not value or not path.is_absolute():
        raise ConfigurationError(name + ' requires an absolute file path')
    # Restrict accidental persistence inside the checkout or runtime media tree.
    root = Path(__file__).resolve().parent.parent
    resolved = path.resolve()
    if resolved.is_relative_to(root) or resolved.is_relative_to(data_dir()):
        raise ConfigurationError(name + ' must be outside code and runtime data directories')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 1024 * 1024:
                raise ConfigurationError(name + ' must be a private regular file (mode 0600)')
            return stream.read(1024 * 1024 + 1)
    except (OSError, UnicodeError):
        raise ConfigurationError(name + ' cannot be read securely') from None


def token():
    value = _read_external('TELEGRAM_BOT_TOKEN_FILE').strip()
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', value):
        raise ConfigurationError('TELEGRAM_BOT_TOKEN_FILE has invalid token format')
    return value


def cookies(platform):
    if platform == 'youtube':
        return {}
    if platform not in _FILES:
        raise ConfigurationError('Unsupported download platform')
    raw = _read_external(_FILES[platform], optional=True)
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not k or not v
            or len(k) > 256 or len(v) > 16384
            or any(ord(ch) < 32 for ch in k + v)
            for k, v in value.items()
        ):
            raise ValueError
        required = {'instagram': ('sessionid', 'csrftoken'), 'twitter': ('auth_token', 'ct0')}
        if any(not value.get(key) for key in required.get(platform, ())):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ConfigurationError(_FILES[platform] + ' requires a valid JSON cookie object') from None


def validate():
    os.umask(0o077)
    root = data_dir()
    _allowed_users()
    token()
    for platform in PLATFORMS:
        cookies(platform)
    for name in ('tmp', 'media'):
        path = root / name
        if path.is_symlink():
            raise ConfigurationError('Runtime directories must not be symbolic links')
        path.mkdir(mode=0o700, exist_ok=True)
        if path.stat().st_dev != root.stat().st_dev:
            raise ConfigurationError('Media and temporary directories must share the data filesystem')
    # Extractors inherit this directory; never silently spill media to a system temp directory.
    for key in ('TMPDIR', 'TMP', 'TEMP'):
        os.environ[key] = str(root / 'tmp')
    tempfile.tempdir = str(root / 'tmp')
