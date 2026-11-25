"""Bot delivery for subscribed users. Each producer maintains its own queue and subscriptions."""
import html
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import time

import requests

from . import access, core, error_log, limits, push_socket


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
    'uncertain': 'A Telegram send has no saved delivery receipt.',
    'format': 'The producer could not process the content.',
    'paused': 'Processing is paused and requires attention.',
}


def report_health(slug, code):
    if not isinstance(code, str) or (code not in HEALTH and not
            (slug == 'takaneko' and (code in error_log.FANCLUB or error_log.provider_http_status(code)))):
        return {'ok': False, 'code': 400}
    key = 'push_health.' + slug
    value = json.dumps(code)
    with core.db() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        if previous and previous[0] == value:
            return {'ok': True}
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))
        if code != 'ok':
            try:
                error_log.record_health(db, slug, code, time.time())
            except Exception as error:
                logging.getLogger(__name__).warning('Provider diagnostic unavailable (%s)', type(error).__name__)
        enabled = db.execute("SELECT value FROM settings WHERE key='failure_alerts'").fetchone()
        if slug != 'takaneko' and code != 'ok' and (not enabled or enabled[0] == 'true'):
            db.execute('INSERT INTO admin_alert_queue(reason,platform) VALUES (?,?)', (code, 'push_provider'))
    return {'ok': True}


def dispatch(entry, request, fd, send=None, slug=None):
    from .processing.telegram_api import telegram, TelegramError, InvalidTelegramResponse
    if request.get('op') == 'health' and fd is None and slug:
        return report_health(slug, request.get('status'))
    if request.get('op') == 'limits' and fd is None:
        return {'ok': True, 'file_limit': limits.telegram_file_limit()}
    chat = request.get('user_id')
    if request.get('op') == 'review_allowed':
        # The provider rechecks its subscription in the resolution transaction.
        # Calling its control socket from this request would recurse into the same worker pool.
        if slug != 'takaneko' or fd is not None or request.keys() != {'op', 'user_id'}:
            return {'ok': False, 'code': 403}
        return {'ok': True} if access.allowed(chat) else {'ok': False, 'code': 403}
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
        if not isinstance(reply, dict) or type(reply.get('message_id')) is not int or reply['message_id'] <= 0:
            error_log.record_send(slug, 'send_receipt')
            return {'ok': False, 'error': 'uncertain'}
        return {'ok': True, 'message_id': reply['message_id']}
    except TelegramError as error:
        if type(error.http_status) is int and 400 <= error.http_status <= 599:
            error_log.record_send(slug, 'send_http', error.http_status)
        elif error.diagnostic:
            error_log.record_send(slug, error.diagnostic)
        else:
            error_log.record_send(slug, 'send_api', error.code)
        if 400 <= error.code < 500:
            return {'ok': False, 'code': error.code, 'retry_after': error.retry}
        return {'ok': False, 'error': 'uncertain'}
    except InvalidTelegramResponse as error:
        if type(error.http_status) is int and 400 <= error.http_status <= 599:
            error_log.record_send(slug, 'send_http', error.http_status)
        else:
            error_log.record_send(slug, 'send_response')
        raise
    except (requests.Timeout, TimeoutError):
        error_log.record_send(slug, 'send_timeout')
        raise
    except (requests.ConnectionError, ConnectionError):
        error_log.record_send(slug, 'send_connection')
        raise
    except Exception:
        error_log.record_send(slug, 'send_failure')
        raise


def web_panel(hidden, test_result=None):
    from .admin import fanclub_session, views as layout

    configured = providers()
    if not configured:
        return ('<section><h2>No subscription providers configured</h2>'
                + fanclub_session.connection_panel(hidden, False, test_result) +
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
            if slug == 'takaneko':
                section += fanclub_session.connection_panel(hidden, False, test_result)
            sections.append(section + '<p>Subscriber list unavailable.</p></section>')
            continue

        caps = listing.get('capabilities', [])
        if not isinstance(caps, list):
            caps = []
        provider = hidden + layout.hidden_field('provider', slug)
        if slug == 'takaneko':
            if 'set_session' in caps:
                section += fanclub_session.login_panel(provider)
            section += fanclub_session.connection_panel(hidden, 'test_session' in caps, test_result)
            if 'set_session' in caps:
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
    if form.get('action'):
        raise ValueError('Unknown subscription action')
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
