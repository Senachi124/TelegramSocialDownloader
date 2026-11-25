"""Runtime switches owned by the media bot's local settings database."""
from . import accounts, core


LABELS = {f'download_{platform}': f'{accounts.LABELS[platform]} downloads'
          for platform in accounts.DOWNLOAD_PLATFORMS}
LABELS['translation'] = 'Translation and OCR'


def _key(feature_id):
    if feature_id not in LABELS:
        raise ValueError('Unknown feature')
    return 'feature:' + feature_id


def enabled(feature_id, db=None):
    key = _key(feature_id)
    if db is None:
        with core.db() as connection:
            return enabled(feature_id, connection)
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row is None or row[0] == 'true'


def snapshot(db=None):
    if db is None:
        with core.db() as connection:
            return snapshot(connection)
    stored = dict(db.execute("SELECT key,value FROM settings WHERE key GLOB 'feature:*'"))
    return {feature_id: stored.get(_key(feature_id), 'true') == 'true' for feature_id in LABELS}


def set_enabled(feature_id, value):
    key = _key(feature_id)
    if type(value) is not bool:
        raise ValueError('Invalid feature state')
    core.retry_write(lambda db: db.execute('''INSERT INTO settings(key,value) VALUES (?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (key, 'true' if value else 'false')))


def download_enabled(platform, db=None):
    return enabled('download_' + platform, db)


def require_downloads(platforms, db):
    disabled = [accounts.LABELS[p] for p in dict.fromkeys(platforms) if not download_enabled(p, db)]
    if disabled:
        raise ValueError('目前已停用 ' + '、'.join(disabled) + ' 下載。可重新送出其他平台的連結。')
