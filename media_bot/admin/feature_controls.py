"""Render and apply the administrator's feature switches."""
from html import escape

from .. import accounts, core, features, push
from . import views
from .forms import FormError, ServiceUnavailable


def _row(hidden, key, label, state, action=None, detail=''):
    control = ''
    if action is not None:
        verb = 'Enable' if action else 'Pause'
        fields = hidden + views.hidden_field('feature', key) + views.hidden_field('enabled', int(action))
        control = views.form('features', fields, views.button(verb, kind='' if action else 'secondary',
                                                             aria_label=f'{verb} {label}'))
    tone = ' on' if state == 'Enabled' else (' warn' if state == 'Unavailable' else '')
    return (f'<div class="feature-row" id="{key}" tabindex="-1"><div class="feature-copy">'
            f'<h3>{escape(label.removesuffix(" downloads"))}</h3>{detail}</div>'
            f'<span class="state{tone}">{escape(state)}</span>{control}</div>')


def _fanclub_state(entry, operation, **fields):
    reply = push.control(entry, operation, **fields)
    if (reply.get('ok') is not True or type(reply.get('enabled')) is not bool
            or 'session_configured' not in reply
            or (reply['session_configured'] is not None and type(reply['session_configured']) is not bool)):
        raise ValueError('Invalid Fanclub status')
    return reply['enabled'], reply['session_configured']


def _fanclub_entry():
    try:
        return push.providers().get('takaneko')
    except (ValueError, AttributeError, KeyError, TypeError):
        raise ServiceUnavailable('Check the Takaneko provider configuration.') from None


def _fanclub_row(hidden):
    label = 'Takaneko Fanclub'
    try:
        entry = _fanclub_entry()
        if entry is None:
            return _row(hidden, 'takaneko', label, 'Not installed')
        enabled, session = _fanclub_state(entry, 'feature_status')
    except (ValueError, OSError):
        return _row(hidden, 'takaneko', label, 'Unavailable',
                    detail='<p>Check the provider service and reload this page.</p>')

    detail = ''
    if session is False:
        detail = f'<p><a href="{core.BASE}subscriptions">Add a refresh token in Takaneko subscriptions</a>.</p>'
    elif session is None:
        detail = '<p>The Fanclub credential service is unavailable.</p>'
    if enabled:
        state = 'Enabled · waiting for a Fanclub login' if session is False else 'Enabled'
        return _row(hidden, 'takaneko', label, state, False, detail)
    return _row(hidden, 'takaneko', label, 'Paused', True if session else None, detail)


def panel(hidden):
    flags = features.snapshot()
    downloads = []
    for platform in accounts.DOWNLOAD_PLATFORMS:
        key = 'download_' + platform
        enabled = flags[key]
        downloads.append(_row(hidden, key, features.LABELS[key],
                              'Enabled' if enabled else 'Paused', not enabled))
    translation = flags['translation']
    bot_status = ''
    if not core.get_setting('enabled', False):
        bot_status = f'<p class="callout">Telegram polling is paused. <a href="{core.BASE}#bot-operation">Manage the bot connection</a>.</p>'
    return (bot_status + '<section><h2>Media downloads</h2>'
            '<p>Pausing a platform holds its queued downloads. An item already in progress can finish.</p>'
            + ''.join(downloads) + '</section><section><h2>Telegram tools</h2>'
            + _row(hidden, 'translation', features.LABELS['translation'], 'Enabled' if translation else 'Paused',
                   not translation, '<p>Translate text and read text from images in chats.</p>')
            + '</section><section><h2>Subscription services</h2>'
            + _fanclub_row(hidden) + '</section>')


def update(form):
    key = form.get('feature')
    value = form.get('enabled')
    if value not in ('0', '1'):
        raise FormError('Choose Enable or Pause for one feature.')
    enabled = value == '1'
    if key == 'takaneko':
        entry = _fanclub_entry()
        if entry is None:
            raise FormError('Install the Takaneko provider to use this feature.')
        try:
            confirmed, _ = _fanclub_state(entry, 'set_enabled', enabled=enabled)
        except push.FanclubSessionRequired:
            raise FormError('Add a refresh token in Takaneko subscriptions before enabling Takaneko.') from None
        except (OSError, ValueError):
            raise ServiceUnavailable('Could not confirm the Takaneko setting. Reload Features to check its status.') from None
        if confirmed != enabled:
            raise ServiceUnavailable('The provider did not confirm the requested setting. Reload Features to check its status.')
    elif key in features.LABELS:
        features.set_enabled(key, enabled)
    else:
        raise FormError('Unknown feature.')
