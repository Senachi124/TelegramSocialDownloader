"""Server-rendered Telegram service management pages."""
from html import escape
from importlib.resources import files
from urllib.parse import urlencode

from .. import accounts, core as c, pool_checks


STYLE = files(__package__).joinpath('console.css').read_text(encoding='utf-8')
PAGES = {'': 'Bot connection', 'features': 'Features', 'users': 'Users & groups',
         'subscriptions': 'Takaneko subscriptions', 'accounts': 'Account pools', 'keys': 'API credentials'}


def page(title, body, *, signed_in=False, route=None, session_fields='', notice=''):
    title = escape(title)
    product = 'Telegram Social Downloader'
    if signed_in:
        links = []
        for path, label in PAGES.items():
            current = ' aria-current="page"' if path == route else ''
            links.append(f'<a href="{c.BASE}{path}"{current}>{escape(label)}</a>')
        signout = form('logout', session_fields, button('Sign out', kind='secondary')) if session_fields else ''
        rail = (f'<aside class="rail"><a class="product" href="{c.BASE}">{product}</a>'
                f'<nav aria-label="Service settings">{"".join(links)}</nav>{signout}</aside>')
        feedback = f'<p class="notice" role="status">{escape(notice)}</p>' if notice else ''
        content = ('<a class="skip" href="#main-content">Skip to content</a><div class="shell">' + rail
                   + f'<main id="main-content" tabindex="-1"><h1>{title}</h1>{feedback}{body}</main></div>')
    else:
        content = (f'<main class="entry"><p class="product">{product}</p>'
                   f'<div class="entry-card"><h1>{title}</h1>{body}</div></main>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="robots" content="noindex,nofollow"><title>{title} · {product}</title>'
            f'<style>{STYLE}</style></head><body>{content}</body></html>')


def field(name, label, kind='text', *, value='', attrs=''):
    return (f'<label>{label}<input type="{kind}" name="{name}" value="{escape(str(value),quote=True)}" '
            f'{attrs}></label>')


def form(action, hidden, controls, multipart=False):
    encoding = ' enctype="multipart/form-data"' if multipart else ''
    return f'<form method="post" action="{c.BASE}{action}"{encoding}>{hidden}{controls}</form>'


def button(text, action='', *, kind='', aria_label=''):
    value = f' name="action" value="{escape(action, quote=True)}"' if action else ''
    style = f' class="{kind}"' if kind in ('secondary', 'danger') else ''
    label = f' aria-label="{escape(aria_label, quote=True)}"' if aria_label else ''
    return f'<button type="submit"{style}{value}{label}>{escape(text)}</button>'


def hidden_field(name, value):
    return f'<input type="hidden" name="{name}" value="{escape(str(value),quote=True)}">'


def connection(hidden):
    token_set = c.rpc(c.SECRET_SOCKET, '/status')['token_set']
    enabled = c.get_setting('enabled', False)
    alerts = c.get_setting('failure_alerts', True)
    health = str(c.get_setting('telegram_health', 'Not checked yet'))
    health = HEALTH_LABELS.get(health, health)
    check = form('check', hidden, button('Check connection', kind='secondary')) if token_set else ''
    token_status = 'Configured' if token_set else f'<a href="#bot-token">Add bot token</a>'
    summary = ('<section id="connection-status"><div class="section-head"><h2>Connection status</h2>'
               + check + '</div><dl class="metrics"><div><dt>Polling</dt><dd>'
               + ('Enabled' if enabled else 'Paused') + '</dd></div><div><dt>Bot token</dt>'
               + f'<dd>{token_status}</dd></div><div><dt>Last API result</dt><dd>{escape(health)}</dd></div></dl></section>')
    options = (f'<label class="check"><input type="checkbox" name="enabled" value="1" {"checked" if enabled else ""}>'
               'Enable Telegram polling and processing</label>'
               f'<label class="check"><input type="checkbox" name="failure_alerts" value="1" {"checked" if alerts else ""}>'
               'Notify the owner about failures and their causes</label>')
    settings = '<section id="bot-operation"><h2>Bot operation</h2>' + form('bot-options', hidden,
                options + button('Save bot settings')) + '</section>'
    limits = []
    for platform in accounts.PLATFORMS:
        key = platform + '_media_limit'
        limits.append(field(key, accounts.LABELS[platform], 'number', value=c.get_setting(key, 20),
                            attrs='min="1" max="1000" required'))
    quota = ('<section id="media-limits"><h2>Media limits</h2><p>Media items per minute for each platform.</p>'
             + form('media-limits', hidden, '<div class="field-grid">' + ''.join(limits) + '</div>'
                    + button('Save media limits')) + '</section>')
    token_action = 'Replace bot token' if token_set else 'Save bot token'
    token = ('<section id="bot-token"><h2>Bot token</h2>' + form('bot-token', hidden,
             field('token', 'New bot token' if token_set else 'BotFather token', 'password',
                   attrs='required autocomplete="new-password" spellcheck="false"') + button(token_action)) + '</section>')
    return summary + (settings + quota + token if token_set else token + settings + quota)


