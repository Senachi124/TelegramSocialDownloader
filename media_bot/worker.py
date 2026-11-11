"""Polling and platform workers. Telegram delivery uncertainty is never auto-retried."""
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder
from . import core as c
from . import config,tiktok,video,inbox,twitter,limits,preferences,links

class TelegramError(Exception):
    def __init__(self,code,retry=0):self.code=code;self.retry=retry

_transport=threading.local()
STOP=threading.Event()
WAKE={platform:threading.Event() for platform in config.PLATFORMS}
# Striped locks bound memory while preventing duplicate initial status messages.
_STATUS_LOCKS=[threading.RLock() for _ in range(64)]
_LARGE_SEND=threading.BoundedSemaphore(1)

def _upload_size(files):
    total=0
    for item in (files or {}).values():
        try:size=os.fstat(item[1].fileno()).st_size
        except (AttributeError,OSError,TypeError):continue
        if size>limits.telegram_file_limit():
            raise ValueError('檔案超過目前 Telegram 傳送上限，請改用下載連結')
        total+=size
    return total

def _local_file_payload(data,files):
    """Use local paths only for opened, verified files inside Bot media storage."""
    if not limits.local_api_enabled() or os.environ.get('MEDIA_BOT_LOCAL_FILE_UPLOADS')!='1' or len(files)!=1:
        return None
    key,item=next(iter(files.items()))
    if key not in ('document','video','audio','animation','photo','voice','video_note'):
        return None
    stream=item[1]
    try:
        path=c.safe_file(stream.name).resolve(strict=True)
        stat=path.stat();opened=os.fstat(stream.fileno())
        if (stat.st_dev,stat.st_ino,stat.st_size)!=(opened.st_dev,opened.st_ino,opened.st_size):return None
    except (AttributeError,OSError,TypeError,ValueError):return None
    # Official Local API treats bare absolute paths as remote file IDs.
    return {**data,key:path.as_uri()}

def telegram(method,data,files=None):
    token=config.token()
    if not token:raise TelegramError(401)
    if not hasattr(_transport,'session'):_transport.session=requests.Session()
    base=limits.telegram_api_base()
    _transport.session.trust_env=not limits.local_api_enabled()
    url=base+'/bot'+token+'/'+method
    size=_upload_size(files)
    # Long media transfers never block independent getUpdates/status sessions.
    with _LARGE_SEND if size>=limits.LARGE_UPLOAD_THRESHOLD else contextlib.nullcontext():
        if files:
            local=_local_file_payload(data,files)
            if local is not None:
                r=_transport.session.post(url,json=local,timeout=(5,limits.MEDIA_SEND_TIMEOUT),allow_redirects=False)
            else:
                fields={k:str(v) for k,v in data.items()}
                fields.update(files);body=MultipartEncoder(fields=fields)
                r=_transport.session.post(url,data=body,headers={'Content-Type':body.content_type},timeout=(5,limits.MEDIA_SEND_TIMEOUT),allow_redirects=False)
        else:
            r=_transport.session.post(url,json=data,timeout=(5,35 if method=='getUpdates' else 10),allow_redirects=False)
        try:
            try:p=r.json()
            except ValueError:raise TelegramError(r.status_code)
            if not p.get('ok'):
                raise TelegramError(p.get('error_code',500),p.get('parameters',{}).get('retry_after',0))
            return p['result']
        finally:r.close()


def status(job,text,buttons=None):
    with _STATUS_LOCKS[int(c.digest(job['id'])[:8],16)%len(_STATUS_LOCKS)]:
        current=c.job_by_id(job['id'])
        if current and current['status_id']:job['status_id']=current['status_id']
        if current and current['message']==text and current['status_id'] and not buttons:return
        c.update(job['id'],message=text)
        data={'chat_id':job['chat'],'text':f"#{job['id'][:6]} · {text}"}
        if buttons:data['reply_markup']={'inline_keyboard':buttons}
        try:
            if job.get('status_id'):
                data['message_id']=job['status_id'];telegram('editMessageText',data)
            else:
                result=telegram('sendMessage',data);c.update(job['id'],status_id=result['message_id']);job['status_id']=result['message_id']
        except Exception:pass  # Durable state retains the message; never log token URLs.

def choice(job):
    status(job,'要下載此貼文，還是作者全部限時動態？',[[{'text':'此貼文內容','callback_data':job['id']+':post'},
           {'text':'全部限時動態','callback_data':job['id']+':stories'}]])

