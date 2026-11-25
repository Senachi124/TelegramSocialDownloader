"""Server-rendered Telegram service management pages."""
from html import escape
from urllib.parse import urlencode

from .. import accounts, core as c, pool_checks


STYLE = """
*{box-sizing:border-box}body{margin:0;background:#f5f7f8;color:#14212c;font:16px/1.5 system-ui,sans-serif}
main{max-width:66rem;margin:auto;padding:2rem 1.2rem}header{border-bottom:2px solid #184a55;margin-bottom:1.5rem}
h1,h2,h3{line-height:1.2}h1{font-size:2rem}h2{font-size:1.3rem;margin-top:0}
section,details.pool{background:white;border:1px solid #ced7dc;border-radius:.5rem;padding:1.15rem;margin:1rem 0}
nav{display:flex;gap:1rem;flex-wrap:wrap;padding-bottom:1rem}a{color:#005668}label{display:block;margin:.7rem 0}
input,select,textarea,button{font:inherit;max-width:100%;border:1px solid #98a7ae;border-radius:.3rem;padding:.4rem .6rem}
label input:not([type=checkbox]),label textarea,label select{display:block;width:min(100%,32rem);margin:.25rem 0}
button{background:#134955;color:white;cursor:pointer;margin:.2rem .25rem .2rem 0}summary{cursor:pointer;font-weight:650}
ul{padding-left:1.4rem}li{margin:.7rem 0}fieldset{padding:.6rem 1rem;margin:1rem 0;border:1px solid #d7dfe2}
:focus-visible{outline:3px solid #00869c;outline-offset:2px}dt{font-weight:600}dd{margin:0 0 .65rem}
"""


def page(title, body, *, signed_in=False):
    routes = [('','Connection'), ('users','Users & groups'), ('subscriptions','Subscriptions'),
              ('accounts','Account pools'), ('keys','API keys')]
    navigation = ''.join(f'<a href="{c.BASE}{path}">{label}</a>' for path,label in routes) if signed_in else ''
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="robots" content="noindex,nofollow"><title>{escape(title)} · Telegram Social Downloader</title>'
            f'<style>{STYLE}</style></head><body><main><header><h1>{escape(title)}</h1>'
            f'<nav aria-label="Service settings">{navigation}</nav></header>{body}</main></body></html>')


def field(name, label, kind='text', *, value='', attrs=''):
    return (f'<label>{label}<input type="{kind}" name="{name}" value="{escape(str(value),quote=True)}" '
            f'{attrs}></label>')


def form(action, hidden, controls, multipart=False):
    encoding = ' enctype="multipart/form-data"' if multipart else ''
    return f'<form method="post" action="{c.BASE}{action}"{encoding}>{hidden}{controls}</form>'


def button(text, action=''):
    value = f' name="action" value="{action}"' if action else ''
    return f'<button type="submit"{value}>{text}</button>'


def hidden_field(name, value):
    return f'<input type="hidden" name="{name}" value="{escape(str(value),quote=True)}">'


def connection(hidden):
    status = c.rpc(c.SECRET_SOCKET, '/status')
    enabled = c.get_setting('enabled',False)
    alerts = c.get_setting('failure_alerts',True)
    health = escape(str(c.get_setting('telegram_health','Awaiting first poll')))
    controls = field('token', 'Replace bot token', 'password', attrs='autocomplete="new-password"')
    controls += '<fieldset><legend>Media items per minute</legend>'
    for platform in accounts.PLATFORMS:
        key = platform+'_media_limit'
        controls += field(key, accounts.LABELS[platform], 'number', value=c.get_setting(key,20), attrs='min="1" max="1000" required')
    controls += '</fieldset>'
    for key,label,on in [('enabled','Enable Telegram polling and processing',enabled),
                         ('failure_alerts','Notify the owner about failures and their causes',alerts)]:
        controls += f'<label><input type="checkbox" name="{key}" value="1" {"checked" if on else ""}> {label}</label>'
    return (f'<section><h2>Bot connection</h2><p>Token: {"configured" if status["token_set"] else "not configured"} · {health}</p>'
            +form('check', hidden, button('Check connection'))+form('settings',hidden,controls+button('Save bot settings'))+'</section>')


