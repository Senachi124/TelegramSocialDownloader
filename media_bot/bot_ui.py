"""Private-chat navigation and owner views; every callback rechecks authority."""
from datetime import datetime
from html import escape
import re
import shutil
import time
from zoneinfo import ZoneInfo

from . import core as c, access, accounts, limits

SHORT_DESCRIPTION = '下載 Instagram、TikTok、X、Threads 和 YouTube 的圖片與影片。貼上連結即可開始，支援混合平台批次下載。'
DESCRIPTION = ('把社群連結傳給我，即可下載圖片與影片。\n\n'
    '• 支援 Instagram、TikTok、X／Twitter、Threads、YouTube\n'
    '• 可混合不同平台批次下載，每則訊息最多 50 個連結\n'
    '• Instagram／TikTok 支援目前可存取的限時動態\n'
    '• YouTube 支援公開單條影片及 Shorts\n\n'
    '輸入 /help 查看使用方法。')
HELP = ('直接貼上連結，我會下載並傳回圖片或影片。\n'
    '支援 Instagram、TikTok、X／Twitter、Threads、YouTube。\n\n'
    '<b>如何使用</b>\n'
    '1. 一則訊息可混合不同平台的連結，每則最多 50 個，建議每行一個。\n'
    '2. 先用 /settings 保存自己的下載範圍、畫質及 HDR 偏好。\n'
    '3. 單條與批次連結都直接依偏好下載；偏好不適用時下載可確認內容並說明原因。\n'
    '4. YouTube 支援公開影片、Shorts 和 youtu.be；播放清單參數只取單片，不支援直播；需要登入時請管理員更新 YouTube Cookie。\n\n'
    '<b>我的下載偏好</b>\n'
    'Instagram：可選貼文內容、限時動態範圍及 HDR 優先。\n'
    'TikTok：貼文全部內容／作者全部限時動態，不提供指定項目。\n'
    'YouTube：最高畫質或 1080p／720p／480p 偏好，以及 HDR 優先。\n'
    'IG 指定圖片取自每條連結的 ?img_index=3（原輪播第 3 項）。缺少或格式無效時下載全部內容；位置超出範圍或不是圖片時下載可確認的圖片，沒有圖片則保留原內容，並說明原因。\n'
    'Instagram 限時動態預設只傳連結指定的一則；沒有指定編號時取最新一則。可在 /settings instagram 選「全部限時動態」。TikTok 只選貼文全部內容或作者全部限時動態。設定只影響新工作。\n'
    'HDR 預設開啟：有 HDR 就優先，沒有則使用一般版本。\n\n'
    '<b>畫質與檔案限制</b>\n'
    'YouTube 優先下載相容 MP4，原片直接傳送，不重新壓制。\n'
    '單一檔案 500 MB 以內直接傳送；超過 500 MB 改提供下載連結並標示原因。\n'
    '單一檔案下載超過 2000 MB 立即取消；已知超限會提前停止。\n'
    '下載連結與已完成交付的連結檔案保留 2 小時，請及時保存。\n'
    'MB 以 1,000,000 bytes 計算。\n\n'
    '<b>常用指令</b>\n'
    '/help — 查看使用說明\n/settings — 我的下載偏好\n\n'
    '限時動態只包含目前可存取的內容。')
COMMANDS = [{'command': key, 'description': label} for key, label in (
    ('help','查看使用說明'),('settings','我的下載偏好'))]
ADMIN_COMMAND = {'command':'admin', 'description':'管理使用者與服務狀態'}
OWNER_COMMANDS = COMMANDS + [ADMIN_COMMAND, {'command':'translate', 'description':'翻譯文字或圖片成繁體中文'}]
STATES = {'choice':'等待選擇','queued':'排隊中','running':'下載中','cooldown':'冷卻中',
    'sending':'傳送中','paused':'已暫停','partial':'部分完成／需要檢查',
    'completed':'已完成','failed':'失敗','cancelled':'已取消'}