def authorized_update(u):
    callback=u.get('callback_query')
    message=callback.get('message',{}) if callback else u.get('message',{})
    actor=callback.get('from',{}) if callback else message.get('from',{})
    chat=message.get('chat',{})
    return (chat.get('type')=='private' and actor.get('id')==chat.get('id')
            and not actor.get('is_bot') and config.allowed(chat.get('id')))

def help_text_for_runtime():
    text='貼上 Instagram、TikTok、Twitter / X 或 YouTube HTTPS 連結；每則最多 50 個。\n/settings 設定 IG／TikTok 貼文或限時動態，以及 YouTube 畫質／HDR。IG「指定連結中的圖片」讀取每條連結的 ?img_index=3（原輪播第 3 項）；缺少或格式無效時下載全部內容，位置超出範圍或不是圖片時下載可確認的圖片，沒有圖片則保留原內容，並說明原因。IG 限時動態一律下載全部可存取內容，不需設定序號；TikTok 只選貼文全部內容或作者全部限時動態。新工作保存設定，不再逐次詢問。\n預設 HDR 優先，然後最高畫質；相同畫質優先 H.264。若偏好無法套用，會說明備援。\n單檔下載超過 2000 MB 立即取消，已有檔案保留。MB 使用十進位。\n'
    text+=('Local Bot API：單檔最多 500 MB 直接傳送；大於 500 MB 改提供下載連結。' if limits.local_api_enabled() else '目前使用雲端 Bot API：單檔達 49 MiB 改提供下載連結。')
    text+=('網址及已確認交付的連結檔案只保留 2 小時；直接傳送成功的檔案保留 24 小時。' if links.enabled() else '尚未設定下載網址：超過傳送上限的檔案保留本機並標記部分完成，無法提供連結。')
    return text

def handle_update(u):
    if not authorized_update(u):return
    if preferences.handle(u,telegram):return
    if 'callback_query' in u:
        q=u['callback_query'];message=q.get('message',{});chat=message.get('chat',{})
        if chat.get('type')!='private':return
        parts=q.get('data','').split(':');job=c.job_by_id(parts[0])
        if not job or job['chat']!=q['from']['id'] or job['chat']!=chat['id'] or len(parts)!=2 or parts[1] not in ('post','stories'):return
        with c.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not config.allowed(chat['id']):return
            changed=db.execute("UPDATE jobs SET state='queued',mode=? WHERE id=? AND state='choice'",(parts[1],job['id'])).rowcount
        with contextlib.suppress(Exception):telegram('answerCallbackQuery',{'callback_query_id':q['id'],'text':'已加入佇列' if changed else '此工作已處理'})
        if changed:
            status(job,'已加入佇列');WAKE[job['platform']].set()
        return
    m=u.get('message',{});chat=m.get('chat',{})
    if chat.get('type')!='private' or m.get('from',{}).get('is_bot'):return
    chat_id=chat['id'];text=m.get('text','').strip()
    if text.startswith('/'):
        command=text.split()[0].split('@')[0]
        help_text=help_text_for_runtime()
        telegram('sendMessage',{'chat_id':chat_id,'text':help_text if command in ('/start','/help') else '請輸入 /help 查看使用說明。'})
    else:
        urls=c.extract_urls(text)
        if not urls:telegram('sendMessage',{'chat_id':chat_id,'text':'請貼上 Instagram、TikTok、Twitter / X 或 YouTube HTTPS 連結'});return
        if len(urls)>1:
            try:
                key=c.enqueue_batch(str(u['update_id'])+':batch',chat_id,urls,m.get('message_id'),preferences=preferences.load(chat_id))
                job=c.job_by_id(key)
                if not job['status_id']:status(job,f'已建立工作，處理 {len(urls)} 個連結')
                if job['state'] in ('queued','running','cooldown','sending'):WAKE[job['platform']].set()
            except ValueError as e:
                data={'chat_id':chat_id,'text':str(e)}
                if m.get('message_id'):data['reply_parameters']={'message_id':m['message_id']}
                telegram('sendMessage',data)
            return
        for n,url in enumerate(urls):
            try:
                key=c.enqueue(str(u['update_id'])+':'+str(n),chat_id,url,preferences=preferences.load(chat_id));job=c.job_by_id(key)
                if job['state']=='choice':choice(job)
                elif not job['status_id']:status(job,job['message'] or '解析中／等待下載')
                WAKE[job['platform']].set()
            except ValueError as e:telegram('sendMessage',{'chat_id':chat_id,'text':str(e)})

