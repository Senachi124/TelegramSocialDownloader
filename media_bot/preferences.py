"""Per-user download preferences; links enqueue with an immutable snapshot."""
import copy
from html import escape
import json
from . import config,core as c

DEFAULTS={
    'instagram':{'content':'post_all','prefer_hdr':True},
    'tiktok':{'content':'post_all'},
    'youtube':{'quality':'best','prefer_hdr':True},
}
LABELS={'instagram':'Instagram','tiktok':'TikTok','youtube':'YouTube'}
CONTENT={
    'instagram':{'post_all':'貼文全部內容','post_images':'指定連結中的圖片','stories_all':'作者全部限時動態'},
    'tiktok':{'post_all':'貼文全部內容','stories_all':'作者全部限時動態'},
}
QUALITY={'best':'最高畫質','1080':'優先 1080p 以下','720':'優先 720p 以下','480':'優先 480p 以下'}


def normalize(value):
    result=copy.deepcopy(DEFAULTS)
    if not isinstance(value,dict):return result
    for platform,fields in result.items():
        saved=value.get(platform,{})
        if not isinstance(saved,dict):continue
        content=saved.get('content')
        if platform=='instagram' and content=='stories_selected':content='stories_all'
        if platform=='tiktok':content={'post_selected':'post_all','stories_selected':'stories_all'}.get(content,content)
        if content in CONTENT.get(platform,{}):fields['content']=content
        if platform=='youtube' and saved.get('quality') in QUALITY:fields['quality']=saved['quality']
        if 'prefer_hdr' in fields and type(saved.get('prefer_hdr')) is bool:fields['prefer_hdr']=saved['prefer_hdr']
    return result


def load(actor):return normalize(c.get_setting('download_preferences:'+str(actor),{}))


def record_notice(job,notice):
    if not notice:return
    key='preference_notice:'+job
    previous=c.get_setting(key,[])
    if not isinstance(previous,list):previous=[]
    text=str(notice)[:500]
    if text not in previous:c.setting(key,(previous+[text])[-20:])


def notice_text(job):
    notes=c.get_setting('preference_notice:'+job,[])
    return ('\n偏好調整：\n'+'\n'.join(notes))[:2500] if isinstance(notes,list) and notes else ''


def save(actor,platform,field,value):
    if platform not in DEFAULTS or field not in DEFAULTS[platform]:raise ValueError('無效設定')
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not config.allowed(actor):raise PermissionError
        key='download_preferences:'+str(actor)
        row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        current=normalize(json.loads(row[0]) if row else {})
        current[platform][field]=value
        if normalize(current)!=current:raise ValueError('設定格式無效')
        db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(current)))


def route(platform,mode,url,preference):
    """Honor a preferred scope only when the recognized URL can supply it."""
    if platform not in CONTENT or mode=='resolve':return mode,''
    content=preference.get('content','post_all')
    wants_stories=content.startswith('stories_')
    if mode=='stories':
        if not wants_stories:
            return 'stories','無法套用貼文／單一圖片偏好；目前識別為限時動態或使用者連結，已改為下載可存取的限時動態。'
        return 'stories',''
    if mode in ('choice','post'):
        return ('stories' if wants_stories else 'post'),''
    return mode,''


def _button(label,data):return {'text':label,'callback_data':'pref:'+data}


