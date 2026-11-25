"""Apply authenticated web console operations."""
import re
import time

from .. import access, accounts, core as c, features, pool_checks
from .forms import FormError

ACCOUNT_RESUME_MAX_AGE = 30 * 60


def resume_accounts(platform,account_id=''):
    # Age is measured from the original request, never the Cookie update or
    # last retry. One write transaction prevents expired jobs becoming runnable.
    cutoff = time.time() - ACCOUNT_RESUME_MAX_AGE
    eligible = """platform=? AND state='paused' AND cancel=0
          AND (account_id='' OR account_id=?)
          AND NOT EXISTS(SELECT 1 FROM files WHERE job=jobs.id AND state='uncertain')"""
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("""UPDATE jobs SET state='cancelled',cancel=1,retry_at=0,
          message='工作建立已超過 30 分鐘，更新帳號後自動撤銷；如仍需要請重新傳送連結。'
          WHERE """ + eligible + ' AND created<?', (platform,account_id,cutoff))
        db.execute("""UPDATE jobs SET state='queued' WHERE """ + eligible + """
          AND created>=?
          AND (mode='batch' OR NOT EXISTS(SELECT 1 FROM files WHERE job=jobs.id))""",
          (platform,account_id,cutoff))


# All redirect paths and notices are derived from known administrative operations.
ACTIONS = {
    'settings': ('', '', 'Bot settings saved.'),
    'bot-options': ('', 'bot-operation', 'Bot settings saved.'),
    'media-limits': ('', 'media-limits', 'Media limits saved.'),
    'bot-token': ('', 'bot-token', 'Bot token saved.'),
    'check': ('', 'connection-status', 'Connection check complete.'),
    'features': ('features', '', 'Feature setting updated.'),
    'access': ('users', 'user-access', 'User access updated.'),
    'admission': ('users', 'approval-policy', 'Approval policy saved.'),
    'groups': ('users', 'translation-groups', 'Group settings saved.'),
    'account': ('accounts', '', 'Account pool updated.'),
    'pool-check': ('accounts', '', 'Download check queued.'),
    'api-key': ('keys', 'translation-keys', 'Translation keys updated.'),
    'local-api': ('keys', 'local-api', 'Local API credentials saved.'),
    'subscriptions': ('subscriptions', '', 'Subscription settings saved.'),
}


def destination(route, form):
    action = (route or '').split('/', 1)[0]
    page, anchor, _ = ACTIONS.get(action, ('', '', ''))
    if action in ('account', 'pool-check'):
        platform = route.removeprefix('pool-check/') if action == 'pool-check' else form.get('platform')
        if platform in accounts.PLATFORMS:
            return f'accounts?platform={platform}#{platform}'
    if action == 'access' and re.fullmatch(r'[1-9][0-9]{0,6}', form.get('offset', '')):
        page += '?offset=' + form['offset']
    if action == 'subscriptions' and re.fullmatch(r'[a-z][a-z0-9-]{0,47}', form.get('provider', '')):
        anchor = 'provider-' + form['provider']
    if action == 'features' and (form.get('feature') in features.LABELS or form.get('feature') == 'takaneko'):
        anchor = form['feature']
    return page + ('#' + anchor if anchor else '')


def _media_limits(form):
    try:
        limits = {p + '_media_limit': int(form[p + '_media_limit']) for p in accounts.PLATFORMS}
    except (KeyError, ValueError):
        limits = {}
    if not limits or not all(1 <= value <= 1000 for value in limits.values()):
        raise FormError('Enter a limit from 1 to 1000 for every platform.')
    return limits


def manage(route, form):
    """Apply an authenticated administrative change and return its redirect route."""
    if route=='features':
        from . import feature_controls
        feature_controls.update(form)
    elif route in ('settings', 'bot-options', 'media-limits', 'bot-token'):
        changes = _media_limits(form) if route in ('settings', 'media-limits') else {}
        if route in ('settings', 'bot-options'):
            changes.update(enabled=form.get('enabled')=='1', failure_alerts=form.get('failure_alerts')=='1')
        if route == 'bot-token' and not form.get('token','').strip():
            raise FormError('Enter a BotFather token to save it.')
        if route in ('bot-token', 'settings') and form.get('token'):
            c.rpc(c.SECRET_SOCKET, '/settings', {'token': form['token']})
        if changes:
            c.setting_many(changes)
    elif route=='check':
        from ..processing.telegram_api import telegram
        telegram('getMe',{})
        hook = telegram('getWebhookInfo',{})
        c.setting('telegram_health','Webhook configured; remove it to poll' if hook.get('url') else 'Connected')
    elif route=='access':
        access.decide(int(form.get('user_id','0')),form.get('action',''))
    elif route=='admission':
        c.setting('auto_approve_users',form.get('enabled')=='1')
    elif route=='groups':
        values = [v.strip() for v in form.get('groups','').split(',') if v.strip()]
        if len(values)>200 or any(not re.fullmatch(r'-[1-9][0-9]{0,15}',v) for v in values):
            raise FormError('Supply negative numeric Telegram group IDs.')
        c.setting('translation_groups',list(dict.fromkeys(map(int,values))))
    elif route=='account':
        result = c.rpc(c.SECRET_SOCKET,'/accounts',{k:form.get(k,'') for k in ('platform','action','id','label','cookie','policy')})
        if form.get('action') in ('save','enable','primary'):
            resume_accounts(form['platform'],result.get('id',''))
    elif route.startswith('pool-check/'):
        platform = route[len('pool-check/'):]
        if platform not in accounts.PLATFORMS:
            return None
        try:
            pool_checks.start(platform,form.get('url',''))
        except ValueError as exc:
            if str(exc) in pool_checks.FORM_ERRORS.values():
                raise FormError(str(exc)) from None
            raise
    elif route=='api-key':
        c.rpc(c.SECRET_SOCKET,'/translation/keys',{k:form.get(k,'') for k in ('id','label','key','action')})
    elif route=='local-api':
        c.rpc(c.SECRET_SOCKET,'/local-api',{k:form.get(k,'') for k in ('api_id','api_hash')})
    elif route=='subscriptions':
        from .. import push
        push.web_post(form)
    else:
        return None
    return destination(route, form)