def _report_worker_error(label,error,health=None):
    logging.getLogger(__name__).warning('%s failed (%s); retrying with progress preserved',label,type(error).__name__)
    if health is not None:
        try:c.setting('telegram_health',health)
        except Exception:logging.getLogger(__name__).warning('Telegram health status unavailable; polling remains active')


def poll(stop=None):
    stop=stop or threading.Event()
    while not stop.is_set():
        delay=0
        try:
            if not c.get_setting('enabled',False):stop.wait(3);continue
            slots=inbox.capacity()
            if slots==0:stop.wait(.2);continue
            offset=c.get_setting('telegram_offset',0)
            updates=telegram('getUpdates',{'offset':offset,'timeout':25,'limit':min(100,slots),'allowed_updates':['message','callback_query']})
            # Persist received commands before optional health reporting. Their
            # offset and inbox rows commit together; retry never reruns a handler.
            inbox.store(updates)
            c.setting_many({'telegram_health':'已連線','telegram_last_poll':time.time()})
        except TelegramError as error:
            _report_worker_error('Telegram polling',error,'Token／Webhook 設定需檢查' if error.code in (401,409) else 'Telegram 暫時不可用')
            delay=max(5,min(error.retry or 15,60))
        except Exception as error:
            _report_worker_error('Telegram polling',error,'待設定或連線暫時中斷')
            delay=5
        if delay:stop.wait(delay)

def cancelled(job):return bool(c.job_by_id(job['id'])['cancel']) or not config.allowed(job['chat'])

def disk_ok():
    if shutil.disk_usage(c.DATA).free<5*1024**3:raise ValueError('可用空間不足 5 GiB，暫停新增下載')
    size=sum(p.stat().st_size for p in (c.DATA/'media').rglob('*') if p.is_file())
    if size>=3*1024**3:raise ValueError('Bot 暫存已達 3 GiB，請檢查未交付工作')

def add_files(job,files):
    with c.db() as db:
        for f in files:db.execute('INSERT OR IGNORE INTO files(job,path,name,size) VALUES (?,?,?,?)',(job['id'],f['path'],f['name'],f['size']))

def _oversized_download(root):
    # Never total the directory: split video/audio and their merged output coexist.
    for path in root.rglob('*'):
        try:
            if not path.is_symlink() and path.is_file() and path.stat().st_size>limits.MAX_DOWNLOAD_BYTES:
                return True
        except FileNotFoundError:pass
    return False

def _cancel_oversized(job,result):
    if not (result.get('oversize') or result.get('failure')=='too_large'
            or any(f.get('size',0)>limits.MAX_DOWNLOAD_BYTES for f in result.get('files',[]))):
        return False
    c.update(job['id'],state='cancelled',cancel=1,retry_at=0)
    status(job,'單一檔案超過 2000 MB，已立即取消下載；不會傳送，現有檔案保留待管理員處理。')
    return True

def capture(job,cookies):
    root=c.DATA/'media'/job['id']
    if '_batch_position' in job:root=root/('item-'+str(job['_batch_position']))
    root.mkdir(parents=True,exist_ok=True)
    result_path=root/'result.json';result_path.unlink(missing_ok=True)
    runner='media_bot.gallery_runner'
    process=subprocess.Popen([sys.executable,'-m',runner],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,text=True)
    try:
        snapshot=json.loads(job.get('preferences') or '{}')
        preferences=snapshot.get(job['platform'],{}) if isinstance(snapshot,dict) else {}
        process.stdin.write(json.dumps({'platform':job['platform'],'mode':job['mode'],'url':job['url'],'root':str(root),'cookies':cookies,'preferences':preferences}));process.stdin.close()
        deadline=time.monotonic()+1800
        while process.poll() is None:
            if _oversized_download(root):
                with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):process.wait(timeout=2)
                return {'code':1,'retry_at':0,'auth_required':False,'files':[],
                        'failure':'too_large','oversize':True}
            if STOP.is_set() or cancelled(job) or time.monotonic()>deadline or shutil.disk_usage(c.DATA).free<5*1024**3:
                os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=10)
                if STOP.is_set():return None
                if not cancelled(job):raise ValueError('下載逾時或磁碟空間不足，已保留檔案')
                break
            time.sleep(0.2)
        if cancelled(job):return None
        result=json.loads(result_path.read_text())
        if _oversized_download(root):result.update(code=1,failure='too_large',oversize=True,files=[])
        return result
    finally:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
            process.wait()

