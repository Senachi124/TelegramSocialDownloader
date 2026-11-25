"""Credential pool operations, called only by the serialized credential broker.

Only public_summary may be returned to the web UI. Cookies stay in secrets.json.
"""
import secrets
import time

PLATFORMS=('instagram','tiktok','twitter','threads','youtube')
DOWNLOAD_PLATFORMS=PLATFORMS
LABELS={'instagram':'Instagram','tiktok':'TikTok','twitter':'Twitter / X','threads':'Threads','youtube':'YouTube'}
LEGACY={'instagram':'instagram_cookies','tiktok':'cookies','twitter':'twitter_cookies','threads':'threads_cookies'}
PUBLIC=('id','label','enabled','revision','updated','last_used','last_result','last_result_at')


def normalize(data):
    data.setdefault('token','');data.setdefault('cookies',{})
    pools=data.setdefault('account_pools',{})
    for platform in PLATFORMS:
        if platform not in pools:
            cookies=data.get(LEGACY.get(platform),{})
            items=[]
            if cookies:
                items=[dict(id='legacy-'+platform,label='Imported account',enabled=True,cookies=cookies,
                            revision=1,updated=0,last_used=0,last_result='unknown',last_result_at=0)]
            pools[platform]={'primary':items[0]['id'] if items else '', 'policy':'primary','items':items}
    return data


def pool(data,platform):
    if platform not in PLATFORMS:raise ValueError('Invalid platform')
    return normalize(data)['account_pools'][platform]


def sync_legacy(data):
    # Compatibility for old diagnostic callers, never an alternate credential pool.
    for platform in LEGACY:
        p=pool(data,platform)
        a=next((a for a in p['items'] if a['id']==p['primary'] and a['enabled']),None)
        data[LEGACY[platform]]=a['cookies'] if a else {}


def public_summary(data):
    return {platform:{'primary':p['primary'],'policy':p['policy'],
            'items':[{**{k:a[k] for k in PUBLIC},'cookie_set':bool(a['cookies'])} for a in p['items']]}
            for platform in PLATFORMS for p in [pool(data,platform)]}


def edit(data,request,parse):
    p=pool(data,request.get('platform'));action=request.get('action')
    key=request.get('id','');now=time.time()
    a=next((a for a in p['items'] if a['id']==key),None)
    if action=='save':
        label=request.get('label','').strip();raw=request.get('cookie','').strip()
        if not label or len(label)>60 or any(ord(x)<32 for x in label):raise ValueError('Supply an account label of up to 60 characters')
        if key and not a:raise ValueError('Unknown account')
        cookies=parse(raw,request['platform']+'.com') if raw else None
        if a is None:
            if not cookies:raise ValueError('A new account requires cookies')
            if len(p['items'])>=32:raise ValueError('Each platform supports up to 32 accounts')
            a=dict(id=secrets.token_hex(12),label=label,enabled=True,cookies=cookies,
                   revision=1,updated=now,last_used=0,last_result='unknown',last_result_at=0)
            p['items'].append(a)
            if not p['primary']:p['primary']=a['id']
        else:
            a['label']=label
            if cookies:
                a.update(cookies=cookies,revision=a['revision']+1,updated=now,last_result='unknown',last_result_at=0)
    elif action in ('enable','disable','primary','delete'):
        if a is None:raise ValueError('Unknown account')
        if action=='primary':
            if not a['enabled']:raise ValueError('Enable the account first')
            p['primary']=key
        elif action=='delete':
            if a['enabled'] or p['primary']==key:raise ValueError('Disable the account first')
            p['items'].remove(a)
        else:
            a['enabled']=action=='enable'
            if action=='disable' and p['primary']==key:p['primary']=''
    elif action=='policy':
        value=request.get('policy')
        if value not in ('primary','round_robin'):raise ValueError('Invalid allocation policy')
        p['policy']=value
    else:raise ValueError('Invalid action')
    sync_legacy(data)
    return {'ok':True,'id':a['id'] if a else ''}


def update_primary(data,platform,cookies):
    p=pool(data,platform)
    a=next((a for a in p['items'] if a['id']==p['primary']),None)
    if a:
        a.update(cookies=cookies,revision=a['revision']+1,updated=time.time(),last_result='unknown',last_result_at=0)
    else:
        if len(p['items'])>=32:raise ValueError('Select a primary account')
        a=dict(id=secrets.token_hex(12),label='Primary account',enabled=True,cookies=cookies,
               revision=1,updated=time.time(),last_used=0,last_result='unknown',last_result_at=0)
        p['items'].append(a);p['primary']=a['id']
    sync_legacy(data)


def select(data,request):
    p=pool(data,request.get('platform'));key=request.get('id','')
    # An empty optional pool is public mode. A configured or pinned
    # account that becomes unavailable must never silently downgrade to public.
    if request.get('platform') in ('threads','youtube') and not key and not p['items']:
        return {'cookies':{},'public':True}
    candidates=[a for a in p['items'] if a['enabled'] and a['cookies'] and a['last_result']!='auth_required']
    if key:candidates=[a for a in candidates if a['id']==key]
    elif p['policy']=='primary':candidates=[a for a in candidates if a['id']==p['primary']]
    if not candidates:return {'cookies':{},'reason':'No eligible account. Check its credentials and allocation policy.'}
    a=min(candidates,key=lambda x:(x['last_used'],x['id']))
    a['last_used']=time.time()
    return {k:a[k] for k in ('id','revision','cookies')}


def report(data,request):
    p=pool(data,request.get('platform'))
    a=next((a for a in p['items'] if a['id']==request.get('id')),None)
    outcome=request.get('outcome')
    if outcome not in ('success','auth_required','access_error','failed','rate_limited'):raise ValueError('Invalid status')
    # A result from an in-flight old Cookie must not poison a newly updated Cookie.
    if a and a['revision']==request.get('revision'):
        a.update(last_result=outcome,last_result_at=time.time())
    return {'ok':True}
