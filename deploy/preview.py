"""Queue the installed release announcement for the configured Telegram owner."""
import argparse
import hashlib
from importlib import resources
import json
import re
import time


def render(template):
    version = template['version']
    changes = template['changes']
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('Invalid notice version')
    if not isinstance(changes, list) or not 1 <= len(changes) <= 20:
        raise ValueError('Provide one to twenty changes')
    if any(not isinstance(s, str) or not s or s != s.strip() or not s.isprintable() for s in changes):
        raise ValueError('Each change must be a single nonempty line')
    message = '\n'.join([f'v{version} 部署完成', *(f'{i}. {s}' for i, s in enumerate(changes, 1))])
    if len(message.encode('utf-16-le')) > 7000:
        raise ValueError('Release announcement exceeds the size limit')
    return version, message


def event_id(version, message):
    return f'release-preview:{version}:{hashlib.sha256(message.encode()).hexdigest()}'


def enqueue(core, version, message):
    event = event_id(version, message)
    digest = event.rsplit(':', 1)[1]
    now = time.time()
    with core.db() as db:
        db.execute('BEGIN IMMEDIATE')
        owners = db.execute("SELECT id FROM bot_users WHERE state='owner'").fetchall()
        if len(owners) != 1 or owners[0][0] <= 0:
            raise ValueError('Exactly one configured owner is required')
        record = dict(version=version, text=message, sha256=digest, status='awaiting_owner_approval',
                      owner_id=owners[0][0], preview_event=event, created=now, broadcast_authorized=False)
        db.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)',
                   (f'release_announcement:{version}:{digest}', json.dumps(record, ensure_ascii=False)))
        payload = json.dumps(dict(text=message, disable_web_page_preview=True), ensure_ascii=False)
        db.execute('INSERT OR IGNORE INTO release_preview_outbox(event_key,payload,created) VALUES (?,?,?)',
                   (event, payload, now))


def status(core, event):
    with core.db() as db:
        row = db.execute('SELECT state,message_id FROM release_preview_outbox WHERE event_key=?', (event,)).fetchone()
    if row is None:
        return 'not_queued'
    return 'uncertain' if row[0] == 'sent' and not row[1] else row[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag', help='installed release tag')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--apply', action='store_true', help='enqueue the owner preview once')
    action.add_argument('--status', action='store_true', help='read the delivery checkpoint')
    parser.add_argument('--wait', type=int, default=0, metavar='SECONDS')
    args = parser.parse_args()
    if not 0 <= args.wait <= 600:
        parser.error('--wait must be between 0 and 600 seconds')
    from media_bot import __version__, core
    template = json.loads(resources.files('media_bot').joinpath('release_notice.json').read_text(encoding='utf-8'))
    version, message = render(template)
    if args.tag != 'v' + version or version != __version__:
        parser.error('Announcement must match the installed release')
    if args.apply:
        enqueue(core, version, message)
    event = event_id(version, message)
    deadline = time.monotonic() + args.wait
    state = status(core, event)
    while state == 'pending' and time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
        state = status(core, event)
    print(json.dumps({'owner_preview': state}))
    return 0 if state == 'sent' or (not args.wait and state == 'pending') else 1


if __name__ == '__main__':
    raise SystemExit(main())