def download(job):
    if cancelled(job):return False
    platform=job['platform'];label=config.LABELS[platform]
    retry=c.get_setting(platform+'_cooldown',0)
    if retry>time.time():
        c.update(job['id'],state='cooldown',retry_at=retry);status(job,label+' 冷卻至 '+datetime.fromtimestamp(retry,timezone.utc).isoformat(timespec='minutes'));return False
    cookies=config.cookies(platform)
    if not cookies and platform in ('instagram','twitter'):
        c.update(job['id'],state='paused');status(job,label+' 需要設定有效 Cookie');return False
    disk_ok();status(job,label+' 下載中（使用已保存 Cookie）' if platform=='tiktok' and cookies else label+' 下載中')
    result=capture(job,cookies)
    if result is None or cancelled(job):return False
    preferences.record_notice(job['id'],result.get('preference_notice'))
    if _cancel_oversized(job,result):return False
    if result['retry_at']:
        c.update(job['id'],state='cooldown',retry_at=result['retry_at'])
        reason='每分鐘媒體配額已用完' if result.get('wait_reason')=='quota' else '平台限流'
        status(job,label+' '+reason+'，將於 '+datetime.fromtimestamp(result['retry_at'],timezone.utc).strftime('%H:%M:%S UTC')+' 自動續作');return False
    if platform!='youtube' and (result.get('auth_required') or result.get('failure')=='login_required'):
        c.update(job['id'],state='paused');status(job,label+' 此工作帳號的 Cookie 需在設定更新');return False
    add_files(job,result['files'])
    reason=tiktok.failure_message(result.get('failure'),job['mode']) if platform=='tiktok' and result['code'] else ''
    if platform=='twitter' and result['code']:reason=twitter.failure_message(result.get('failure'))
    if platform=='youtube' and result['code']:
        from .yt_runner import youtube_failure_message
        reason=youtube_failure_message(result.get('failure'))
    if platform=='twitter' and result.get('no_media'):
        c.update(job['id'],state='completed',delivered=time.time());status(job,'此 '+label+' 貼文沒有圖片或影片');return False
    if result['code'] and not result['files']:
        c.update(job['id'],state='failed');status(job,reason or label+' 擷取失敗；請檢查 Cookie、網址或平台限制');return False
    c.setting('partial:'+job['id'],bool(result['code']));c.setting('partial_reason:'+job['id'],reason)
    return True

def _batch_next(job):
    """Hand off only after all writes for the current item have committed."""
    with c.db() as db:
        row=db.execute("SELECT platform FROM batch_items WHERE job=? AND state='pending' ORDER BY position LIMIT 1",(job['id'],)).fetchone()
        if row:
            db.execute("UPDATE jobs SET platform=?,state='queued',retry_at=0 WHERE id=?",(row['platform'],job['id']))
    if row:WAKE[row['platform']].set()
    return bool(row)

def _batch_notify_errors(job):
    with c.db() as db:
        message=db.execute('SELECT message_id FROM batch_requests WHERE job=?',(job['id'],)).fetchone()
        rows=[dict(r) for r in db.execute("SELECT * FROM batch_items WHERE job=? AND state='failed' AND error_notified=0 ORDER BY position",(job['id'],))]
    for item in rows:
        if cancelled(job):return False
        # Mark before sending: a transport failure has an unknown outcome and
        # must not automatically duplicate a message after a worker restart.
        with c.db() as db:
            changed=db.execute("UPDATE batch_items SET error_notified=1 WHERE job=? AND position=? AND error_notified=0",(job['id'],item['position'])).rowcount
        if not changed:continue
        data={'chat_id':job['chat'],'text':'連結處理失敗：\n'+item['original_url']+'\n'+item['error']}
        if message and message['message_id']:data['reply_parameters']={'message_id':message['message_id'],'allow_sending_without_reply':True}
        try:
            telegram('sendMessage',data)
        except TelegramError as error:
            with c.db() as db:db.execute('UPDATE batch_items SET error_notified=0 WHERE job=? AND position=?',(job['id'],item['position']))
            c.update(job['id'],state='cooldown' if error.code==429 else 'paused',retry_at=time.time()+max(5,error.retry))
            status(job,'錯誤訊息尚未送出，工作保留待重試');return False
        except Exception:
            # Retain the uncertain marker for delivery review; continue with
            # other links and deliver their successfully downloaded media.
            continue
        with c.db() as db:db.execute('UPDATE batch_items SET error_notified=2 WHERE job=? AND position=?',(job['id'],item['position']))
    return True

