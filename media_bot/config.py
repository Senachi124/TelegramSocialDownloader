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
        raw = _read_external('YOUTUBE_COOKIES_FILE', optional=True)
        if raw is None:return {}
        from .youtube_cookies import parse
        try:return parse(raw)
        except ValueError:raise ConfigurationError('YOUTUBE_COOKIES_FILE requires valid Netscape cookies') from None
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
    from . import limits,links
    try:limits.telegram_api_base()
    except ValueError:raise ConfigurationError('MEDIA_BOT_API_BASE must use the official API or literal loopback HTTP endpoint') from None
    links.configuration()
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


def load_environment(filename):
    """Read literal KEY=value assignments. Never execute a shell or expand secrets."""
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 1024 * 1024:
            raise ConfigurationError('Environment file must be private and regular')
        parsed = {}
        for number, raw in enumerate(stream, 1):
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if not sep or not re.fullmatch(r'[A-Z][A-Z_0-9]*', key) or '\x00' in value or key in parsed:
                raise ConfigurationError(f'Invalid environment assignment on line {number}')
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ConfigurationError(f'Unclosed quote on line {number}')
                value = value[1:-1]
            parsed[key] = value
        for key, value in parsed.items():
            os.environ.setdefault(key, value)


def validate_service(component):
    from urllib.parse import urlsplit
    from . import limits
    try:
        limits.telegram_api_base()
        origin = urlsplit(os.environ.get('MEDIA_BOT_ORIGIN', ''))
        if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
                or origin.path not in ('', '/') or origin.query or origin.fragment):
            raise ValueError()
        origin.port
    except ValueError:
        raise ConfigurationError('Set MEDIA_BOT_ORIGIN to the public HTTPS origin and use a supported Telegram API endpoint') from None
    base = os.environ.get('MEDIA_BOT_BASE', '/')
    if not re.fullmatch(r'/(?:[a-zA-Z0-9_-]+/)*', base):
        raise ConfigurationError('MEDIA_BOT_BASE must be a slash-terminated URL path')
    socket_path = Path(os.environ.get('MEDIA_BOT_SECRET_SOCKET', ''))
    if not socket_path.is_absolute():
        raise ConfigurationError('Set MEDIA_BOT_SECRET_SOCKET to an absolute path')
    owner = os.environ.get('MEDIA_BOT_OWNER_ID', '')
    if owner and (not re.fullmatch(r'[1-9]\d{0,18}', owner) or int(owner) > 2**63-1):
        raise ConfigurationError('MEDIA_BOT_OWNER_ID must be a positive Telegram user ID')
    if component == 'broker':
        secret_dir = Path(os.environ.get('MEDIA_BOT_SECRET_DIR', ''))
        if (not secret_dir.is_absolute() or secret_dir.resolve() != secret_dir
                or not secret_dir.is_dir() or secret_dir.stat().st_mode & 0o077
                or secret_dir.stat().st_uid != os.geteuid()):
            raise ConfigurationError('Set MEDIA_BOT_SECRET_DIR to a private directory owned by the broker')
        return
    if component == 'web':
        from . import admin_auth
        try:
            admin_auth._verifier()
        except ValueError as error:
            raise ConfigurationError(str(error)) from None
    if component == 'push' and not Path(os.environ.get('MEDIA_BOT_PUSH_SOCKET', '')).is_absolute():
        raise ConfigurationError('Set MEDIA_BOT_PUSH_SOCKET to an absolute path')
    root = data_dir()
    if os.environ.get('MEDIA_BOT_STORAGE_UUID'):
        import subprocess
        try:
            output = subprocess.check_output(['findmnt', '--json', '--target', str(root),
                                              '--output', 'UUID,TARGET,FSROOT'], timeout=10)
            mount, = json.loads(output)['filesystems']
            actual = Path(mount['fsroot']) / root.relative_to(mount['target'])
            expected = Path(os.environ.get('MEDIA_BOT_FILESYSTEM_ROOT', str(root)))
            if mount['uuid'] != os.environ['MEDIA_BOT_STORAGE_UUID'] or actual != expected:
                raise ValueError()
        except (ValueError, OSError, KeyError, subprocess.SubprocessError):
            raise ConfigurationError('Configured data filesystem is unavailable') from None
    temporary = Path(os.environ.get('TMPDIR', str(root / 'tmp')))
    if not temporary.is_absolute() or not temporary.is_relative_to(root / 'tmp'):
        raise ConfigurationError('TMPDIR must be in the service data tmp directory')
    for directory in (root / 'media', temporary):
        if directory.resolve() != directory:
            raise ConfigurationError('Media and temporary directories must not contain symlinks')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_dev != root.stat().st_dev:
            raise ConfigurationError('Media and temporary directories must share the data filesystem')
    for key in ('TMPDIR', 'TMP', 'TEMP'):
        os.environ[key] = str(temporary)
    tempfile.tempdir = str(temporary)
