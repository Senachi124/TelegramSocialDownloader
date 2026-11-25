"""Release queue metadata after processing. File leases follow the delivery lifetime."""
import re
import time

from . import core as c
from .limits import DIRECT_RETENTION_SECONDS


def _delete_manifests(job):
    if not re.fullmatch('[a-f0-9]{16}', job):
        return
    root = c.safe_file(c.DATA / 'media' / job)
    if not root.is_dir():
        return
    folders = [root]
    folders.extend(path for path in root.glob('item-*') if re.fullmatch(r'item-\d+', path.name)
                   and not path.is_symlink() and path.is_dir())
    for folder in folders:
        for pattern in ('result.json', 'result.tmp', '*.receipt.json', '*.format.json'):
            for marker in folder.glob(pattern):
                if marker.is_file() and not marker.is_symlink():
                    marker.unlink(missing_ok=True)


def _forget_notes(db, key):
    db.executemany('DELETE FROM settings WHERE key=?',
                   [(prefix + key,) for prefix in ('preference_notice:', 'partial:', 'partial_reason:')])


def _retire(db, job, now):
    key = job['id']
    files = db.execute('SELECT * FROM files WHERE job=?', (key,)).fetchall()
    for file in files:
        live = db.execute('SELECT 1 FROM links WHERE file=? AND expires>?', (file['id'], now)).fetchone()
        if live:
            # The download endpoint needs the recipient for revocation, filename and path.
            db.execute("UPDATE files SET telegram_id=NULL, telegram_media_kind='' WHERE id=?", (file['id'],))
            continue
        db.execute('DELETE FROM links WHERE file=?', (file['id'],))
        if job['state'] in ('completed', 'delivered', 'link_active') and file['state'] == 'sent':
            expires = file['link_retention_until'] or (job['delivered'] or now) + DIRECT_RETENTION_SECONDS
            db.execute('''INSERT INTO media_expiry VALUES (?,?) ON CONFLICT(path)
                          DO UPDATE SET expires=max(expires,excluded.expires)''', (file['path'], expires))
        # Unconfirmed local originals are never scheduled for deletion.
        db.execute('DELETE FROM files WHERE id=?', (file['id'],))

    _delete_manifests(key)
    for table in ('batch_items', 'batch_requests'):
        db.execute('DELETE FROM ' + table + ' WHERE job=?', (key,))
    _forget_notes(db, key)
    if db.execute('SELECT 1 FROM files WHERE job=?', (key,)).fetchone():
        db.execute("""UPDATE jobs SET state='link_active',url='',update_key=NULL,platform='',mode='',
                      message='',status_id=NULL,bridge_id=NULL,account_id='',created=NULL,delivered=NULL,
                      group_chat_id=NULL,original_message_id=NULL,message_thread_id=NULL,preferences='{}'
                      WHERE id=?""", (key,))
    else:
        db.execute('DELETE FROM jobs WHERE id=?', (key,))


def _retire_batch():
    now = time.time()
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM links WHERE expires<=?', (now,))
        # No source content is needed to reconcile a previously attempted send.
        # Keep pending item inputs if work is still queued; keep receipt and file
        # checkpoints for ambiguity. An active inbox key still protects a replay.
        held = db.execute("""SELECT j.id FROM jobs j WHERE j.state IN
            ('partial','failed','cancelled','uncertain','expired','delivered','completed')
            AND (j.url!='' OR j.update_key IS NOT NULL)
            AND (EXISTS(SELECT 1 FROM files WHERE job=j.id AND state='uncertain')
                 OR EXISTS(SELECT 1 FROM batch_items WHERE job=j.id AND error_notified=1)
                 OR (j.state IN ('partial','failed') AND EXISTS(
                   SELECT 1 FROM files WHERE job=j.id AND state='pending')))
            AND NOT EXISTS(SELECT 1 FROM batch_items WHERE job=j.id AND state='pending')
            AND NOT EXISTS(SELECT 1 FROM telegram_inbox i WHERE i.state IN ('pending','working')
                           AND j.update_key LIKE i.id||':%') LIMIT 32""").fetchall()
        for row in held:
            key = row['id']
            db.execute("""UPDATE jobs SET url='',update_key=NULL,message='',bridge_id=NULL,
                        preferences='{}',account_id='' WHERE id=?""", (key,))
            db.execute("UPDATE batch_items SET original_url='',url='',error='' WHERE job=?", (key,))
            _forget_notes(db, key)
            _delete_manifests(key)
        jobs = db.execute("""SELECT j.* FROM jobs j
            WHERE (j.state IN ('completed','delivered','expired','failed','cancelled','partial') OR
                   (j.state='link_active' AND EXISTS(SELECT 1 FROM files f WHERE f.job=j.id
                    AND NOT EXISTS(SELECT 1 FROM links WHERE file=f.id))))
            AND NOT EXISTS(SELECT 1 FROM files f WHERE f.job=j.id AND
                (f.state='uncertain' OR (j.state IN ('failed','partial') AND f.state='pending')))
            AND NOT EXISTS(SELECT 1 FROM batch_items b WHERE b.job=j.id AND b.error_notified=1)
            AND NOT EXISTS(SELECT 1 FROM telegram_inbox i WHERE i.state IN ('pending','working')
                           AND j.update_key LIKE i.id||':%')
            LIMIT 32""").fetchall()
        for job in jobs:
            _retire(db, job, now)
        return len(held) + len(jobs)


def _expire_files():
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute('SELECT path FROM media_expiry WHERE expires<=? LIMIT 64', (time.time(),)).fetchall():
            path = c.safe_file(row['path'])
            if not db.execute('SELECT 1 FROM files WHERE path=?', (str(path),)).fetchone():
                path.unlink(missing_ok=True)
                parent = path.parent
                while parent != c.DATA / 'media':
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            db.execute('DELETE FROM media_expiry WHERE path=?', (str(path),))


def sweep():
    now = time.time()
    with c.db() as db:
        db.execute("UPDATE jobs SET state='cancelled',message='' WHERE state='choice' AND created<?", (now-86400,))
        db.execute('DELETE FROM media_budget WHERE time<?', (now-60,))
    _retire_batch()
    _expire_files()


def migrate():
    directory = c.DATA / 'tmp' / 'llm-trace'
    if directory.exists():
        if directory.resolve() != directory:
            raise ValueError('Invalid temporary directory')
        for suffix in ('', '-wal', '-shm', '-journal'):
            (directory / ('traces.sqlite3' + suffix)).unlink(missing_ok=True)
    with c.db() as db:
        db.execute("DELETE FROM telegram_inbox WHERE state='done'")
        db.execute("UPDATE telegram_inbox SET payload='',chat=0 WHERE state='uncertain'")
    while _retire_batch():
        pass
    with c.db() as db:
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
