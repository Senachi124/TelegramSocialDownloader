"""Bot delivery for subscribed users. Each producer maintains its own queue and subscriptions."""
import html
import json
import mimetypes
import os
from pathlib import Path
import re

from . import access, core, limits, push_socket


def providers():
    configured = json.loads(os.environ.get('MEDIA_BOT_PUSH_PROVIDERS', '{}'))
    uids = set()
    for slug, entry in configured.items():
        if (not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', slug) or
                type(entry.get('uid')) is not int or entry['uid'] < 1 or entry['uid'] in uids or
                not Path(entry['control']).is_absolute()):
            raise ValueError('Invalid push provider configuration')
        uids.add(entry['uid'])
    return configured


def control(entry, operation, **fields):
    reply = push_socket.exchange(entry['control'], entry['uid'], {'op': operation, **fields})
    if reply.get('ok') is not True:
        raise ValueError('Subscription service unavailable')
    return reply


HEALTH = {
    'ok': 'Available',
    'account': 'The producer login requires attention.',
    'storage': 'The producer storage check failed.',
    'rate_limit': 'The upstream service is limiting requests.',
    'access': 'The source denied access.',
    'network': 'The upstream service could not be reached.',
    'uncertain': 'A delivery is awaiting manual confirmation.',
    'format': 'The producer could not process the content.',
    'paused': 'Processing is paused and requires attention.',
}


def report_health(slug, code):
    if code not in HEALTH:
        return {'ok': False, 'code': 400}
    key = 'push_health.' + slug
    value = json.dumps(code)
    with core.db() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        if previous and previous[0] == value:
            return {'ok': True}
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))
        enabled = db.execute("SELECT value FROM settings WHERE key='failure_alerts'").fetchone()
        if code != 'ok' and (not enabled or enabled[0] == 'true'):
            db.execute('INSERT INTO admin_alert_queue(reason,platform) VALUES (?,?)', (code, 'push'))
    return {'ok': True}


def dispatch(entry, request, fd, send=None, slug=None):
    from .worker import telegram, TelegramError
    if request.get('op') == 'health' and fd is None and slug:
        return report_health(slug, request.get('status'))
    if request.get('op') == 'limits' and fd is None:
        return {'ok': True, 'file_limit': limits.telegram_file_limit()}
    chat = request.get('user_id')
    if not access.allowed(chat):
        return {'ok': False, 'code': 403}
    if control(entry, 'subscribed', user_id=chat).get('enabled') is not True:
        return {'ok': False, 'code': 403}
    if request.get('op') == 'allowed' and fd is None:
        return {'ok': True}
    if not core.get_setting('enabled', False):
        return {'ok': False, 'code': 403}
    if request.get('op') != 'send':
        return {'ok': False, 'code': 400}
    send = send or telegram
    kind = request.get('kind')
    data = {'chat_id': chat}
    try:
        if kind == 'text' and fd is None:
            text = request.get('text')
            if not isinstance(text, str) or not 1 <= len(text) <= 16384:
                return {'ok': False, 'code': 400}
            data.update(text=text, link_preview_options={'is_disabled': True})
            if request.get('html') is True:
                data['parse_mode'] = 'HTML'
            reply = send('sendMessage', data)
        elif kind in ('photo', 'video', 'document', 'audio', 'animation') and fd is not None:
            name = request.get('name', '')
            if (not isinstance(name, str) or not re.fullmatch(r'[^/\\\x00-\x1f\x7f"]{1,240}', name) or
                    not 0 < os.fstat(fd).st_size <= limits.telegram_file_limit()):
                return {'ok': False, 'code': 400}
            if kind == 'video':
                for key in ('width', 'height', 'duration'):
                    val = request.get(key)
                    if type(val) is int and 0 < val <= 2**31 - 1:
                        data[key] = val
                data['supports_streaming'] = True
            with os.fdopen(os.dup(fd), 'rb') as stream:
                file = (name, stream, mimetypes.guess_type(name)[0] or 'application/octet-stream')
                reply = send('send' + kind.title(), data, {kind: file})
        else:
            return {'ok': False, 'code': 400}
        if type(reply.get('message_id')) is not int:
            return {'ok': False, 'error': 'uncertain'}
        return {'ok': True, 'message_id': reply['message_id']}
    except TelegramError as error:
        if 400 <= error.code < 500:
            return {'ok': False, 'code': error.code, 'retry_after': error.retry}
        return {'ok': False, 'error': 'uncertain'}


def web_panel(hidden):
    sections = []
    with core.db() as db:
        users = list(db.execute('SELECT id,name FROM bot_users ORDER BY id'))
    for slug, entry in providers().items():
        section = '<h3>' + html.escape(slug) + '</h3>'
        try:
            subscribed = set(control(entry, 'list')['users'])
        except (OSError, ValueError, KeyError):
            sections.append(section + '<p>Subscription service unavailable.</p>')
            continue
        health = HEALTH.get(core.get_setting('push_health.' + slug, 'ok'))
        if health:
            section += '<p>' + html.escape(health) + '</p>'
        for uid, name in users:
            enabled = uid in subscribed
            section += (f'<form method="post" action="{core.BASE}subscriptions">{hidden}'
                        f'<input type="hidden" name="provider" value="{slug}">'
                        f'<input type="hidden" name="user_id" value="{uid}">'
                        f'<span>{html.escape(name or str(uid))} ({uid})</span> '
                        f'<button name="enabled" value="{int(not enabled)}">'
                        f'{"Unsubscribe" if enabled else "Subscribe"}</button></form>')
        sections.append(section)
    return '<section id="subscriptions"><h2>Subscriptions</h2>' + (''.join(sections) or '<p>No providers configured.</p>') + '</section>'


def web_post(form):
    entry = providers().get(form.get('provider'))
    uid = int(form.get('user_id') or 0)
    enabled = form.get('enabled')
    if entry is None or enabled not in ('0', '1') or uid <= 0:
        raise ValueError('Invalid subscription')
    if enabled == '1' and not access.allowed(uid):
        raise ValueError('Approve the user before enabling a subscription')
    try:
        control(entry, 'set', user_id=uid, enabled=enabled == '1')
    except (OSError, ValueError):
        raise ValueError('Subscription service unavailable') from None


def main():
    core.initialize()
    by_uid = {entry['uid']: (slug, entry) for slug, entry in providers().items()}
    path = os.environ['MEDIA_BOT_PUSH_SOCKET']
    push_socket.serve(path, by_uid, lambda uid, req, fd: dispatch(by_uid[uid][1], req, fd, slug=by_uid[uid][0]))


if __name__ == '__main__':
    main()