PAGE = 5


def initialize():
    with c.db() as db:
        db.executescript('''
        DROP INDEX IF EXISTS jobs_chat_created;
        DROP INDEX IF EXISTS jobs_created;
        CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs(state,created DESC);
        CREATE INDEX IF NOT EXISTS files_job ON files(job,id);
        ''')


def owner_id():
    with c.db() as db: row = db.execute("SELECT id FROM bot_users WHERE state='owner'").fetchone()
    return row['id'] if row else None


def is_owner(actor):
    return isinstance(actor, int) and not isinstance(actor, bool) and actor == owner_id()


def clean(value, limit=160):
    return ''.join(ch for ch in str(value or '') if ch.isprintable())[:limit]


def date(value):
    return datetime.fromtimestamp(value, ZoneInfo('Asia/Hong_Kong')).strftime('%m-%d %H:%M') if value else '—'


def identity(row):
    return (clean(row['name']) or '未提供名稱') + (' (@'+clean(row['username'],64)+')' if row['username'] else '（未設定 username）')


def button(label, data):return {'text':label,'callback_data':'ui:'+data}


def help_text():
    from . import links
    text = HELP
    if not limits.local_api_enabled():
        text = text.replace('500 MB','49 MiB')
    if not links.enabled():
        text += '\n尚未設定下載網址，超過傳送上限的檔案會保留。'
    return text


def home(actor):
    rows=[[button('使用說明','help')]]
    if is_owner(actor):rows.append([button('管理介面','admin')])
    return rows


def respond(send, actor, text, rows=None):
    data={'chat_id':actor,'text':text,'parse_mode':'HTML','link_preview_options':{'is_disabled':True}}
    if rows:data['reply_markup']={'inline_keyboard':rows}
    return send('sendMessage',data)


def page_number(value):
    if not re.fullmatch(r'\d{1,5}',value):raise ValueError('無效頁數')
    return int(value)


def navigation(rows, prefix, page, more):
    buttons=[]
    if page:buttons.append(button('上一頁',prefix+':'+str(page-1)))
    if more:buttons.append(button('下一頁',prefix+':'+str(page+1)))
    if buttons:rows.append(buttons)


def users(send,actor,page):
    if not is_owner(actor):raise PermissionError
    with c.db() as db:found=[dict(r) for r in db.execute('SELECT * FROM bot_users ORDER BY requested DESC,id DESC LIMIT ? OFFSET ?',(PAGE+1,page*PAGE))]
    lines=['<b>使用者</b>'];rows=[]
    for u in found[:PAGE]:
        lines.append(escape(identity(u))+'\nID '+str(u['id'])+' · '+escape(u['state'])+' · 最近 '+date(u['last_seen']))
        rows.append([button(clean(u['name'],30) or str(u['id']),'user:'+str(u['id'])+':0')])
    navigation(rows,'users',page,len(found)>PAGE)
    rows.append([button('管理介面','admin')]);respond(send,actor,'\n\n'.join(lines),rows)


def system_status(send,actor):
    if not is_owner(actor):raise PermissionError
    with c.db() as db:
        counts=list(db.execute('SELECT state,count(*) FROM jobs GROUP BY state'))
    poll=c.get_setting('telegram_last_poll',0)
    lines=['<b>系統狀態</b>', '下載服務：'+('啟用' if c.get_setting('enabled',False) else '暫停'),
           '最近 Telegram 接收：'+date(poll)+'（香港）',
           '儲存空間剩餘：'+f'{shutil.disk_usage(c.DATA).free/1024**3:.1f} GiB',
           *[escape(STATES.get(state,state))+': '+str(count) for state,count in counts]]
    for platform in accounts.DOWNLOAD_PLATFORMS:
        cooldown=c.get_setting(platform+'_cooldown',0)
        if cooldown>time.time():lines.append(accounts.LABELS[platform]+' 冷卻至 '+date(cooldown))
    respond(send,actor,'\n'.join(lines),[[button('重新整理','system'),button('管理介面','admin')]])