def show(send,actor,platform=''):
    prefs=load(actor)
    text=['<b>我的下載設定</b>','只影響你之後送出的連結，已排隊工作保留原設定。貼上連結後會直接下載，不再詢問貼文或限時動態。']
    rows=[]
    if not platform:
        for name in DEFAULTS:
            p=prefs[name]
            summary=QUALITY[p['quality']] if name=='youtube' else CONTENT[name][p['content']]
            if 'prefer_hdr' in p:summary+=' · HDR '+('優先' if p['prefer_hdr'] else '不優先')
            text.append(escape(LABELS[name]+'：'+summary))
            rows.append([_button('設定 '+LABELS[name],name)])
        rows.append([_button('恢復我的預設','reset')])
    else:
        if platform not in DEFAULTS:raise ValueError('無效平台')
        p=prefs[platform];text.append('<b>'+LABELS[platform]+'</b>')
        if platform in CONTENT:
            text.append('目前：'+CONTENT[platform][p['content']])
            for key,label in CONTENT[platform].items():
                rows.append([_button(('✓ ' if p['content']==key else '')+label,platform+':content:'+key)])
            if platform=='instagram':
                text.append('「指定連結中的圖片」讀取每條貼文連結的 <code>?img_index=3</code>，代表原始輪播第 3 項；不使用全域圖片序號。缺少或格式無效時下載全部內容；超出範圍或該位置不是圖片時改下載可確認的圖片，沒有圖片則保留可確認的原內容並說明。')
                text.append('Instagram 限時動態一律下載目前可存取的全部內容，不需設定序號。')
            else:
                text.append('TikTok 只選貼文全部內容或作者全部限時動態，不提供指定項目或序號。')
        else:
            text.append('目前：'+QUALITY[p['quality']])
            for key,label in QUALITY.items():rows.append([_button(('✓ ' if p['quality']==key else '')+label,platform+':quality:'+key)])
        if 'prefer_hdr' in p:
            text.append('HDR：'+('有 HDR 時優先，沒有則使用一般版本。' if p['prefer_hdr'] else '優先一般版本；只有 HDR 可用時保留原片。'))
            rows.append([_button('HDR '+('關閉優先' if p['prefer_hdr'] else '開啟優先'),platform+':prefer_hdr:'+('0' if p['prefer_hdr'] else '1'))])
        rows.append([_button('返回全部設定','home')])
    text.append('設定是偏好：若連結類型、圖片位置或畫質不可用，會下載能確認的內容並說明原因；沒有存取權限時不會改抓其他內容。')
    send('sendMessage',{'chat_id':actor,'text':'\n\n'.join(text),'parse_mode':'HTML','reply_markup':{'inline_keyboard':rows}})


def handle(update,send):
    callback=update.get('callback_query');message=callback.get('message',{}) if callback else update.get('message',{})
    actor=(callback.get('from',{}) if callback else message.get('from',{})).get('id')
    chat=message.get('chat',{})
    if chat.get('type')!='private' or actor!=chat.get('id') or not config.allowed(actor):return False
    data=callback.get('data','') if callback else ''
    parts=message.get('text','').strip().split()
    if callback and not data.startswith('pref:'):return False
    if not callback and (not parts or parts[0].split('@')[0]!='/settings'):return False
    try:
        if callback:
            send('answerCallbackQuery',{'callback_query_id':callback['id']})
            fields=data[5:].split(':')
            if fields==['reset']:
                c.setting('download_preferences:'+str(actor),{});show(send,actor)
            elif fields==['home']:show(send,actor)
            elif len(fields)==1:show(send,actor,fields[0])
            elif len(fields)==3:
                platform,field,value=fields
                if field=='prefer_hdr':
                    if value not in ('0','1'):raise ValueError('無效設定')
                    value=value=='1'
                save(actor,platform,field,value);show(send,actor,platform)
            else:raise ValueError('無效設定')
        elif len(parts)==1:show(send,actor)
        elif len(parts)==2:show(send,actor,{'ig':'instagram','yt':'youtube'}.get(parts[1],parts[1]))
        elif len(parts)==3:
            platform={'ig':'instagram'}.get(parts[1],parts[1])
            if platform=='instagram':
                text='Instagram 限時動態會自動下載目前可存取的全部內容，不需設定序號。指定貼文圖片請選「指定連結中的圖片」，並使用貼文連結的 ?img_index=3。'
            elif platform=='tiktok':
                text='TikTok 不提供指定項目或序號。請在 /settings tiktok 選擇貼文全部內容或作者全部限時動態。'
            else:raise ValueError('無效平台')
            send('sendMessage',{'chat_id':actor,'text':text})
        else:raise ValueError('請輸入 /settings')
    except (ValueError,PermissionError):
        send('sendMessage',{'chat_id':actor,'text':'設定無效或無權操作。請輸入 /settings；限時動態一律下載全部可存取內容，不需設定序號。Instagram 指定圖片從連結的 ?img_index=3 取得。'})
    return True