def process_batch(job):
    """Process one durable item, then hand the parent to its next platform."""
    if cancelled(job):
        c.update(job['id'],state='cancelled',cancel=1);return
    with c.db() as db:
        row=db.execute("SELECT * FROM batch_items WHERE job=? AND state='pending' ORDER BY position LIMIT 1",(job['id'],)).fetchone()
    if not row:
        if not _batch_notify_errors(job):return
        with c.db() as db:
            failed=db.execute("SELECT count(*) FROM batch_items WHERE job=? AND state='failed'",(job['id'],)).fetchone()[0]
            uncertain=db.execute("SELECT count(*) FROM batch_items WHERE job=? AND error_notified=1",(job['id'],)).fetchone()[0]
        c.setting('partial:'+job['id'],bool(failed or uncertain))
        c.setting('partial_reason:'+job['id'],'錯誤訊息傳送結果未明，傳送狀態需確認。' if uncertain else '')
        deliver(job);return
    item=dict(row)
    # A handoff can survive a crash between committing an item and scheduling
    # the next platform, or be reached following a manual retry.
    if item['platform']!=job['platform']:
        _batch_next(job);return
    try:
        if item['mode']=='resolve':
            status(job,'正在解析分享連結')
            retry=c.get_setting(item['platform']+'_cooldown',0)
            if retry>time.time():raise c.RateLimited(retry)
            url=c.resolve(item['url']);platform,mode=c.classify(url)
            if mode=='resolve':raise ValueError('不支援的分享連結')
            mode,notice=preferences.route(platform,mode,url,json.loads(job.get('preferences') or '{}').get(platform,{}))
            preferences.record_notice(job['id'],notice)
            if mode=='choice':mode='post'
            with c.db() as db:db.execute('UPDATE batch_items SET url=?,platform=?,mode=? WHERE job=? AND position=?',(url,platform,mode,job['id'],item['position']))
            _batch_next(job);return
        platform=item['platform'];label=config.LABELS[platform]
        retry=c.get_setting(platform+'_cooldown',0)
        if retry>time.time():raise c.RateLimited(retry)
        cookies=config.cookies(platform)
        if not cookies and platform in ('instagram','twitter'):
            c.update(job['id'],state='paused');status(job,label+' 需要設定有效 Cookie');return
        try:disk_ok()
        except ValueError as error:
            c.update(job['id'],state='paused');status(job,str(error));return
        except OSError:
            c.update(job['id'],state='paused');status(job,'媒體儲存空間不可用，已保留工作待恢復');return
        status(job,label+' 下載中')
        capture_job=dict(job,url=item['url'],platform=platform,mode=item['mode'],_batch_position=item['position'])
        try:result=capture(capture_job,cookies)
        except OSError:
            c.update(job['id'],state='paused');status(job,'媒體儲存空間不可用，已保留工作待恢復');return
        except ValueError as error:
            if str(error)=='下載逾時或磁碟空間不足，已保留檔案':
                c.update(job['id'],state='paused');status(job,str(error));return
            raise
        if result is None or cancelled(job):return
        preferences.record_notice(job['id'],result.get('preference_notice'))
        if _cancel_oversized(job,result):return
        if result['retry_at']:raise c.RateLimited(result['retry_at'])
        reason=''
        if result.get('auth_required'):reason=label+' 此工作帳號的 Cookie 需在設定更新'
        elif result.get('no_media'):reason='此貼文沒有圖片或影片'
        elif platform=='youtube' and result['code']:
            from .yt_runner import youtube_failure_message
            reason=youtube_failure_message(result.get('failure'))
        elif result['code']:
            reason=(tiktok.failure_message(result.get('failure'),item['mode']) if platform=='tiktok' else twitter.failure_message(result.get('failure')) if platform=='twitter' else label+' 擷取失敗；請檢查網址或平台限制')
        elif not result['files']:reason='沒有目前可見的媒體'
        # Files and completion are committed together. A crash before this
        # transaction safely resumes from the item's own download directory.
        with c.db() as db:
            for f in result['files']:db.execute('INSERT OR IGNORE INTO files(job,path,name,size) VALUES (?,?,?,?)',(job['id'],f['path'],f['name'],f['size']))
            db.execute('UPDATE batch_items SET state=?,error=? WHERE job=? AND position=?',('failed' if reason else 'done',reason,job['id'],item['position']))
    except c.RateLimited as error:
        c.update(job['id'],state='cooldown',retry_at=error.until);status(job,'平台冷卻中，稍後自動續作');return
    except Exception as error:
        if c.is_busy(error):raise
        reason='下載暫時失敗；請檢查連結、Cookie 或平台限制'
        with c.db() as db:db.execute("UPDATE batch_items SET state='failed',error=? WHERE job=? AND position=?",(reason,job['id'],item['position']))
    # No writes after handing the parent to another platform worker.
    if not _batch_next(job):
        c.update(job['id'],state='queued',retry_at=0)
        WAKE[job['platform']].set()

