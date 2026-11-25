"""Fanclub login instructions and feedback from the saved-session connection test."""
import base64
import hashlib
import hmac
from html import escape
from importlib.resources import files

from .. import push
from . import views
from .forms import FormError


COPY_SCRIPT = """const scriptBox = document.getElementById('fanclub-export-script');
const copyButton = document.getElementById('copy-fanclub-script');
const feedback = document.getElementById('fanclub-copy-status');
copyButton.hidden = false;
copyButton.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(scriptBox.value);
    feedback.textContent = 'Script copied.';
  } catch {
    scriptBox.focus();
    scriptBox.select();
    feedback.textContent = 'Script selected. Use your browser’s Copy command.';
  }
});"""
COPY_HASH = base64.b64encode(hashlib.sha256(COPY_SCRIPT.encode()).digest()).decode('ascii')

RESULTS = {
    'connected': 'Connected. The saved login could read the protected Fanclub notification count.',
    'no_session': 'Save a Fanclub login before testing the connection.',
    'unauthorized': 'Fanclub rejected the saved login. Sign in again and save fresh login JSON.',
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
    example = files(__package__).joinpath('fanclub_export.txt').read_text(encoding='utf-8').rstrip()
    howto = ('<details><summary>Get login JSON with DevTools</summary><ol>'
             '<li>Sign in to the official Takaneko Fanclub site. Open Developer Tools on that page.</li>'
             '<li>Copy the script below and run it in the Console to open the login helper.</li>'
             '<li>In the helper, paste your <code>refreshToken</code> from Application → Local Storage, '
             'or the selected cookies from the Fanclub API’s <code>/auth/refresh</code> request. '
             'Select <strong>Copy login JSON</strong>.</li>'
             '<li>Paste that JSON in the field below and save the session.</li></ol>'
             '<label for="fanclub-export-script">DevTools script</label>'
             f'<textarea id="fanclub-export-script" rows="5" readonly spellcheck="false">{escape(example)}</textarea>'
             '<div class="actions"><button id="copy-fanclub-script" type="button" class="secondary" hidden>'
             'Copy script</button><span role="status" id="fanclub-copy-status"></span></div>'
             f'<script>{COPY_SCRIPT}</script></details>')
    controls = views.field('session', 'Fanclub login (JSON)', 'password',
                           attrs='required autocomplete="new-password" spellcheck="false" aria-describedby="fanclub-login-help"')
    controls += ('<p id="fanclub-login-help" class="muted">For automatic renewal, use a refresh token or cookies '
                 'that can renew the Fanclub login. An access token on its own works until it expires. '
                 'Saving replaces the current login.</p>')
    return '<h3>Fanclub session</h3>' + howto + views.form('subscriptions', provider,
                controls + views.button('Save Fanclub session', 'set_session'))


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
