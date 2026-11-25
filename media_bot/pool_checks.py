"""Admin-triggered, single-post download checks executed by the platform worker."""
import json
import logging
import re
import secrets
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from . import accounts, core as c, features, storage, video


FORM_ERRORS = {
    'link': 'Enter an HTTPS post or video URL for this platform.',
    'missing': 'Enter a media post URL for this platform.',
    'disabled': 'Enable the downloader before testing.',
    'busy': 'Another check is already in progress.',
    'feature': 'Enable downloads for this platform in Features before testing.',
}
def frozen():
    import os
    flag = os.environ.get('MEDIA_BOT_FREEZE_FILE')
    return bool(flag and Path(flag).exists())

QUEUE_TTL = 600
CAPTURE_TIMEOUT = 120
PUBLIC_LABEL = 'Public access'
PROBE_FORMATS = {**video.VIDEO_DEMUXERS, '.jpg': 'jpeg_pipe', '.jpeg': 'jpeg_pipe',
                 '.png': 'png_pipe', '.webp': 'webp_pipe', '.gif': 'gif'}


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS admin_pool_checks(
        platform TEXT PRIMARY KEY, id TEXT NOT NULL, url TEXT NOT NULL, explicit INTEGER NOT NULL,
        state TEXT NOT NULL,
        requested REAL NOT NULL, started REAL, finished REAL,
        account_label TEXT NOT NULL DEFAULT '', revision INTEGER, detail TEXT NOT NULL DEFAULT '')''')
    db.execute('''CREATE TABLE IF NOT EXISTS admin_pool_check_scratch(
        id TEXT PRIMARY KEY, platform TEXT NOT NULL)''')


    db.execute("UPDATE admin_pool_checks SET url='' WHERE state NOT IN ('queued','running') AND url!=''")


def _post(platform, value):
    try:
        url = c.validate_url(value.strip())
        provider, mode = c.classify(url)
        if provider != platform or mode not in ('post', 'choice'):
            raise ValueError
        parts = urlsplit(url)
        return url if platform == 'youtube' else urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    except (AttributeError, TypeError, ValueError):
        raise ValueError(FORM_ERRORS['link']) from None


def start(platform, url=''):
    if platform not in accounts.DOWNLOAD_PLATFORMS:
        raise ValueError(FORM_ERRORS['link'])
    target = _post(platform, url) if url and url.strip() else ''
    explicit = bool(target)
    if frozen() or not c.get_setting('enabled', False):
        raise ValueError(FORM_ERRORS['disabled'])
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not features.download_enabled(platform, db):
            raise ValueError(FORM_ERRORS['feature'])
        previous = db.execute('SELECT url,explicit,state,requested FROM admin_pool_checks WHERE platform=?',
                              (platform,)).fetchone()
        now = time.time()
        if previous:
            if previous['state'] == 'running' or (
                    previous['state'] == 'queued' and previous['requested'] >= now - QUEUE_TTL):
                if target and target != previous['url']:
                    raise ValueError(FORM_ERRORS['busy'])
                return
        if not target:
            raise ValueError(FORM_ERRORS['missing'])
        db.execute("""INSERT INTO admin_pool_checks(platform,id,url,explicit,state,requested)
            VALUES (?,?,?,?,'queued',?) ON CONFLICT(platform) DO UPDATE SET id=excluded.id,
            url=excluded.url,explicit=excluded.explicit,state='queued',requested=excluded.requested,
            started=NULL,finished=NULL,account_label='',revision=NULL,detail=''""",
            (platform, secrets.token_hex(12), target, explicit, now))


def latest(platform):
    with c.db() as db:
        row = db.execute('''SELECT state,requested,started,finished,account_label,revision,detail
                           FROM admin_pool_checks WHERE platform=?''', (platform,)).fetchone()
    return dict(row) if row else None


def _scratch(check_id):
    if not re.fullmatch(r'[0-9a-f]{24}', check_id):
        raise ValueError('Invalid check ID')
    return c.DATA / 'media' / ('.pool-check-' + check_id)


def _remove_scratch(check_id):
    root = _scratch(check_id)
    if root.is_symlink():
        raise ValueError('Invalid check directory')
    try:
        shutil.rmtree(root)
    except FileNotFoundError:
        pass
    c.retry_write(lambda db: db.execute('DELETE FROM admin_pool_check_scratch WHERE id=?', (check_id,)))


def cleanup_pending():
    with c.db() as db:
        rows = db.execute("""SELECT s.id FROM admin_pool_check_scratch s WHERE NOT EXISTS(
            SELECT 1 FROM admin_pool_checks p WHERE p.platform=s.platform AND p.id=s.id AND p.state='running')
            ORDER BY s.rowid LIMIT 16""").fetchall()
    for row in rows:
        try:
            _remove_scratch(row['id'])
        except (OSError, ValueError):
            logging.getLogger(__name__).warning('Could not clean registered pool check directory')


def recover(platform=None):
    # On startup, or when this platform's sole worker is idle after a failed checkpoint.
    query = "SELECT id FROM admin_pool_checks WHERE state='running'"
    parameters = ()
    if platform is not None:
        query += ' AND platform=?'
        parameters = (platform,)
    with c.db() as db:
        rows = db.execute(query, parameters).fetchall()
    for row in rows:
        c.retry_write(lambda db: db.execute("""UPDATE admin_pool_checks SET state='interrupted',url='',finished=?,detail=?
            WHERE id=? AND state='running'""",
            (time.time(), 'The check was interrupted. Submit a new check to retry.', row['id'])))
    cleanup_pending()


def run_next(platform):
    def claim(db):
        row = db.execute("SELECT * FROM admin_pool_checks WHERE platform=? AND state='queued'",
                         (platform,)).fetchone()
        if row:
            db.execute("UPDATE admin_pool_checks SET state='running',started=? WHERE id=?",
                       (time.time(), row['id']))
            return dict(row)

    check = c.retry_write(claim)
    if not check:
        return False
    if check['requested'] < time.time() - QUEUE_TTL:
        state, detail = 'skipped', 'The check expired while waiting for the platform worker.'
    else:
        try:
            state, detail = _run(check)
        except Exception:
            state, detail = 'failed', 'The worker could not complete the check.'
    c.retry_write(lambda db: db.execute("""UPDATE admin_pool_checks SET state=?,url='',detail=?,finished=?
        WHERE platform=? AND id=? AND state='running'""", (state, detail, time.time(), platform, check['id'])))
    return True


def _current(platform, selected):
    pool = c.rpc(c.SECRET_SOCKET, '/accounts')[platform]
    item = next((item for item in pool['items'] if item['id'] == selected.get('id')), None)
    return pool, item


def _unavailable(pool):
    items = pool['items']
    if not items:
        return 'No credentials are configured.'
    if pool['policy'] == 'primary':
        items = [item for item in items if item['id'] == pool['primary']]
        if not items:
            return 'Select a primary account first.'
    enabled = [item for item in items if item['enabled']]
    if not enabled:
        return 'All eligible accounts are disabled.'
    if all(item.get('last_result') == 'auth_required' for item in enabled):
        return 'Sign in to the platform again and update the cookies.'
    return 'No eligible account has cookies.'


def _eligible(selected, item):
    return (item and item['enabled'] and item.get('cookie_set') and
            item['revision'] == selected['revision'])


def _until(timestamp):
    return datetime.fromtimestamp(timestamp, ZoneInfo('Asia/Hong_Kong')).strftime('%m-%d %H:%M:%S')


def _run(check):
    from . import worker

    platform = check['platform']
    if frozen() or not c.get_setting('enabled', False):
        return 'skipped', FORM_ERRORS['disabled']
    if not features.download_enabled(platform):
        return 'skipped', FORM_ERRORS['feature']
    until = c.get_setting(platform + '_cooldown', 0)
    if until > time.time():
        return 'skipped', 'The platform is cooling down. Retry after ' + _until(until) + ' HKT.'
    try:
        storage.check(download=True)
        worker.disk_ok()
    except (OSError, ValueError) as error:
        if isinstance(error, worker.CaptureStorageError) or error.args == ('SSD 可用空間不足 5 GiB；保留工作',):
            return 'skipped', 'Free space is below the 5 GiB reserve.'
        if error.args == ('Bot 暫存已達 3 GiB，請管理員處理未交付工作',):
            return 'skipped', 'Pending downloads have reached the 3 GiB working limit.'
        return 'skipped', 'The service filesystem or temporary directory is unavailable.'

    try:
        selected = c.rpc(c.SECRET_SOCKET, '/select', {'platform': platform, 'id': ''})
        pool, item = _current(platform, selected)
    except (OSError, ValueError, KeyError):
        return 'failed', 'The credential broker is unavailable.'
    public = (platform in ('threads', 'youtube') and selected.get('public')
              and not selected.get('cookies') and not pool['items'])
    if public:
        label, revision = PUBLIC_LABEL, None
    elif not selected.get('cookies'):
        return 'skipped', _unavailable(pool)
    elif not _eligible(selected, item):
        return 'skipped', 'Account or cookie settings changed. Run the check again.'
    else:
        label, revision = item['label'], selected['revision']
        if sum(account['label'] == label for account in pool['items']) > 1:
            label += ' (account …' + item['id'][-8:] + ')'
    with c.db() as db:
        db.execute("UPDATE admin_pool_checks SET account_label=?,revision=? WHERE id=? AND state='running'",
                   (label, revision, check['id']))

    # A test uses the normal adapter and rate budgets, without a Telegram job or delivery.
    root = _scratch(check['id'])
    with c.db() as db:
        db.execute('INSERT INTO admin_pool_check_scratch(id,platform) VALUES (?,?)', (check['id'], platform))
        root.mkdir(mode=0o700)
    try:
        job = {'id': check['id'], 'platform': platform, 'url': _post(platform, check['url']), 'mode': 'post'}
        deadline = time.monotonic() + CAPTURE_TIMEOUT
        result = worker.capture(job, selected.get('cookies', {}), root=root,
                                should_cancel=lambda: frozen() or not c.get_setting('enabled', False),
                                timeout=CAPTURE_TIMEOUT)
        if result is None:
            return 'interrupted', FORM_ERRORS['disabled']
        outcome = _outcome(result, root, public=bool(public), deadline=deadline)
        try:
            pool, item = _current(platform, selected)
        except (OSError, ValueError, KeyError):
            return 'failed', 'Account settings could not be verified after the check.'
        if (public and pool['items']) or (not public and not _eligible(selected, item)):
            return 'skipped', 'Account settings changed during the check. Try again.'
        return outcome
    except (TimeoutError, worker.CaptureTimeout):
        return 'failed', 'The download exceeded the two-minute timeout.'
    except worker.CaptureStorageError:
        return 'failed', 'Free space fell below the 5 GiB reserve during the check.'
    finally:
        _remove_scratch(check['id'])


FAILURES = {
    'login_required': 'The platform requires sign-in or account verification.',
    'forbidden': 'The platform refused access to this post.',
    'unavailable': 'The post was deleted or is unavailable.',
    'challenge': 'The platform requested an additional verification step.',
    'bot_verification': 'The platform requested human verification.',
    'region_restricted': 'This content is region-restricted.',
    'network_error': 'The platform connection failed or timed out.',
    'conversion_failed': 'Video conversion failed.',
    'unsupported_media': 'This media format is unsupported.',
    'unsupported_video': 'This video format is unsupported.',
    'live_unsupported': 'The video is live, upcoming, or still processing.',
    'storage_error': 'The download filesystem check failed.',
    'too_large': 'The file exceeds the 2000 MB download limit.',
    'empty_unconfirmed': 'The platform did not supply a verifiable list of media.',
    'extractor_error': 'The downloader could not parse the platform response.',
}


def _has_visual_stream(path, demuxer, timeout):
    # Pin a local demuxer just as the delivery worker does for video metadata.
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-f', demuxer,
         '-threads', '1', '-max_pixels', '67108864', '-i', str(path), '-select_streams', 'V:0',
         '-read_intervals', '%+#32', '-count_frames', '-show_entries', 'stream=width,height,nb_read_frames',
         '-of', 'json'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=timeout)
    if probe.returncode:
        return False
    streams = json.loads(probe.stdout).get('streams', [])
    return bool(streams and streams[0].get('width', 0) > 0 and streams[0].get('height', 0) > 0
                and str(streams[0].get('nb_read_frames', '')).isdecimal() and int(streams[0]['nb_read_frames']) > 0)


def _outcome(result, root, *, public, deadline):
    diagnostic = result.get('instagram_diagnostic') or {}
    status = diagnostic.get('status')
    redirect = diagnostic.get('redirect')
    if result.get('retry_at'):
        state, reason = 'skipped', 'The platform requested a cooldown'
        if result.get('wait_reason') == 'quota':
            reason = 'The per-minute media limit was reached'
        elif type(status) is int and 400 <= status <= 599:
            state = 'skipped' if status == 429 else 'failed'
            reason = f'Instagram returned HTTP {status} and entered cooldown'
        elif 429 in (result.get('http_errors') or []):
            reason = 'The platform returned HTTP 429'
        return state, reason + '. Retry after ' + _until(result['retry_at']) + ' HKT.'
    if type(status) is int and 300 <= status < 400:
        target = {'home': 'home page', 'login': 'login page', 'verification': 'verification page'}.get(redirect)
        if target:
            source = 'home page' if diagnostic.get('endpoint') == 'home' else 'request'
            return 'failed', f'Instagram {source} returned HTTP {status} and redirected to the {target}.'
        return 'failed', f'Instagram redirected the request (HTTP {status}).'
    if type(status) is int and 400 <= status <= 599:
        return 'failed', f'Instagram returned HTTP {status}.'
    if result.get('oversize'):
        return 'failed', FAILURES['too_large']
    if result.get('auth_required'):
        return 'failed', FAILURES['login_required']
    if result.get('failure') in FAILURES:
        return 'failed', FAILURES[result['failure']]
    if result.get('no_media'):
        return 'failed', 'The post has no images or videos.'
    files = result.get('files') or []
    if result.get('code') != 0 or result.get('error') or result.get('failure') or result.get('partial'):
        if files:
            return 'failed', 'Only part of the post was downloaded.'
        errors = result.get('http_errors') or []
        status = next((v for v in reversed(errors) if v in (401, 403, 404, 429)), None)
        return 'failed', (f'The platform returned HTTP {status}.' if status else 'The platform did not provide more information about this failure.')
    if not files:
        return 'failed', 'The downloader produced no verifiable media.'
    if bool(result.get('cookie_used')) != (not public):
        return 'failed', 'Cookie use did not match the selected account.'
    for entry in files:
        path = Path(entry['path'])
        demuxer = PROBE_FORMATS.get(path.suffix.lower())
        if (path.parent != root or path.is_symlink() or not path.is_file()
                or not demuxer
                or not (0 < path.stat().st_size == entry.get('size'))):
            return 'failed', 'The downloaded file is incomplete or could not be verified.'
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            if not _has_visual_stream(path, demuxer, min(remaining, 10)):
                return 'failed', 'The downloaded file is not a recognized image or video.'
        except FileNotFoundError:
            return 'failed', 'Install ffprobe to verify media.'
        except subprocess.TimeoutExpired:
            return 'failed', 'Media verification timed out.'
    return 'passed', f'Downloaded and verified {len(files)} media files.'