def _save_file_receipts(pairs):
    def save(db):
        for file_id,message_id in pairs:
            db.execute("UPDATE files SET state='sent',telegram_id=? WHERE id=?",(str(message_id),file_id))
    c.retry_write(save)


def _mark_file_states(files,state):
    def save(db):
        for file in files:db.execute('UPDATE files SET state=? WHERE id=?',(state,file['id']))
    c.retry_write(save)


def _save_file_rejection(job,files,error):
    retry_at=time.time()+max(5,error.retry)
    def save(db):
        for file in files:db.execute("UPDATE files SET state='pending' WHERE id=?",(file['id'],))
        db.execute("UPDATE jobs SET state=?,retry_at=? WHERE id=? AND cancel=0 AND state!='cancelled'",
                   ('sending' if error.code==429 else 'paused',retry_at,job['id']))
    c.retry_write(save)


def _mark_delivery_uncertain(job):
    c.retry_write(lambda db:db.execute("UPDATE jobs SET state='partial' WHERE id=? AND cancel=0 AND state!='cancelled'",(job['id'],)))


def deliver(job):
    if cancelled(job):
        c.update(job['id'],state='cancelled',cancel=1);return
    if job.get('mode')=='batch':
        with c.db() as db:
            pending=db.execute("SELECT 1 FROM batch_items WHERE job=? AND state='pending' LIMIT 1",(job['id'],)).fetchone()
        if pending:
            _batch_next(job);return
    c.update(job['id'],state='sending');status(job,'正在傳送媒體')
    with c.db() as db:files=[dict(r) for r in db.execute('SELECT * FROM files WHERE job=? ORDER BY id',(job['id'],))]
    if _cancel_oversized(job,{'files':files}):return
    if any(f['state']=='uncertain' for f in files):
        _mark_delivery_uncertain(job);return
    # Telegram mobile/forwarded messages rely on sender-supplied dimensions.
    # Probe once per delivery; unknown dimensions fall back to an original file.
    dimensions={f['id']:video.attributes(c.safe_file(f['path'])) for f in files
                if f['state']=='pending' and Path(f['path']).suffix.lower()=='.mp4' and f['size']<=limits.telegram_file_limit()}
    # Telegram albums accept 2–10 photo/video entries. Preserve source order.
    pending=[]
    def album(batch):
        if len(batch)<2 or cancelled(job):return
        _mark_file_states(batch,'uncertain')
        try:
            with contextlib.ExitStack() as stack:
                attachments={};media=[]
                for n,f in enumerate(batch):
                    name='file'+str(n);path=c.safe_file(f['path']);stream=stack.enter_context(path.open('rb'))
                    attachments[name]=(f['name'],stream,mimetypes.guess_type(f['name'])[0] or 'application/octet-stream')
                    item={'type':'video' if path.suffix.lower()=='.mp4' else 'photo','media':'attach://'+name}
                    if item['type']=='video':item.update(dimensions[f['id']])
                    media.append(item)
                results=telegram('sendMediaGroup',{'chat_id':job['chat'],'media':json.dumps(media)},attachments)
            if not isinstance(results,list) or len(results)!=len(batch) or any(not isinstance(r,dict) or not r.get('message_id') for r in results):raise ValueError('Incomplete album response')
            _save_file_receipts([(f['id'],result['message_id']) for f,result in zip(batch,results)])
            for f in batch:f['state']='sent'
        except TelegramError as error:
            if error.code>=500 or error.code<400:
                _mark_delivery_uncertain(job)
                raise
            if error.code==400:
                _mark_file_states(batch,'pending');return  # Definite rejection: individual fallback.
            _save_file_rejection(job,batch,error)
            raise error
        except Exception:
            for f in batch:f['state']='uncertain'
            _mark_delivery_uncertain(job);raise
    try:
        candidates=[f for f in files if f['state'] not in ('sent','expired')]
        all_album=all(f['state']=='pending' and f['size']<limits.LARGE_UPLOAD_THRESHOLD and ((Path(f['path']).suffix.lower()=='.mp4' and bool(dimensions.get(f['id']))) or (Path(f['path']).suffix.lower() in ('.jpg','.jpeg','.png') and f['size']<9*1024**2)) for f in candidates)
        # In a batch, group eligible runs even if another item requires a
        # document or link fallback; those files are delivered individually.
        for f in candidates if all_album or job.get('mode')=='batch' else []:
            ext=Path(f['path']).suffix.lower()
            eligible=f['state']=='pending' and f['size']<limits.LARGE_UPLOAD_THRESHOLD and ((ext=='.mp4' and f['size']<=limits.telegram_file_limit() and bool(dimensions.get(f['id']))) or (ext in ('.jpg','.jpeg','.png') and f['size']<9*1024**2))
            if not eligible:
                album(pending);pending=[];continue
            pending.append(f)
            if len(pending)==10:album(pending);pending=[]
        album(pending)
    except Exception:
        status(job,'媒體分組傳送尚未完成，檔案保留待確認／重試');return
    for f in files:
        if cancelled(job):return
        if f['state'] in ('sent','expired','too_large'):continue
        if f['state']=='uncertain':
            c.update(job['id'],state='partial');status(job,'部分媒體傳送結果未明，請管理員確認後重試');return
        path=c.safe_file(f['path'])
        _mark_file_states([f],'uncertain')
        try:
            if f['size']>limits.telegram_file_limit():
                reason='檔案大於 500 MB，改提供下載連結。' if f['size']>limits.MAX_TELEGRAM_BYTES else '目前雲端傳送介面限制為 49 MiB，改提供下載連結。'
                if not links.enabled():
                    _mark_file_states([f],'too_large');f['state']='too_large'
                    c.setting('partial:'+job['id'],True)
                    c.setting('partial_reason:'+job['id'],reason.replace('改提供下載連結。','')+'尚未設定下載網址，檔案保留在本機，未傳送。')
                    continue
                result=telegram('sendMessage',{'chat_id':job['chat'],'text':f['name']+'\n'+reason+'\n檔案及連結保留 2 小時：'+links.create(f['id'])})
            else:
                ext=path.suffix.lower();kind='video' if ext=='.mp4' and dimensions.get(f['id']) else ('photo' if ext in ('.jpg','.jpeg','.png') and f['size']<9*1024**2 else 'document')
                data={'chat_id':job['chat']}
                if kind=='video':data.update(dimensions[f['id']])
                with path.open('rb') as stream:
                    try:result=telegram('send'+kind.title(),data,{kind:(f['name'],stream,mimetypes.guess_type(f['name'])[0] or 'application/octet-stream')})
                    except TelegramError as e:
                        if e.code!=400 or kind=='document':raise
                        stream.seek(0);result=telegram('sendDocument',{'chat_id':job['chat']},{'document':(f['name'],stream,'application/octet-stream')})
            if not isinstance(result,dict) or not result.get('message_id'):raise ValueError('Missing Telegram receipt')
            _save_file_receipts([(f['id'],result['message_id'])])
        except TelegramError as e:
            if e.code>=500 or e.code<400:
                _mark_delivery_uncertain(job);return
            _save_file_rejection(job,[f],e)
            status(job,'Telegram 拒絕傳送，檔案保留待重試');return
        except Exception:
            _mark_delivery_uncertain(job);status(job,'傳送結果未明，檔案保留；請管理員確認後重試');return
    if cancelled(job):return
    partial=c.get_setting('partial:'+job['id'],False)
    c.update(job['id'],state='partial' if partial else 'completed',delivered=time.time())
    from . import preferences
    status(job,((('部分下載成功，已傳送可用內容。'+c.get_setting('partial_reason:'+job['id'],'')) if partial else f'完成，共 {len(files)} 個檔案') if files else (('沒有可傳送的媒體。'+c.get_setting('partial_reason:'+job['id'],'')) if partial else '沒有目前可見的限時動態／媒體'))+preferences.notice_text(job['id']))

