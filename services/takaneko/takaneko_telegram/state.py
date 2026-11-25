"""Notification state, scoped to the Fanclub producer."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit

CONTROL = Path(os.getenv('TAKANEKO_CONTROL', '/var/lib/telegram-takaneko/control'))
MEDIA = Path(os.getenv('TAKANEKO_MEDIA', '/var/lib/telegram-takaneko/media'))
TMP = Path(os.getenv('TMPDIR', '/var/lib/telegram-takaneko/tmp'))
LINK_BASE = os.getenv('TAKANEKO_LINK_BASE', '')


def validate_link_base():
    url = urlsplit(LINK_BASE)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or
            url.query or url.fragment or not url.path.startswith('/') or not url.path.endswith('/') or
            any(char.isspace() or ord(char) < 32 for char in LINK_BASE)):
        raise ValueError('TAKANEKO_LINK_BASE must be an absolute HTTPS URL ending in /')


@contextmanager
def db():
    with closing(sqlite3.connect(CONTROL / 'state.sqlite3', timeout=15)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        with connection:
            yield connection


def initialize():
    from . import takaneko_feed
    with db() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)')
    takaneko_feed.initialize()


def is_busy(error):
    return isinstance(error, sqlite3.OperationalError) and getattr(error, 'sqlite_errorcode', 0) & 255 in (5, 6)


def retry_write(operation):
    deadline = time.monotonic() + 120
    while True:
        try:
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                return operation(conn)
        except sqlite3.OperationalError as error:
            if not is_busy(error) or time.monotonic() >= deadline:
                raise
            time.sleep(.5)


def get_setting(key, default=None):
    with db() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def setting(key, value):
    setting_many({key: value})


def setting_many(values):
    with db() as conn:
        conn.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',
                         [(key, json.dumps(value)) for key, value in values.items()])


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def safe_file(path):
    path = Path(path)
    if path.resolve() != path or not path.is_relative_to(MEDIA):
        raise ValueError('Invalid media path')
    return path


def storage_guard(*, verify_identity=True):
    if (not MEDIA.is_absolute() or not TMP.is_absolute() or MEDIA.resolve(strict=True) != MEDIA
            or TMP.resolve(strict=True) != TMP or not MEDIA.is_dir() or not TMP.is_dir()
            or TMP.stat().st_dev != MEDIA.stat().st_dev or Path(os.environ['TMPDIR']) != TMP):
        raise RuntimeError('Producer storage unavailable')
    expected_uuid = os.getenv('TAKANEKO_STORAGE_UUID')
    if verify_identity and expected_uuid:
        for path, setting in ((MEDIA, 'TAKANEKO_MEDIA_FSROOT'), (TMP, 'TAKANEKO_TMP_FSROOT')):
            output = subprocess.check_output(
                ['findmnt', '--json', '--target', str(path), '--output', 'TARGET,UUID,FSROOT'],
                timeout=10, stderr=subprocess.DEVNULL)
            mount, = json.loads(output)['filesystems']
            actual = Path(mount['fsroot']) / path.relative_to(mount['target'])
            if mount.get('uuid') != expected_uuid or actual != Path(os.getenv(setting, str(path))):
                raise RuntimeError('Producer filesystem identity or root does not match')
    if shutil.disk_usage(MEDIA).free < 5 * 1024**3:
        raise RuntimeError('Producer storage reserve reached')
