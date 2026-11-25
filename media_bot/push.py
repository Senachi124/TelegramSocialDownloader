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
    bundled_uid = os.environ.get('MEDIA_BOT_TAKANEKO_UID', '')
    if bundled_uid:
        if not re.fullmatch(r'[1-9][0-9]{0,9}', bundled_uid) or int(bundled_uid) >= 2**32 - 1:
            raise ValueError('Invalid Takaneko service UID')
        if 'takaneko' in configured and configured['takaneko'].get('uid') != int(bundled_uid):
            raise ValueError('Conflicting Takaneko service UIDs')
        configured.setdefault('takaneko', {'uid': int(bundled_uid),
                                          'control': '/run/telegram-social-takaneko/control.sock'})
    uids = set()
    for slug, entry in configured.items():
        if (not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', slug) or
                type(entry.get('uid')) is not int or entry['uid'] < 1 or entry['uid'] in uids or
                not Path(entry['control']).is_absolute()):
            raise ValueError('Invalid push provider configuration')
        uids.add(entry['uid'])
    return configured


class FanclubSessionRequired(ValueError):
    pass


def control(entry, operation, **fields):
    reply = push_socket.exchange(entry['control'], entry['uid'], {'op': operation, **fields})
    if reply.get('ok') is not True:
        if operation == 'set_enabled' and reply.get('error') == 'session_required':
            raise FanclubSessionRequired('Save a Fanclub session before enabling notifications.')
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
    from .processing.telegram_api import telegram, TelegramError
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
    from .admin import views as layout

    configured = providers()
    if not configured:
        return ('<section><h2>No subscription providers configured</h2>'
                '<p>To send Fanclub updates, install the optional Takaneko provider on your server.</p></section>')

    with core.db() as db:
        users = {uid: (name or 'Unnamed user', state)
                 for uid, name, state in db.execute('SELECT id,name,state FROM bot_users')}
    eligible = {uid for uid, (_, state) in users.items() if state in ('owner', 'approved')}
    sections = []
    for slug, entry in configured.items():
        title = 'Takaneko Fanclub' if slug == 'takaneko' else slug
        section = f'<section id="provider-{slug}"><h2>{html.escape(title)}</h2>'
        try:
            listing = control(entry, 'list')
            subscribers = listing['users']
            if not isinstance(subscribers, list) or any(type(uid) is not int or not 0 < uid < 2**63 for uid in subscribers):
                raise ValueError('Invalid subscriber list')
            subscribed = set(subscribers)
        except (OSError, ValueError, KeyError):
            sections.append(section + '<p>Subscriber list unavailable. Check the provider service and reload this page.</p></section>')
            continue

        health = core.get_setting('push_health.' + slug, 'ok')
        if health in HEALTH and health != 'ok':
            section += f'<p class="state warn">{html.escape(HEALTH[health])}</p>'
        provider = hidden + layout.hidden_field('provider', slug)
        if slug == 'takaneko' and 'set_session' in listing.get('capabilities', []):
            section += '<h3>Fanclub session</h3>'
            controls = layout.field('session', 'Session or browser cookies (JSON)', 'password',
                                    attrs='required autocomplete="new-password" spellcheck="false"')
            controls += layout.button('Save Fanclub session', 'set_session')
            section += layout.form('subscriptions', provider, controls)
            section += f'<p>Enable or pause Fanclub updates in <a href="{core.BASE}features#takaneko">Features</a>.</p>'

        section += '<h3>Recipients</h3>'
        if not eligible and not subscribed:
            section += f'<p>Approve a user in <a href="{core.BASE}users#user-access">Users &amp; groups</a> to add a recipient.</p>'
        for uid in sorted(eligible | subscribed, key=lambda uid: (uid not in subscribed, uid)):
            name, state = users.get(uid, ('Unregistered user', 'unregistered'))
            active = uid in subscribed
            if active and uid not in eligible:
                reason = {'revoked': 'access revoked', 'rejected': 'access rejected'}.get(state, 'approval required')
                subscription, tone = f'Subscribed · {reason}', ' warn'
            elif active:
                subscription, tone = 'Subscribed', ' on'
            else:
                subscription, tone = 'Not subscribed', ''
            verb = 'Unsubscribe' if active else 'Subscribe'
            fields = provider + layout.hidden_field('user_id', uid) + layout.hidden_field('enabled', int(not active))
            action = layout.button(verb, kind='secondary' if active else '',
                                   aria_label=f'{verb} {name} ({uid}) · {title}')
            section += (f'<div class="record"><div class="record-info"><strong>{html.escape(name)}</strong>'
                        f'<p class="muted">Telegram ID {uid}</p></div>'
                        f'<span class="state{tone}">{html.escape(subscription)}</span>'
                        + layout.form('subscriptions', fields, action) + '</div>')
        sections.append(section + '</section>')
    return ''.join(sections)


def web_post(form):
    entry = providers().get(form.get('provider'))
    if form.get('action') == 'set_session':
        if entry is None or form.get('provider') != 'takaneko':
            raise ValueError('Unknown session provider')
        session = json.loads(form.get('session', ''))
        if isinstance(session, list):
            session = {'cookies': session}
        if not isinstance(session, dict) or not session:
            raise ValueError('Supply a Fanclub session or cookie array')
        control(entry, 'set_session', session=session)
        return
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