def platform_loop(platform,stop=None):
    stop=stop or threading.Event()
    while not stop.is_set():
        job=None
        WAKE[platform].clear()
        try:
            with c.db() as db:
                needs_cancel=db.execute("SELECT 1 FROM jobs WHERE cancel=1 AND state IN ('choice','queued','running','cooldown','sending','paused') LIMIT 1").fetchone()
            if needs_cancel:
                c.retry_write(lambda db:db.execute("UPDATE jobs SET state='cancelled' WHERE cancel=1 AND state IN ('choice','queued','running','cooldown','sending','paused')"))
            with c.db() as db:
                row=db.execute("SELECT * FROM jobs WHERE platform=? AND state IN ('queued','running','cooldown','sending') AND retry_at<=? ORDER BY created LIMIT 1",(platform,time.time())).fetchone()
            if not row or not c.get_setting('enabled',False):WAKE[platform].wait(0.5);continue
            job=dict(row)
            if cancelled(job):
                c.update(job['id'],state='cancelled',cancel=1);continue
            if job['mode']=='batch' and job['state']!='sending':
                c.update(job['id'],state='running');process_batch(job);continue
            if job['state']=='sending':deliver(job);continue
            if job['mode']=='resolve':
                status(job,'正在解析分享連結')
                if c.get_setting(platform+'_cooldown',0)>time.time():
                    retry=c.get_setting(platform+'_cooldown');c.update(job['id'],state='cooldown',retry_at=retry)
                    status(job,'平台冷卻中，稍後自動續作');continue
                url=c.resolve(job['url'])
                provider,mode=c.classify(url)
                if mode=='resolve':raise ValueError('不支援的分享連結')
                mode,notice=preferences.route(provider,mode,url,json.loads(job.get('preferences') or '{}').get(provider,{}))
                preferences.record_notice(job['id'],notice)
                c.update(job['id'],url=url,platform=provider,mode=mode,state='choice' if mode=='choice' else 'queued')
                if mode=='choice':choice(c.job_by_id(job['id']))
                continue
            c.update(job['id'],state='running')
            done=download(job)
            if cancelled(job):c.update(job['id'],state='cancelled')
            elif done:deliver(job)
            time.sleep(0.2)
        except c.RateLimited as error:
            try:
                if job is not None:
                    c.update(job['id'],state='cooldown',retry_at=error.until)
                    status(job,'平台冷卻，稍後自動續作')
            except Exception as failure:_report_worker_error('Download cooldown checkpoint',failure)
        except Exception as error:
            if c.is_busy(error):
                _report_worker_error('Download database',error)
            else:
                try:
                    if job is not None:
                        c.update(job['id'],state='failed')
                        status(job,'下載暫時失敗；請檢查連結、Cookie 或平台限制')
                except Exception as failure:_report_worker_error('Download failure checkpoint',failure)
                _report_worker_error('Download worker',error)
            stop.wait(2 if c.is_busy(error) else 5)