HEALTH_LABELS = {
    '已連線': 'Connected', 'Awaiting first poll': 'Not checked yet',
    'Token／Webhook 設定需檢查': 'Check the bot token and webhook',
    'Telegram 暫時不可用': 'Telegram temporarily unavailable',
    '待設定或連線暫時中斷': 'Check the connection and bot settings',
}


def users(hidden, offset):
    with c.db() as db:
        rows = list(db.execute("""SELECT id,name,username,state FROM bot_users ORDER BY
          CASE state WHEN 'pending' THEN 0 WHEN 'owner' THEN 1 ELSE 2 END,id LIMIT 51 OFFSET ?""", (offset,)))
    items = []
    decisions = {'pending': ('approve', 'reject'), 'approved': ('revoke',),
                 'rejected': ('approve',), 'revoked': ('approve',)}
    for row in rows[:50]:
        uid = row['id']
        name = row['name'] or 'Unnamed user'
        handle = '@' + escape(row['username']) + ' · ' if row['username'] else ''
        state = row['state']
        state_text = 'Awaiting approval' if state == 'pending' else escape(state.capitalize())
        tone = ' warn' if state == 'pending' else ' on' if state in ('approved', 'owner') else ''
        buttons = []
        for action in decisions.get(state, ()):
            label = 'Revoke access' if action == 'revoke' else action.capitalize()
            kind = {'approve': '', 'reject': 'secondary', 'revoke': 'danger'}[action]
            buttons.append(button(label, action, kind=kind, aria_label=f'{label} · {name} ({uid})'))
        controls = ''
        if buttons:
            target = hidden + hidden_field('user_id', uid) + hidden_field('offset', offset)
            controls = form('access', target, '<div class="actions">' + ''.join(buttons) + '</div>')
        items.append(f'<div class="record"><div class="record-info"><strong>{escape(name)}</strong>'
                     f'<p class="muted">{handle}Telegram ID {uid}</p></div>'
                     f'<span class="state{tone}">{state_text}</span>{controls}</div>')

    pages = []
    if offset:
        previous = urlencode({'offset': max(0, offset - 50)})
        pages.append(f'<a rel="prev" href="{c.BASE}users?{previous}">Previous users</a>')
    if len(rows) > 50:
        following = urlencode({'offset': offset + 50})
        pages.append(f'<a rel="next" href="{c.BASE}users?{following}">Next users</a>')
    pagination = '<nav aria-label="User list pages">' + ' '.join(pages) + '</nav>' if pages else ''
    listing = ''.join(items)
    if not offset and all(row['state'] == 'owner' for row in rows):
        listing += '<p>Ask a user to send <code>/start</code> in a private chat with the bot to request access.</p>'
    elif not listing:
        listing = '<p>No users on this page.</p>'
    auto = c.get_setting('auto_approve_users', False)
    policy = (f'<label><input type="checkbox" name="enabled" value="1" {"checked" if auto else ""}> '
              'Automatically approve requests in private chats</label>' + button('Save approval policy'))
    groups = ', '.join(str(i) for i in c.get_setting('translation_groups', []))
    group_field = field('groups', 'Telegram group IDs', value=groups, attrs='aria-describedby="group-id-help"')
    revoke_note = ('<p class="muted">Revoking access cancels that user’s active downloads and invalidates their download links.</p>'
                   if any(row['state'] == 'approved' for row in rows[:50]) else '')
    return ('<section id="user-access"><h2>User access</h2>' + revoke_note
            + listing + pagination + '</section>'
            + '<section id="approval-policy"><h2>Approval policy</h2>' + form('admission', hidden, policy) + '</section>'
            + '<section id="translation-groups"><h2>Translation groups</h2>'
            + '<p id="group-id-help">Enter negative group IDs separated by commas. Clear the field to remove all groups.</p>'
            + form('groups', hidden, group_field + button('Save groups')) + '</section>')