def route(send,actor,data):
    parts=data.split(':');action=parts[0]
    if action in ('cancel','confirm','canceldo'):
        respond(send,actor,'取消工作功能已停用；現有工作會繼續處理。',home(actor));return
    if action!='help' and not is_owner(actor):raise PermissionError
    if action=='help' and len(parts)==1:respond(send,actor,help_text(),home(actor))
    elif action=='admin' and len(parts)==1:
        if not is_owner(actor):raise PermissionError
        respond(send,actor,'<b>管理介面</b>',[
            [button('使用者','users:0'),button('系統狀態','system')],
            [{'text':'管理設定','url':c.ORIGIN+c.BASE}]])
    elif action=='users' and len(parts)==2:users(send,actor,page_number(parts[1]))
    elif action=='user' and len(parts)==3:
        if not is_owner(actor):raise PermissionError
        with c.db() as db:u=db.execute('SELECT * FROM bot_users WHERE id=?',(int(parts[1]),)).fetchone()
        if not u:raise PermissionError
        respond(send,actor,escape(identity(u))+f'\nTelegram ID：{u["id"]}\n首次使用：{date(u["requested"])}（香港）\n最近活動：{date(u["last_seen"])}（香港）\n權限：'+escape(u['state']))
    elif action=='system' and len(parts)==1:system_status(send,actor)
    else:raise ValueError('無效操作')


def handle(update,send):
    """Return True only for this UI's commands/callbacks. No access by UI visibility."""
    q=update.get('callback_query');m=q.get('message',{}) if q else update.get('message',{})
    chat=m.get('chat',{});actor=(q.get('from',{}) if q else m.get('from',{})).get('id')
    if chat.get('type')!='private' or actor!=chat.get('id') or not access.allowed(actor):return False
    if q:
        data=q.get('data','')
        if not data.startswith('ui:'):return False
        try:send('answerCallbackQuery',{'callback_query_id':q['id']})
        except Exception:pass
        data=data[3:]
    else:
        text=m.get('text','').strip();parts=text.split(maxsplit=1)
        command=parts[0].split('@')[0] if parts else ''
        mapping={'/start':'help','/help':'help','/admin':'admin'}
        if command not in mapping:return False
        data=mapping[command]
    try:route(send,actor,data)
    except PermissionError:respond(send,actor,'無權查看或操作這項內容。')
    except (ValueError,OverflowError):respond(send,actor,'操作無效，請重新開啟選單。',home(actor))
    return True


def publish_profile(send):
    """Explicit deployment action. Verify mutations by reading the same scopes."""
    owner=owner_id()
    if not owner:raise ValueError('Owner must be configured')
    for language in ('','zh'):
        send('setMyShortDescription',{'short_description':SHORT_DESCRIPTION,'language_code':language})
        send('setMyDescription',{'description':DESCRIPTION,'language_code':language})
        assert send('getMyShortDescription',{'language_code':language})['short_description']==SHORT_DESCRIPTION
        assert send('getMyDescription',{'language_code':language})['description']==DESCRIPTION
        for scope,commands in (({'type':'default'},COMMANDS),({'type':'all_private_chats'},COMMANDS),
                               ({'type':'chat','chat_id':owner},OWNER_COMMANDS)):
            send('setMyCommands',{'commands':commands,'scope':scope,'language_code':language})
            assert send('getMyCommands',{'scope':scope,'language_code':language})==commands
    send('setChatMenuButton',{'menu_button':{'type':'commands'}})
    assert send('getChatMenuButton',{})['type']=='commands'
    return {'profile_verified':True,'public_commands':len(COMMANDS),'owner_commands':len(OWNER_COMMANDS)}
