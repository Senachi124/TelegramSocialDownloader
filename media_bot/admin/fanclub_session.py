"""Fanclub login instructions and feedback from the saved-session connection test."""
import hmac
import re
from html import escape

from .. import push
from . import views
from .forms import FormError


RESULTS = {
    'connected': 'Connected. The saved login could read the protected Fanclub notification count.',
    'no_session': 'Save a refresh token before testing the connection.',
    'unauthorized': 'Fanclub rejected the saved login. Sign in again and save a fresh refresh token.',
    'forbidden': 'Fanclub denied this account access to the protected notification count.',
    'rate_limited': 'Fanclub is limiting requests. Try again later.',
    'network': 'The provider could not reach Fanclub or receive its response in time.',
    'invalid': 'The provider could not validate the saved login or the Fanclub response.',
    'upstream': 'Fanclub returned an unexpected HTTP error. Try again later.',
    'busy': 'The provider is already using the saved login. Try again in a moment.',
    'unavailable': 'The provider could not confirm the connection. Check the provider service and try again.',
    'changed': 'The saved login changed during the test. Test the current login again.',
}


def login_panel(provider):
    field = views.field('refresh_token', 'Refresh token', 'password',
                        attrs='required maxlength="16384" autocomplete="new-password" spellcheck="false" '
                              'aria-describedby="fanclub-login-help"')
    help_text = ('<p id="fanclub-login-help" class="muted">On the signed-in Fanclub site, open DevTools → '
                 'Application → Local Storage and copy the value of <code>refreshToken</code>. '
                 'Saving replaces the current login.</p>')
    return '<h3>Fanclub login</h3>' + views.form('subscriptions', provider,
                field + help_text + views.button('Save refresh token', 'set_session'))


def save_refresh_token(entry, form):
    token = form.get('refresh_token', '')
    if (form.keys() - {'csrf', 'provider', 'action', 'refresh_token'} or not isinstance(token, str)
            or not re.fullmatch(r'[!-~]{1,16384}', token) or token.startswith(('{', '[', '"'))
            or re.match(r'(?:refreshToken[=:]|Cookie:)', token, re.IGNORECASE)):
        raise FormError('Paste the refreshToken value from Fanclub Local Storage.')
    push.control(entry, 'set_session', session={'refreshToken': token})


def connection_panel(hidden, supported, result):
    if not supported and not result:
        return ''
    body = '<div id="fanclub-connection" tabindex="-1">'
    if supported:
        body += views.form('fanclub-test', hidden,
                '<p class="muted">Test the saved login against the protected Fanclub notification count.</p>'
                + views.button('Test connection', kind='secondary'))
    if result:
        role = 'status' if result == 'connected' else 'alert'
        body += f'<p class="test-result" role="{role}">{escape(RESULTS[result])}</p>'
    return body + '</div>'


def test_saved(form):
    if set(form) != {'csrf'}:
        raise FormError('The connection test accepts only the saved Fanclub login.')
    try:
        entry = push.providers().get('takaneko')
        if entry is None:
            return 'unavailable'
        reply = push.control(entry, 'test_session')
        result = reply.get('result')
        if reply.get('ok') is True and isinstance(result, str) and result in RESULTS:
            return result
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    return 'unavailable'


def result_cookie(result, csrf):
    signature = hmac.digest(csrf.encode(), result.encode(), 'sha256').hex()
    return result + '.' + signature


def read_result(cookie, csrf):
    result = cookie.partition('.')[0]
    if result in RESULTS and cookie.isascii() and hmac.compare_digest(cookie, result_cookie(result, csrf)):
        return result
    return None