def _ensure_workers(workers):
    specs=[('telegram-poll',poll,(STOP,))]
    specs += [('telegram-inbox-'+str(i),inbox.loop,(handle_update,STOP)) for i in range(4)]
    specs += [('download-'+p,platform_loop,(p,STOP)) for p in config.PLATFORMS]
    for name,target,args in specs:
        previous=workers.get(name)
        if previous is not None and previous.is_alive():continue
        if previous is not None:logging.getLogger(__name__).warning('%s thread stopped; restarting',name)
        thread=threading.Thread(target=target,args=args,daemon=True,name=name)
        thread.start();workers[name]=thread


def maintenance(workers):
    """Supervise download workers and retain failed or uncertain media."""
    _ensure_workers(workers)
    for name,action in (('media cleanup',c.cleanup),('inbox cleanup',inbox.cleanup)):
        try:action()
        except Exception as error:
            logging.getLogger(__name__).warning('%s failed (%s); retry next pass',name,type(error).__name__)


def main():
    config.validate()
    STOP.clear()
    c.initialize()
    c.setting('enabled',True)
    old_handlers={}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT,signal.SIGTERM):
            old_handlers[sig]=signal.signal(sig,lambda *_:STOP.set())
    try:
        with c.db_keeper(),links.serving():_run_main()
    finally:
        STOP.set()
        for event in WAKE.values():event.set()
        inbox.READY.set()
        for sig,handler in old_handlers.items():signal.signal(sig,handler)


def _run_main():
    # Uncertain sends stay preserved and never get automatically duplicated.
    with c.db() as db:
        db.execute("UPDATE jobs SET state='partial',message='上次傳送結果未明，已保留檔案' WHERE id IN (SELECT job FROM files WHERE state='uncertain')")
    inbox.recover()
    workers={}
    try:
        while not STOP.is_set():
            maintenance(workers)
            STOP.wait(30)
    finally:
        STOP.set()
        for event in WAKE.values():event.set()
        inbox.READY.set()
        deadline=time.monotonic()+12
        for name,thread in workers.items():
            if name.startswith('download-'):thread.join(max(0,deadline-time.monotonic()))

if __name__=='__main__':main()