def _credential_actions(item, primary, noun):
    label = item['label']
    if item['enabled']:
        controls = ''
        if not primary:
            controls += button('Make primary', 'primary', kind='secondary',
                               aria_label=f'Make primary — {noun} {label}')
        controls += button('Disable', 'disable', kind='secondary', aria_label=f'Disable {noun} {label}')
    else:
        controls = button('Enable', 'enable', aria_label=f'Enable {noun} {label}')
        controls += button('Delete', 'delete', kind='danger', aria_label=f'Delete {noun} {label}')
    return f'<div class="actions">{controls}</div>'


def _credential_state(enabled, primary):
    if not enabled:
        return '<span class="state">Disabled</span>'
    text = 'Enabled · Primary' if primary else 'Enabled'
    return f'<span class="state on">{text}</span>'


def _account_edit(platform, entry, item=None):
    label = accounts.LABELS[platform]
    controls = field('label', 'Account name', value=item['label'] if item else '',
                     attrs='maxlength="60" required')
    if platform == 'youtube':
        hint = 'Paste a Netscape cookies.txt export or select the file below.'
    elif platform == 'twitter':
        hint = 'Paste a Cookie header, browser cookie JSON or Netscape cookies.txt export.'
    else:
        hint = 'Paste a Cookie header or Netscape cookies.txt export.'
    if item:
        hint += ' Leave blank to keep the saved cookies.'
    required = ' required' if item is None and platform != 'youtube' else ''
    hint_id = f'cookie-format-{platform}-{item["id"] if item else "new"}'
    controls += (f'<label>Cookies<textarea name="cookie" rows="3" autocomplete="off" spellcheck="false" '
                 f'aria-describedby="{escape(hint_id,quote=True)}"{required}></textarea></label>'
                 f'<p class="muted" id="{escape(hint_id,quote=True)}">{hint}</p>')
    if platform == 'youtube':
        controls += field('youtube_cookie_file', 'Upload cookies.txt', 'file', attrs='accept=".txt,text/plain"')
    action_label = f'Save changes to {item["label"]}' if item else f'Add account for {label}'
    controls += button('Save changes' if item else 'Add account', 'save', aria_label=action_label)
    return form('account', entry, controls, multipart=(platform == 'youtube'))


_ACCOUNT_RESULTS = {
    'unknown': 'No result yet', 'success': 'Last download succeeded', 'auth_required': 'Sign in again and replace cookies',
    'access_error': 'Access denied on last attempt', 'failed': 'Last download failed', 'rate_limited': 'Rate limited on last attempt',
}


def _account_record(platform, prefix, item, primary):
    entry = prefix + hidden_field('id', item['id'])
    label = escape(item['label'])
    result = _ACCOUNT_RESULTS.get(item['last_result'], 'Unknown')
    result_text = f'{result} · Cookie revision {item["revision"]}'
    return ('<div class="record"><div class="record-info">'
            + f'<strong>{label}</strong> ' + _credential_state(item['enabled'], primary)
            + f'<p class="muted">{escape(result_text)}</p></div>'
            + form('account', entry, _credential_actions(item, primary, 'account')) + '</div>'
            + f'<details><summary aria-label="Edit account {label}">Edit account</summary>'
            + _account_edit(platform, entry, item) + '</details>')


def _pool_check(platform, hidden, polling, downloads):
    import time

    check = pool_checks.latest(platform)
    body = '<h3>Download check</h3>'
    if check:
        result = ' · '.join(escape(part) for part in
                            (check['state'].capitalize(), check['account_label'], check['detail']) if part)
        body += f'<p>Last check: {result}</p>'
        pending = check['state'] == 'queued' and check['requested'] >= time.time() - pool_checks.QUEUE_TTL
        if pending or check['state'] == 'running':
            return body + f'<a href="{c.BASE}accounts?platform={platform}#{platform}">Refresh check status</a>'
    if not polling:
        return body + f'<p><a href="{c.BASE}">Enable Telegram polling</a> to test a download.</p>'
    if not downloads:
        return body + f'<p><a href="{c.BASE}features#download_{platform}">Enable this platform in Features</a> to test a download.</p>'
    controls = field('url', f'{accounts.LABELS[platform]} post URL', 'url', attrs='required maxlength="2048"')
    controls += button('Test download', kind='secondary')
    return body + form('pool-check/' + platform, hidden, controls)


