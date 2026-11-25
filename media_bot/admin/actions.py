"""Apply authenticated web console operations."""
import re
import time

from .. import access, accounts, core as c, pool_checks

ACCOUNT_RESUME_MAX_AGE = 30 * 60


class FormError(ValueError):
    """A form error with a fixed user-facing message."""


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


def manage(route, form):
    """Apply an authenticated administrative change and return its redirect route."""
    if route=='settings':
        changes = {p+'_media_limit':int(form[p+'_media_limit']) for p in accounts.PLATFORMS}
        if not all(1<=v<=1000 for v in changes.values()):
            raise FormError('Limits must be between 1 and 1000 media items per minute.')
        if form.get('token'):
            c.rpc(c.SECRET_SOCKET,'/settings',{'token':form['token']})
        changes.update(enabled=form.get('enabled')=='1',failure_alerts=form.get('failure_alerts')=='1')
        c.setting_many(changes)
        return ''
    if route=='check':
        from ..processing.telegram_api import telegram
        telegram('getMe',{})
        hook = telegram('getWebhookInfo',{})
        c.setting('telegram_health','Webhook configured; remove it to poll' if hook.get('url') else 'Connected')
        return ''
    if route=='access':
        access.decide(int(form.get('user_id','0')),form.get('action',''))
        return 'users'
    if route=='admission':
        c.setting('auto_approve_users',form.get('enabled')=='1')
        return 'users'
    if route=='groups':
        values = [v.strip() for v in form.get('groups','').split(',') if v.strip()]
        if len(values)>200 or any(not re.fullmatch(r'-[1-9][0-9]{0,15}',v) for v in values):
            raise FormError('Supply negative numeric Telegram group IDs.')
        c.setting('translation_groups',list(dict.fromkeys(map(int,values))))
        return 'users'
    if route=='account':
        result = c.rpc(c.SECRET_SOCKET,'/accounts',{k:form.get(k,'') for k in ('platform','action','id','label','cookie','policy')})
        if form.get('action') in ('save','enable','primary'):
            resume_accounts(form['platform'],result.get('id',''))
        return 'accounts'
    if route.startswith('pool-check/'):
        platform = route[len('pool-check/'):]
        if platform not in accounts.PLATFORMS:
            return None
        try:
            pool_checks.start(platform,form.get('url',''))
        except ValueError as exc:
            if str(exc) in pool_checks.FORM_ERRORS.values():
                raise FormError(str(exc)) from None
            raise
        return 'accounts'
    if route=='api-key':
        c.rpc(c.SECRET_SOCKET,'/translation/keys',{k:form.get(k,'') for k in ('id','label','key','action')})
        return 'keys'
    if route=='local-api':
        c.rpc(c.SECRET_SOCKET,'/local-api',{k:form.get(k,'') for k in ('api_id','api_hash')})
        return 'keys'
    if route=='subscriptions':
        from .. import push
        push.web_post(form)
        return 'subscriptions'
    return None