def users(hidden, offset):
    with c.db() as db:
        rows = list(db.execute("""SELECT id,name,username,state FROM bot_users ORDER BY
          CASE state WHEN 'owner' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,id LIMIT 51 OFFSET ?""",(offset,)))
    items = []
    for row in rows[:50]:
        label = escape(row['name'] or 'Unnamed user')
        handle = ' · @'+escape(row['username']) if row['username'] else ''
        controls = ''
        if row['state'] != 'owner':
            controls = form('access',hidden + hidden_field('user_id',row['id']),
                            button('Approve','approve')+button('Reject','reject')+button('Revoke','revoke'))
        items.append(f'<li><strong>{label}</strong>{handle} · {row["id"]} · {escape(row["state"])}{controls}</li>')
    more = []
    for title,where,show in [('Previous',offset-50,offset>0),('Next',offset+50,len(rows)>50)]:
        if show:
            more.append(f'<a href="{c.BASE}users?{urlencode({"offset":where})}">{title}</a>')
    auto = c.get_setting('auto_approve_users',False)
    auto_form = (f'<label><input type="checkbox" name="enabled" value="1" {"checked" if auto else ""}> '
                 'Automatically approve new private-chat users</label>'+button('Save approval policy'))
    groups = c.get_setting('translation_groups',[])
    groups_text = ', '.join(str(i) for i in groups)
    return ('<section><h2>User access</h2>'+form('admission',hidden,auto_form)
            +('<ul>'+''.join(items)+'</ul>' if items else '<p>No registered users.</p>')+' '.join(more)+'</section>'
            +'<section><h2>Translation groups</h2>'+form('groups',hidden,field('groups','Telegram group IDs (comma separated)',value=groups_text)
            +button('Save groups'))+'</section>')


def pools(hidden):
    data = c.rpc(c.SECRET_SOCKET, '/accounts')
    sections = []
    for platform in accounts.PLATFORMS:
        pool = data[platform]
        label = accounts.LABELS[platform]
        prefix = hidden + hidden_field('platform',platform)
        options = ''.join(f'<option value="{value}" {"selected" if pool["policy"]==value else ""}>{title}</option>'
                       for value,title in [('primary','Primary account'),('round_robin','Round robin')])
        controls = form('account',prefix,f'<label>Assignment<select name="policy">{options}</select></label>'+button('Save policy','policy'))
        upload = (field('youtube_cookie_file','Select a Netscape cookies.txt file','file',attrs='accept=".txt"') if platform=='youtube' else '')
        for item in [*pool['items'],None]:
            item_id = item['id'] if item else ''
            entry = prefix + hidden_field('id', item_id)
            edit = field('label','Label',value=item['label'] if item else '',attrs='maxlength="60" required')
            edit += '<label>Cookie'+(' (leave blank to keep)' if item else '')+'<textarea name="cookie" autocomplete="off" spellcheck="false"></textarea></label>'+upload
            title = escape(item['label']) if item else 'Add account'
            controls += f'<details><summary>{title}</summary>'
            if item:
                state = 'enabled' if item['enabled'] else 'disabled'
                primary = ' · primary' if pool['primary']==item_id else ''
                controls += f'<p>{state}{primary} · {escape(item["last_result"])} · credential revision {item["revision"]}</p>'
                actions = button('Make primary','primary')+button('Disable','disable') if item['enabled'] else button('Enable','enable')+button('Delete','delete')
                controls += form('account',entry,actions)
            controls += form('account',entry,edit+button('Save','save'),platform=='youtube')+'</details>'
        check = pool_checks.latest(platform)
        if check:
            controls += f'<p>Download check: {escape(check["state"])} · {escape(check["account_label"])} · {escape(check["detail"])}</p>'
        controls += form('pool-check/'+platform,hidden,field('url','Post URL for a download check','url',attrs='required maxlength="2048"')+button('Test download'))
        sections.append(f'<details class="pool" id="{platform}"><summary>{label}</summary>{controls}</details>')
    return ''.join(sections)


def keys(hidden):
    status = c.rpc(c.SECRET_SOCKET, '/local-api/status')
    state = 'configured' if status['credentials_set'] else 'not configured'
    controls = field('api_id','Telegram application ID','password',attrs='autocomplete="new-password" inputmode="numeric"')
    controls += field('api_hash','Telegram application hash','password',attrs='autocomplete="new-password"')
    body = '<section><h2>Telegram Local Bot API</h2><p>Application credentials: '+state+'</p>'+form('local-api',hidden,controls+button('Save credentials'))+'</section>'
    body += '<section><h2>Translation API keys</h2>'
    for item in [*c.rpc(c.SECRET_SOCKET,'/translation/status')['keys'],None]:
        entry = hidden + hidden_field('id',item['id'] if item else '')
        if item:
            primary = ' · primary' if item['primary'] else ''
            body += f'<h3>{escape(item["label"])}{primary}</h3>'
            actions = button('Make primary','primary')+button('Disable','disable') if item['enabled'] else button('Enable','enable')+button('Delete','delete')
            body += form('api-key',entry,actions)
        else:
            body += '<h3>Add key</h3>'
        body += form('api-key',entry,field('label','Label',value=item['label'] if item else '',attrs='required maxlength="60"')
                     +field('key','OpenAI API key','password',attrs='autocomplete="new-password"')+button('Save key','save'))
    return body+'</section>'
