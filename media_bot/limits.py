"""Shared decimal-byte limits and constrained Telegram transport configuration."""
import ipaddress
import os
from urllib.parse import urlsplit

MAX_TELEGRAM_BYTES = TELEGRAM_FILE_LIMIT = 500_000_000
MAX_DOWNLOAD_BYTES = DOWNLOAD_FILE_LIMIT = 2_000_000_000
LINK_TTL_SECONDS = LINK_TTL = 2 * 60 * 60
DIRECT_RETENTION_SECONDS = 24 * 60 * 60
LARGE_UPLOAD_THRESHOLD = 50_000_000
MEDIA_SEND_TIMEOUT = 1800
CLOUD_FILE_LIMIT = 49 * 1024**2 - 1


def telegram_api_base():
    value = os.environ.get('MEDIA_BOT_API_BASE', 'https://api.telegram.org').rstrip('/')
    if value == 'https://api.telegram.org':
        return value
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or '')
        if (parsed.scheme != 'http' or not address.is_loopback or parsed.port is None
                or not 1 <= parsed.port <= 65535 or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError()
    except ValueError:
        raise ValueError('Telegram API 必須是官方 HTTPS 或明確指定埠的本機 loopback HTTP 位址') from None
    return value


def local_api_enabled():
    return telegram_api_base() != 'https://api.telegram.org'


def telegram_file_limit():
    # Retain safe cloud behavior during preparation or an intentional rollback.
    return MAX_TELEGRAM_BYTES if local_api_enabled() else CLOUD_FILE_LIMIT