def pools(hidden, selected_platform=None):
    from .. import features

    data = c.rpc(c.SECRET_SOCKET, '/accounts')
    flags = features.snapshot()
    polling = c.get_setting('enabled', False)
    sections = []
    for platform in accounts.PLATFORMS:
        pool = data[platform]
        items = pool['items']
        enabled = sum(item['enabled'] for item in items)
        count = f'{enabled} of {len(items)} enabled' if items else 'No accounts'
        if items and pool['policy'] == 'primary':
            primary = next((item['label'] for item in items if item['id'] == pool['primary']), None)
            count += f' · Primary: {primary}' if primary else ' · No primary selected'
        elif items:
            count += ' · Rotation'
        label = accounts.LABELS[platform]
        prefix = hidden + hidden_field('platform', platform)
        records = ''.join(_account_record(platform, prefix, item, pool['primary'] == item['id']) for item in items)
        open_pool = ' open' if selected_platform == platform else ''
        section = (f'<details class="pool" id="{platform}"{open_pool}>'
                   f'<summary><strong>{label}</strong> <span class="muted">{escape(count)}</span></summary>')
        if items:
            section += records
        elif platform in ('threads', 'youtube'):
            section += '<p>Downloads can use public access while this pool is empty.</p>'
        editor_open = '' if items else ' open'
        section += (f'<details{editor_open}><summary>Add account</summary>'
                    + _account_edit(platform, prefix + hidden_field('id', '')) + '</details>')
        choices = [('primary', 'Use primary account'), ('round_robin', 'Rotate enabled accounts')]
        options = ''.join(f'<option value="{value}"{" selected" if pool["policy"] == value else ""}>{title}</option>'
                          for value, title in choices)
        controls = f'<label>Account selection<select name="policy">{options}</select></label>'
        if items and pool['policy'] == 'primary' and not pool['primary']:
            controls += '<p>Choose an enabled account as primary or switch to rotation.</p>'
        section += form('account', prefix, controls + button('Save selection', 'policy', kind='secondary'))
        section += _pool_check(platform, hidden, polling, flags['download_' + platform])
        sections.append(section + '</details>')
    return ''.join(sections)


def keys(hidden):
    local = c.rpc(c.SECRET_SOCKET, '/local-api/status')['credentials_set']
    keys = c.rpc(c.SECRET_SOCKET, '/translation/status')['keys']
    body = '<section id="translation-keys"><h2>Translation API keys</h2>'
    if not keys:
        body += '<p>Add an OpenAI key to translate text and images in Telegram.</p>'
    for item in keys:
        entry = hidden + hidden_field('id', item['id'])
        label = escape(item['label'])
        body += ('<div class="record"><div class="record-info">'
                 + f'<strong>{label}</strong> ' + _credential_state(item['enabled'], item['primary'])
                 + '</div>' + form('api-key', entry, _credential_actions(item, item['primary'], 'key')) + '</div>')
        controls = field('label', 'Key name', value=item['label'], attrs='required maxlength="60"')
        controls += field('key', 'Replacement key (leave blank to keep the saved key)', 'password',
                          attrs='autocomplete="new-password"')
        body += (f'<details><summary aria-label="Edit key {label}">Edit key</summary>'
                 + form('api-key', entry, controls + button('Save changes', 'save',
                        aria_label=f'Save changes to {item["label"]}')) + '</details>')
    entry = hidden + hidden_field('id', '')
    controls = field('label', 'Key name', attrs='required maxlength="60"')
    controls += field('key', 'OpenAI API key', 'password', attrs='required autocomplete="new-password"')
    body += ('<details' + ('' if keys else ' open') + '><summary>Add translation key</summary>'
             + form('api-key', entry, controls + button('Add key', 'save')) + '</details></section>')
    state = '<span class="state on">Configured</span>' if local else '<span class="state">Not configured</span>'
    body += ('<section id="local-api"><div class="section-head"><h2>Telegram Local Bot API</h2>'
             + state + '</div><p>Only needed for a self-hosted Telegram Bot API server. '
             'Enter the application ID and hash together.</p>')
    controls = field('api_id', 'Telegram application ID', 'password',
                     attrs='required autocomplete="new-password" inputmode="numeric"')
    controls += field('api_hash', 'Telegram application hash', 'password', attrs='required autocomplete="new-password"')
    return body + form('local-api', hidden, controls + button('Replace credentials' if local else 'Save credentials')) + '</section>'
