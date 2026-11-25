"""Polling and platform workers. Telegram delivery uncertainty is never auto-retried."""
from urllib.parse import urlsplit
import contextlib
from collections import deque
from datetime import datetime
from functools import partial
import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder
from . import core as c
from . import access,tiktok,video,inbox,accounts,twitter,threads,pool_checks
from . import links,group_download,group_menu,limits,storage,translation,translation_albums

class TelegramError(Exception):
    def __init__(self,code,retry=0):self.code=code;self.retry=retry

class CaptureTimeout(ValueError):
    pass

class CaptureStorageError(ValueError):
    pass

STOP = threading.Event()
_transport=threading.local()
WAKE={platform:threading.Event() for platform in accounts.DOWNLOAD_PLATFORMS}
# Striped locks bound memory while preventing duplicate initial status messages.
_STATUS_LOCKS=[threading.RLock() for _ in range(64)]
_LARGE_SEND=threading.BoundedSemaphore(1)
_TRANSLATIONS=Queue(maxsize=8)
_TRANSLATION_BUSY=Queue(maxsize=1)


def _upload_size(files):
    total=0
    for item in (files or {}).values():
        try:size=os.fstat(item[1].fileno()).st_size
        except (AttributeError,OSError,TypeError):continue
        if size>limits.telegram_file_limit():
            raise ValueError('檔案超過目前 Telegram 傳送上限，請改用下載連結')
        total+=size
    return total


def _verified_file_uri(stream):
    try:
        path = c.safe_file(stream.name).resolve(strict=True)
        stat = path.stat()
        opened = os.fstat(stream.fileno())
        if (stat.st_dev, stat.st_ino, stat.st_size) == (opened.st_dev, opened.st_ino, opened.st_size):
            return path.as_uri()
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return None


def _local_file_payload(data, files, method=None):
    """Use local paths only for opened, verified files inside Bot media storage."""
    if not limits.local_api_enabled() or os.environ.get('MEDIA_BOT_LOCAL_FILE_UPLOADS') != '1':
        return None
    if method == 'sendMediaGroup':
        media = json.loads(data['media'])
        for item in media:
            field = item['media'].removeprefix('attach://')
            if field not in files:
                return None
            uri = _verified_file_uri(files[field][1])
            if uri is None:
                return None
            item['media'] = uri
        return {**data, 'media': media}
    if len(files) != 1:
        return None
    key, item = next(iter(files.items()))
    if key not in ('document', 'video', 'audio', 'animation', 'photo', 'voice', 'video_note'):
        return None
    # Telegram honors disable_content_type_detection only on multipart uploads.
    if key == 'document' and data.get('disable_content_type_detection'):
        return None
    uri = _verified_file_uri(item[1])
    return {**data, key: uri} if uri else None

def telegram(method,data,files=None):
    now=time.monotonic()
    if now>=getattr(_transport,'token_until',0):
        _transport.token=c.rpc(c.SECRET_SOCKET,'/telegram')['token']
        _transport.token_until=now+5
    token=_transport.token
    if not token:raise TelegramError(401)
    if not hasattr(_transport,'session'):_transport.session=requests.Session()
    base=limits.telegram_api_base()
    _transport.session.trust_env=not limits.local_api_enabled()
    url=base+'/bot'+token+'/'+method
    size=_upload_size(files)
    # Long media transfers never block independent getUpdates/status sessions.
    with _LARGE_SEND if size>=limits.LARGE_UPLOAD_THRESHOLD else contextlib.nullcontext():
        if files:
            local=_local_file_payload(data,files,method)
            if local is not None:
                r=_transport.session.post(url,json=local,timeout=(5,limits.MEDIA_SEND_TIMEOUT),allow_redirects=False)
            else:
                fields={k:json.dumps(v) if isinstance(v,(dict,list,bool)) else str(v) for k,v in data.items()}
                fields.update(files);body=MultipartEncoder(fields=fields)
                r=_transport.session.post(url,data=body,headers={'Content-Type':body.content_type},timeout=(5,limits.MEDIA_SEND_TIMEOUT),allow_redirects=False)
        else:
            r=_transport.session.post(url,json=data,timeout=(5,35 if method=='getUpdates' else 10),allow_redirects=False)
        try:
            try:p=r.json()
            except ValueError:raise ValueError('Unrecognized Telegram response') from None
            if not isinstance(p,dict) or type(p.get('ok')) is not bool:
                raise ValueError('Unrecognized Telegram response')
            if not p['ok']:
                # Local Bot API can return a single error after collecting both
                # successful and failed per-message callbacks for one album.
                description = p.get('description')
                description = description.strip().lower() if isinstance(description,str) else ''
                if method == 'sendMediaGroup' and (not description or 'failed to send message #' in description
                                                   or 'group send failed' in description):
                    raise ValueError('Album delivery could be partial')
                if p.get('error_code')==401:_transport.token_until=0
                raise TelegramError(p.get('error_code',500),p.get('parameters',{}).get('retry_after',0))
            return p['result']
        finally:r.close()


def group_reply(chat_id, message_id, thread_id=None):
    destination={'chat_id':chat_id, 'reply_parameters':{'message_id':message_id}}
    if thread_id is not None:
        destination['message_thread_id']=thread_id
    return destination


def _job_reply(job):
    if job.get('group_chat_id') is not None:
        return group_reply(job['group_chat_id'], job['original_message_id'], job.get('message_thread_id'))
    return {'chat_id':job['chat']}


def status(job,text,buttons=None,*,force=False):
    with _STATUS_LOCKS[int(c.digest(job['id'])[:8],16)%len(_STATUS_LOCKS)]:
        current=c.job_by_id(job['id'])
        if current and current['status_id']:job['status_id']=current['status_id']
        if current and current['message']==text and current['status_id'] and not buttons and not force:return
        c.update(job['id'],message=text)
        data={'text':f"#{job['id'][:6]} · {text}"}
        if buttons:data['reply_markup']={'inline_keyboard':buttons}
        try:
            if job.get('status_id'):
                data.update(chat_id=job.get('group_chat_id') or job['chat'],message_id=job['status_id'])
                telegram('editMessageText',data)
            else:
                data.update(_job_reply(job))
                result=telegram('sendMessage',data);c.update(job['id'],status_id=result['message_id']);job['status_id']=result['message_id']
        except Exception:pass  # The queue checkpoint is already committed.

def transition(job,text,**fields):
    # Save the safe alert classification and state together, even if Telegram is down.
    c.update(job['id'],message=text,**fields)
    status(job,text,force=True)


def choice(job):
    status(job,'要下載此貼文，還是作者全部限時動態？',[[{'text':'此貼文內容','callback_data':job['id']+':post'},
           {'text':'全部限時動態','callback_data':job['id']+':stories'}]])

def bot_username():
    """Verify a group address against getMe, refreshing after a rename or token change."""
    now=time.monotonic()
    bot_id=getattr(_transport,'token','').partition(':')[0]
    if now<getattr(_transport,'identity_until',0) and bot_id==_transport.identity_id:
        return _transport.identity_username
    me=telegram('getMe',{})
    if not me.get('is_bot') or not me.get('username'):
        raise ValueError('無法取得 Telegram Bot 帳號')
    _transport.identity_username=me['username'].lower()
    _transport.identity_id=str(me['id'])
    _transport.identity_until=now+60
    return _transport.identity_username


def authorized_update(u, *, from_inbox=False):
    callback=u.get('callback_query')
    message=callback.get('message',{}) if callback else u.get('message',{})
    actor=callback.get('from',{}) if callback else message.get('from',{})
    chat=message.get('chat',{})
    if chat.get('type') in ('group','supergroup'):
        target=group_download.target(message)
        # Durable group rows were admitted only after a successful getMe match.
        # Recheck membership here so a revocation while queued takes effect.
        return bool(not callback and target and access.allowed(actor.get('id'))
                    and (from_inbox or target==bot_username().lower()))
    if chat.get('type')!='private' or actor.get('id')!=chat.get('id') or actor.get('is_bot'):return False
    member=access.observe(actor)
    if not member:return False
    if member['state'] in ('owner','approved'):return True
    text=('申請已收到，等待管理員批准。批准後請重新貼上下載連結。' if member['state']=='pending' else '此帳號目前沒有使用權限，請聯絡管理員。')
    if member['notify']:
        text+='\n你的 Telegram User ID：'+str(actor['id'])
        if callback:
            with contextlib.suppress(Exception):telegram('answerCallbackQuery',{'callback_query_id':callback['id'],'text':text,'show_alert':True})
        else:
            with contextlib.suppress(Exception):telegram('sendMessage',{'chat_id':chat['id'],'text':text})
    return False

def handle_update(u, *, from_inbox=False):
    if not authorized_update(u,from_inbox=from_inbox):return
    from . import bot_ui,preferences
    message=u.get('message',{})
    if message.get('chat',{}).get('type') in ('group','supergroup'):
        _download_message(u['update_id'],message,message['from']['id'],in_group=True,
                          command=group_download.command(message))
        return
    if preferences.handle(u,telegram):return
    if bot_ui.handle(u,telegram):return
    if 'callback_query' in u:
        q=u['callback_query'];message=q.get('message',{});chat=message.get('chat',{})
        if chat.get('type')!='private':return
        parts=q.get('data','').split(':');job=c.job_by_id(parts[0])
        if not job or job['chat']!=q['from']['id'] or job['chat']!=chat['id'] or len(parts)!=2 or parts[1] not in ('post','stories'):return
        with c.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not access.allowed(chat['id'],db):return
            changed=db.execute("UPDATE jobs SET state='queued',mode=? WHERE id=? AND state='choice'",(parts[1],job['id'])).rowcount
        with contextlib.suppress(Exception):telegram('answerCallbackQuery',{'callback_query_id':q['id'],'text':'已加入佇列' if changed else '此工作已處理'})
        if changed:
            status(job,'已加入佇列');WAKE[job['platform']].set()
        return
    m=u.get('message',{});chat=m.get('chat',{})
    if chat.get('type')!='private' or m.get('from',{}).get('is_bot'):return
    chat_id=chat['id'];text=m.get('text','').strip()
    command=group_download.command(m)
    if command:
        if command[1] and command[1].lower()!=bot_username().lower():return
        _download_message(u['update_id'],m,chat_id,command=command)
    elif text.startswith('/'):
        telegram('sendMessage',{'chat_id':chat_id,'text':'未有這個指令，請輸入 /help 查看使用說明。'})
    else:_download_message(u['update_id'],m,chat_id)


def _download_message(update_id,message,requester,in_group=False,*,command=None):
    from . import preferences
    message_id=message.get('message_id')
    destination={'chat_id':message['chat']['id']}
    routing={}
    if in_group:
        thread=message.get('message_thread_id') if message.get('is_topic_message') else None
        destination=group_reply(message['chat']['id'],message_id,thread)
        routing={'group_chat_id':message['chat']['id'],'message_thread_id':thread}
    urls=group_download.command_urls(message,command) if command else c.extract_message_urls(message)
    if not urls:
        if not command and message.get('media_group_id'):
            return  # A shared album can be selected later with a reply.
        telegram('sendMessage',{**destination,'text':'請貼上 Instagram、TikTok、Twitter / X、Threads 或 YouTube HTTPS 連結，或用下載指令回覆含有連結的訊息或引文。'})
        return
    try:
        prefs=preferences.load(requester)
        if len(urls)>1:
            key=c.enqueue_batch(f'{update_id}:batch',requester,urls,message_id,preferences=prefs,**routing)
            job=c.job_by_id(key)
            if not job['status_id']:status(job,f'已建立工作，處理 {len(urls)} 個連結')
            if job['state'] in ('queued','running','cooldown','sending'):WAKE[job['platform']].set()
        else:
            if in_group:routing['original_message_id']=message_id
            key=c.enqueue(f'{update_id}:0',requester,urls[0],preferences=prefs,**routing)
            job=c.job_by_id(key)
            if job['state']=='choice':choice(job)
            elif not job['status_id']:status(job,(job.get('message')+'\n' if job.get('message') else '')+'解析中／等待下載')
            WAKE[job['platform']].set()
    except ValueError as error:
        if not in_group and len(urls)>1 and message_id:
            destination['reply_parameters']={'message_id':message_id}
        telegram('sendMessage',{**destination,'text':str(error)})

def _report_worker_error(label,error,health=None):
    logging.getLogger(__name__).warning('%s failed (%s); retrying with progress preserved',label,type(error).__name__)
    if health is not None:
        try:c.setting('telegram_health',health)
        except Exception:logging.getLogger(__name__).warning('Telegram health status unavailable; polling remains active')


def _admit_translation(message):
    match=translation.command(message)
    if not match and not translation.prompt_reply(message):return
    if not translation.authorized(message,match,bot_username):return
    thread=message.get('message_thread_id') if message.get('is_topic_message') else None
    reply=group_reply(message['chat']['id'],message['message_id'],thread)
    try:_TRANSLATIONS.put_nowait((message,reply))
    except Full:
        # Report one burst from the translation consumer, outside the polling thread.
        with contextlib.suppress(Full):
            _TRANSLATION_BUSY.put_nowait(reply)


def _translation_loop():
    while True:
        message,reply=_TRANSLATIONS.get()
        try:
            translation.handle(message,telegram,lambda: _transport.token,reply,bot_username=bot_username)
            match=translation.command(message)
            if match and match[1] and not translation.prompt_reply(message) and message['chat']['type'] in ('group','supergroup'):
                try:
                    group_menu.publish(message['from']['id'],message['chat']['id'],telegram)
                except Exception:
                    logging.getLogger(__name__).warning('Owner group menu registration unavailable')
        except Exception:
            logging.getLogger(__name__).warning('Telegram translation reply unavailable')
        finally:
            _TRANSLATIONS.task_done()
            del message,reply
        try:
            busy_reply=_TRANSLATION_BUSY.get_nowait()
        except Empty:
            continue
        try:
            telegram('sendMessage',{**busy_reply,'text':'翻譯忙碌中，請稍後再試。'})
        except Exception:
            logging.getLogger(__name__).warning('Translation busy notice unavailable')
        finally:
            _TRANSLATION_BUSY.task_done()


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
            if any(group_download.target(u.get('message',{})) for u in updates):
                inbox.store(updates,bot_username=bot_username())
            else:
                inbox.store(updates)
            # Translation runs in memory only after the durable offset advances.
            for update in updates:
                if update['update_id']<offset:continue
                try:_admit_translation(update.get('message',{}))
                except Exception:
                    logging.getLogger(__name__).warning('Telegram translation admission unavailable')
            c.setting_many({'telegram_health':'已連線','telegram_last_poll':time.time()})
        except TelegramError as error:
            _report_worker_error('Telegram polling',error,'Token／Webhook 設定需檢查' if error.code in (401,409) else 'Telegram 暫時不可用')
            delay=max(5,min(error.retry or 15,60))
        except Exception as error:
            _report_worker_error('Telegram polling',error,'待設定或連線暫時中斷')
            delay=5
        if delay:stop.wait(delay)

def cancelled(job):
    current = c.job_by_id(job['id'])
    return not current or bool(current['cancel']) or not access.allowed(job['chat'])

def disk_ok():
    if shutil.disk_usage(c.DATA).free<5*1024**3:raise CaptureStorageError('可用空間不足 5 GiB，暫停新增下載')
    storage.check(download=True)
    size=0
    for path in (c.DATA/'media').rglob('*'):
        try:
            if not path.is_symlink() and path.is_file():size+=path.stat().st_size
        except FileNotFoundError:continue
    if size>=3*1024**3:raise ValueError('Bot 暫存已達 3 GiB，請管理員處理未交付工作')

def _register_files(db, job_id, files):
    db.executemany(
        """INSERT INTO files(job,path,name,size,force_document,supports_streaming) VALUES (?,?,?,?,?,?)
           ON CONFLICT(job,path) DO UPDATE SET name=excluded.name, size=excluded.size,
             force_document=max(files.force_document,excluded.force_document),
             supports_streaming=max(files.supports_streaming,excluded.supports_streaming)
           WHERE files.state='pending'""",
        [(job_id, f['path'], f['name'], f['size'], bool(f.get('force_document')),
          bool(f.get('supports_streaming'))) for f in files],
    )


def add_files(job,files):
    with c.db() as db:
        _register_files(db, job['id'], files)

def _oversized_download(root):
    # Never total the directory: split video/audio and their merged output coexist.
    for path in root.rglob('*'):
        # Reserved scratch from a previous release is never delivered.
        if video.is_playback_scratch(path):
            continue
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


def capture(job,cookies,*,root=None,should_cancel=None,timeout=1800):
    if root is None:
        root=c.DATA/'media'/job['id']
        if '_batch_position' in job:root=root/('item-'+str(job['_batch_position']))
    if should_cancel is None:should_cancel=lambda:cancelled(job)
    root.mkdir(parents=True,exist_ok=True)
    video.cleanup_playback_scratch(root)
    result_path=root/'result.json';result_path.unlink(missing_ok=True)
    runner='media_bot.threads_runner' if job['platform']=='threads' else 'media_bot.gallery_runner'
    deadline=time.monotonic()+timeout
    process=subprocess.Popen([sys.executable,'-m',runner],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,start_new_session=True,text=True)
    try:
        snapshot=json.loads(job.get('preferences') or '{}')
        preferences=snapshot.get(job['platform'],{}) if isinstance(snapshot,dict) else {}
        process.stdin.write(json.dumps({'platform':job['platform'],'mode':job['mode'],'url':job['url'],'root':str(root),'cookies':cookies,'preferences':preferences}));process.stdin.close()
        while process.poll() is None:
            if _oversized_download(root):
                with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):process.wait(timeout=2)
                return {'code':1,'retry_at':0,'auth_required':False,'files':[],
                        'failure':'too_large','oversize':True}
            if STOP.is_set():
                with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):process.wait(timeout=10)
                return None
            if should_cancel():return None
            if time.monotonic()>deadline:raise CaptureTimeout('下載逾時，已保留檔案')
            if shutil.disk_usage(c.DATA).free<5*1024**3:raise CaptureStorageError('可用空間不足 5 GiB，已保留檔案')
            time.sleep(0.2)
        if should_cancel():return None
        result=json.loads(result_path.read_text())
        if _oversized_download(root):result.update(code=1,failure='too_large',oversize=True,files=[])
        return result
    finally:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):process.wait(timeout=2)
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
            process.wait()
        video.cleanup_playback_scratch(root)

def download(job):
    if cancelled(job):return False
    platform=job['platform'];label=accounts.LABELS[platform]
    retry=c.get_setting(platform+'_cooldown',0)
    if retry>time.time():
        c.update(job['id'],state='cooldown',retry_at=retry);status(job,label+' 冷卻至 '+datetime.fromtimestamp(retry,ZoneInfo('Asia/Hong_Kong')).isoformat(timespec='minutes'));return False
    credential=None;cookies={}
    credential=c.rpc(c.SECRET_SOCKET,'/select',{'platform':platform,'id':job.get('account_id','')})
    cookies=credential.get('cookies',{})
    if platform in ('threads','youtube') and credential.get('public') and not job.get('account_id'):
        credential=None;cookies={}
    elif not cookies:
        transition(job,label+' 帳號池沒有可用 Cookie，請管理員更新或啟用已綁定帳號',state='paused');return False
    else:
        c.update(job['id'],account_id=credential['id']);job['account_id']=credential['id']
    disk_ok();status(job,label+' 下載中（使用已保存 Cookie）' if platform=='tiktok' else label+' 下載中')
    result=capture(job,cookies)
    if result is None or cancelled(job):return False
    from . import preferences
    preferences.record_notice(job['id'],result.get('preference_notice'))
    if _cancel_oversized(job,result):return False
    outcome=('rate_limited' if result['retry_at'] and result.get('wait_reason')!='quota' else
             'auth_required' if result.get('auth_required') or result.get('failure')=='login_required' else
             'access_error' if result.get('failure')=='forbidden' else
             'failed' if result['code'] and result.get('failure')!='conversion_failed' else 'success')
    if credential is not None and not (result['retry_at'] and result.get('wait_reason')=='quota'):
        with contextlib.suppress(Exception):
            c.rpc(c.SECRET_SOCKET,'/report',{'platform':platform,'id':credential['id'],
                  'revision':credential['revision'],'outcome':outcome})
    if result['retry_at']:
        c.update(job['id'],state='cooldown',retry_at=result['retry_at'])
        reason='每分鐘媒體配額已用完' if result.get('wait_reason')=='quota' else '平台限流'
        status(job,label+' '+reason+'，將於 '+datetime.fromtimestamp(result['retry_at'],ZoneInfo('Asia/Hong_Kong')).strftime('%H:%M:%S')+' 自動續作');return False
    if platform in ('instagram','twitter','threads','youtube') and credential is not None and result['auth_required']:
        transition(job,label+' 此工作帳號的 Cookie 需在帳號池更新',state='paused');return False
    add_files(job,result['files'])
    reason=tiktok.failure_message(result.get('failure'),job['mode']) if platform=='tiktok' and result['code'] else ''
    if platform=='instagram' and result.get('failure')=='story_unavailable':reason='指定的限時動態已過期或目前無法存取。'
    if platform=='twitter' and result['code']:reason=twitter.failure_message(result.get('failure'))
    if platform=='threads' and result['code']:reason=threads.failure_message(result.get('failure'))
    if platform=='youtube' and result['code']:
        from .yt_runner import youtube_failure_message
        reason=youtube_failure_message(result.get('failure'))
    if result.get('failure') == 'conversion_failed':
        reason='影片播放版轉換失敗，原檔保留並按文件交付。'
    if platform in ('twitter','threads') and result.get('no_media'):
        transition(job,'此 '+label+' 貼文沒有圖片或影片',state='completed',delivered=time.time());return False
    if result['code'] and not result['files']:
        transition(job,reason or label+' 擷取失敗；請檢查 Cookie、網址或平台限制',state='failed');return False
    c.setting('partial:'+job['id'],bool(result['code']));c.setting('partial_reason:'+job['id'],reason)
    return True

def _batch_next(job):
    """Hand off only after all writes for the current item have committed."""
    with c.db() as db:
        row=db.execute("SELECT platform FROM batch_items WHERE job=? AND state='pending' ORDER BY position LIMIT 1",(job['id'],)).fetchone()
        if row:
            db.execute("UPDATE jobs SET platform=?,state='queued',retry_at=0,account_id='' WHERE id=?",(row['platform'],job['id']))
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
        data={**_job_reply(job),'text':'連結處理失敗：\n'+item['original_url']+'\n'+item['error']}
        if not job.get('group_chat_id') and message and message['message_id']:
            data['reply_parameters']={'message_id':message['message_id'],'allow_sending_without_reply':True}
        try:
            telegram('sendMessage',data)
        except TelegramError as error:
            with c.db() as db:db.execute('UPDATE batch_items SET error_notified=0 WHERE job=? AND position=?',(job['id'],item['position']))
            c.update(job['id'],state='cooldown' if error.code==429 else 'paused',retry_at=time.time()+max(5,error.retry))
            status(job,'錯誤訊息尚未送出，工作保留待重試');return False
        except Exception:
            # Retain the uncertain marker for operator review; continue with
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
        c.setting('partial_reason:'+job['id'],'錯誤訊息傳送結果未明，請管理員確認。' if uncertain else '')
        deliver(job);return
    item=dict(row)
    # A handoff can survive a crash between committing an item and scheduling
    # the next platform, or be reached following an administrator retry.
    if item['platform']!=job['platform']:
        _batch_next(job);return
    try:
        if item['mode']=='resolve':
            status(job,'正在解析分享連結')
            retry=c.get_setting(item['platform']+'_cooldown',0)
            if retry>time.time():raise c.RateLimited(retry)
            url=c.resolve(item['url']);platform,mode=c.classify(url)
            if mode=='resolve':raise ValueError('不支援的分享連結')
            from . import preferences
            mode,notice=preferences.route(platform,mode,url,json.loads(job.get('preferences') or '{}').get(platform,{}))
            preferences.record_notice(job['id'],notice)
            if mode=='choice':mode='post'
            with c.db() as db:db.execute('UPDATE batch_items SET url=?,platform=?,mode=? WHERE job=? AND position=?',(url,platform,mode,job['id'],item['position']))
            _batch_next(job);return
        platform=item['platform'];label=accounts.LABELS[platform]
        retry=c.get_setting(platform+'_cooldown',0)
        if retry>time.time():raise c.RateLimited(retry)
        credential=None;cookies={}
        credential=c.rpc(c.SECRET_SOCKET,'/select',{'platform':platform,'id':job.get('account_id','')})
        cookies=credential.get('cookies',{})
        if platform in ('threads','youtube') and credential.get('public') and not job.get('account_id'):
            credential=None;cookies={}
        elif not cookies:
            if platform in ('threads','youtube'):
                transition(job,label+' 此工作帳號不可用，請更新或啟用已綁定帳號',state='paused');return
            raise ValueError(label+' 帳號池沒有可用 Cookie，請管理員更新')
        else:
            c.update(job['id'],account_id=credential['id']);job['account_id']=credential['id']
        try:disk_ok()
        except ValueError as error:
            transition(job,str(error),state='paused');return
        except OSError:
            transition(job,'媒體儲存空間不可用，已保留工作待恢復',state='paused');return
        status(job,label+' 下載中')
        capture_job=dict(job,url=item['url'],platform=platform,mode=item['mode'],_batch_position=item['position'])
        try:result=capture(capture_job,cookies)
        except OSError:
            transition(job,'媒體儲存空間不可用，已保留工作待恢復',state='paused');return
        except ValueError as error:
            if isinstance(error, (CaptureTimeout, CaptureStorageError)) or str(error)=='下載逾時或磁碟空間不足，已保留檔案':
                transition(job,str(error),state='paused');return
            raise
        if result is None or cancelled(job):return
        from . import preferences
        preferences.record_notice(job['id'],result.get('preference_notice'))
        if _cancel_oversized(job,result):return
        outcome=('rate_limited' if result['retry_at'] and result.get('wait_reason')!='quota' else
                 'auth_required' if result.get('auth_required') or result.get('failure')=='login_required' else
                 'access_error' if result.get('failure')=='forbidden' else 'failed' if result['code'] and result.get('failure')!='conversion_failed' else 'success')
        if credential is not None and not (result['retry_at'] and result.get('wait_reason')=='quota'):
            with contextlib.suppress(Exception):c.rpc(c.SECRET_SOCKET,'/report',{'platform':platform,'id':credential['id'],'revision':credential['revision'],'outcome':outcome})
        if result['retry_at']:raise c.RateLimited(result['retry_at'])
        if platform in ('threads','youtube') and credential is not None and result.get('auth_required'):
            transition(job,label+' 此工作帳號的 Cookie 需在帳號池更新',state='paused');return
        reason=''
        if result.get('auth_required'):reason=threads.failure_message('login_required') if platform=='threads' else label+' 此工作帳號的 Cookie 需在帳號池更新'
        elif result.get('failure')=='conversion_failed':reason='影片播放版轉換失敗，原檔保留並按文件交付。'
        elif platform=='instagram' and result.get('failure')=='story_unavailable':reason='指定的限時動態已過期或目前無法存取。'
        elif result.get('no_media'):reason='此貼文沒有圖片或影片'
        elif platform=='youtube' and result['code']:
            from .yt_runner import youtube_failure_message
            reason=youtube_failure_message(result.get('failure'))
        elif result['code']:
            reason=(tiktok.failure_message(result.get('failure'),item['mode']) if platform=='tiktok' else twitter.failure_message(result.get('failure')) if platform=='twitter' else threads.failure_message(result.get('failure')) if platform=='threads' else label+' 擷取失敗；請檢查網址或平台限制')
        elif not result['files']:reason='沒有目前可見的媒體'
        # Files and completion are committed together. A crash before this
        # transaction safely resumes from the item's own download directory.
        with c.db() as db:
            _register_files(db, job['id'], result['files'])
            db.execute('UPDATE batch_items SET state=?,error=? WHERE job=? AND position=?',('failed' if reason else 'done',reason,job['id'],item['position']))
    except c.RateLimited as error:
        c.update(job['id'],state='cooldown',retry_at=error.until);status(job,'平台冷卻中，稍後自動續作');return
    except Exception as error:
        if c.is_busy(error):raise
        reason=str(error) if isinstance(error,ValueError) else '下載服務暫時失敗，請管理員重試'
        with c.db() as db:db.execute("UPDATE batch_items SET state='failed',error=? WHERE job=? AND position=?",(reason,job['id'],item['position']))
    # No writes after handing the parent to another platform worker.
    if not _batch_next(job):
        c.update(job['id'],state='queued',retry_at=0,account_id='')
        WAKE[job['platform']].set()

def _save_file_receipts(pairs):
    def save(db):
        for file_id,message in pairs:
            kind=next((k for k in ('video','document') if isinstance(message.get(k),dict)), '')
            db.execute("UPDATE files SET state='sent',telegram_id=?,telegram_media_kind=? WHERE id=?",
                       (str(message['message_id']),kind,file_id))
        if pairs:
            c.update_album_index(db, translation_albums.record_delivery, pairs, time.time())
    c.retry_write(save)


def _mark_file_states(files,state):
    def save(db):
        for file in files:db.execute('UPDATE files SET state=? WHERE id=?',(state,file['id']))
    c.retry_write(save)


def _save_file_rejection(job,files,error):
    retry_at=time.time()+max(5,error.retry)
    def save(db):
        for file in files:db.execute("UPDATE files SET state='pending' WHERE id=?",(file['id'],))
        db.execute("UPDATE jobs SET state=?,retry_at=?,message='Telegram 拒絕傳送，檔案保留待重試' WHERE id=? AND cancel=0 AND state!='cancelled'",
                   ('sending' if error.code==429 else 'paused',retry_at,job['id']))
    c.retry_write(save)


def _mark_delivery_uncertain(job):
    c.retry_write(lambda db:db.execute("UPDATE jobs SET state='partial',message='傳送結果未明，檔案已保留。' WHERE id=? AND cancel=0 AND state!='cancelled'",(job['id'],)))


def _delivery_batches(files, album_kind):
    # Telegram can mix photos and videos. Documents require their own album.
    pending = [(f, album_kind(f)) for f in files if f['state'] == 'pending']
    groups = {'visual': deque(), 'document': deque()}
    for f, kind in pending:
        if kind:
            groups[kind].append(f)
    for f, kind in pending:
        if kind is None:
            yield [f]
            continue
        group = groups[kind]
        if group and f is group[0]:
            # Leave two items for the last album instead of a lone eleventh item.
            count = 9 if len(group) == 11 else min(10, len(group))
            yield [group.popleft() for _ in range(count)]


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
    # Probe the original in its own container. Unknown video attributes use document delivery.
    dimensions={f['id']:video.attributes(c.safe_file(f['path'])) for f in files
                if f['state']=='pending' and not f['force_document'] and Path(f['path']).suffix.lower() in video.VIDEO_DEMUXERS and f['size']<=limits.telegram_file_limit()}
    for f in files:
        if f['supports_streaming'] and dimensions.get(f['id']):
            dimensions[f['id']]['supports_streaming'] = True

    def delivery_kind(f):
        if not f['force_document']:
            if dimensions.get(f['id']):
                return 'video'
            if Path(f['path']).suffix.lower() in ('.jpg', '.jpeg', '.png') and f['size'] < 9 * 1024**2:
                return 'photo'
        return 'document'

    def album_kind(f):
        if f['size'] > limits.telegram_file_limit():
            return None
        if delivery_kind(f) == 'document':
            return None if f['supports_streaming'] else 'document'
        return 'visual'

    def album(batch):
        _mark_file_states(batch,'uncertain')
        try:
            with contextlib.ExitStack() as stack:
                attachments={};media=[]
                for n,f in enumerate(batch):
                    name='file'+str(n);path=c.safe_file(f['path']);stream=stack.enter_context(path.open('rb'))
                    mime_type=mimetypes.guess_type(f['name'])[0] or 'application/octet-stream'
                    item={'type':delivery_kind(f),'media':'attach://'+name}
                    if item['type']=='video':mime_type=video.VIDEO_MIME_TYPES[path.suffix.lower()]
                    if item['type'] == 'document':
                        item['disable_content_type_detection'] = True
                        mime_type = 'application/octet-stream'
                    attachments[name]=(f['name'],stream,mime_type)
                    if item['type']=='video':item.update(dimensions[f['id']])
                    media.append(item)
                results=telegram('sendMediaGroup',{**_job_reply(job),'media':json.dumps(media)},attachments)
            if not isinstance(results,list) or len(results)!=len(batch) or any(not isinstance(r,dict) or not r.get('message_id') for r in results):raise ValueError('Incomplete album response')
            _save_file_receipts([(f['id'],result) for f,result in zip(batch,results)])
            return True
        except TelegramError as error:
            if error.code>=500 or error.code<400:
                _mark_delivery_uncertain(job)
                raise
            if error.code in (400, 413):
                _mark_file_states(batch, 'pending')
                return False
            _save_file_rejection(job,batch,error)
            raise error
        except Exception:
            for f in batch:f['state']='uncertain'
            _mark_delivery_uncertain(job);raise
    deliveries = deque(_delivery_batches(files, album_kind))
    while deliveries:
        if cancelled(job):return
        batch = deliveries.popleft()
        if len(batch) > 1:
            try:
                if not album(batch):
                    # Telegram definitively rejected this attempt. Keep smaller
                    # albums where possible, isolating a lone large upload first.
                    large = [f for f in batch if f['size'] >= limits.LARGE_UPLOAD_THRESHOLD]
                    if len(large) == 1:
                        smaller = [f for f in batch if f is not large[0]]
                        parts = (large, smaller) if batch[0] is large[0] else (smaller, large)
                    else:
                        middle = (len(batch) + 1) // 2
                        parts = (batch[:middle], batch[middle:])
                    deliveries.extendleft(reversed(parts))
            except Exception:
                status(job,'媒體分組傳送尚未完成，檔案保留待確認／重試');return
            continue
        f = batch[0]
        path=c.safe_file(f['path'])
        _mark_file_states([f],'uncertain')
        try:
            if f['size']>limits.telegram_file_limit():
                reason='檔案大於 500 MB，改提供下載連結。' if f['size']>limits.MAX_TELEGRAM_BYTES else '目前雲端傳送介面限制為 49 MiB，改提供下載連結。'
                if not links.enabled():
                    _mark_file_states([f],'too_large')
                    f['state']='too_large'
                    c.setting_many({'partial:'+job['id']:True,'partial_reason:'+job['id']:
                                    reason.replace('改提供下載連結。','')+'尚未設定下載網址，檔案保留在本機，未傳送。'})
                    continue
                result=telegram('sendMessage',{**_job_reply(job),'text':f['name']+'\n'+reason+'\n檔案及連結保留 2 小時：'+links.create(f['id'])})
            else:
                kind=delivery_kind(f)
                if f['supports_streaming'] and kind!='video':
                    _mark_file_states([f],'preview_failed')
                    f['state']='preview_failed'
                    continue
                data=_job_reply(job)
                mime_type=mimetypes.guess_type(f['name'])[0] or 'application/octet-stream'
                if kind=='video':
                    data.update(dimensions[f['id']])
                    mime_type=video.VIDEO_MIME_TYPES[path.suffix.lower()]
                if kind=='document' and (f['force_document'] or path.suffix.lower() in video.VIDEO_DEMUXERS):
                    data['disable_content_type_detection']=True
                    mime_type='application/octet-stream'
                with path.open('rb') as stream:
                    try:result=telegram('send'+kind.title(),data,{kind:(f['name'],stream,mime_type)})
                    except TelegramError as e:
                        if e.code!=400 or kind=='document':raise
                        if f['supports_streaming']:
                            _mark_file_states([f],'preview_failed')
                            f['state']='preview_failed'
                            continue
                        if cancelled(job):
                            _mark_file_states([f],'pending')
                            c.update(job['id'],state='cancelled',cancel=1)
                            return
                        stream.seek(0)
                        document_data=_job_reply(job)
                        document_data['disable_content_type_detection']=True
                        result=telegram('sendDocument',document_data,{'document':(f['name'],stream,'application/octet-stream')})
            if not isinstance(result,dict) or not result.get('message_id'):raise ValueError('Missing Telegram receipt')
            _save_file_receipts([(f['id'],result)])
        except TelegramError as e:
            if e.code>=500 or e.code<400:
                _mark_delivery_uncertain(job);return
            _save_file_rejection(job,[f],e)
            status(job,'Telegram 拒絕傳送，檔案保留待重試');return
        except Exception:
            _mark_delivery_uncertain(job);status(job,'傳送結果未明，檔案保留；請管理員確認後重試');return
    if cancelled(job):return
    preview_failed=any(f['state']=='preview_failed' for f in files)
    partial=preview_failed or c.get_setting('partial:'+job['id'],False)
    if partial:
        message='部分下載成功，已傳送可用內容。' if files else '沒有可傳送的媒體。'
        message+=c.get_setting('partial_reason:'+job['id'],'')
        if preview_failed:message+='播放版無法以影片傳送。'
    else:
        message=f'完成，共 {len(files)} 個檔案' if files else '沒有目前可見的限時動態／媒體'
    from . import preferences
    transition(job,message+preferences.notice_text(job['id']),state='partial' if partial else 'completed',delivered=time.time())

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
                check=db.execute("SELECT requested,state FROM admin_pool_checks WHERE platform=? AND state IN ('queued','running')",(platform,)).fetchone()
            if check and check['state']=='running':
                pool_checks.recover(platform)
                continue
            if check and (not row or (row['state']=='queued' and row['created']>check['requested'])
                          or not c.get_setting('enabled',False)):
                pool_checks.run_next(platform)
                continue
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
                from . import preferences
                mode,notice=preferences.route(provider,mode,url,json.loads(job.get('preferences') or '{}').get(provider,{}))
                if job.get('group_chat_id') and mode=='choice':mode='post'
                if '/share/' in urlsplit(job['url']).path and provider=='instagram':
                    notice=notice or '已將 Instagram 分享連結識別為'+('限時動態' if mode=='stories' else '貼文分享連結')+'，按可確認內容下載。'
                preferences.record_notice(job['id'],notice)
                c.update(job['id'],url=url,platform=provider,mode=mode,bridge_id=None,state='choice' if mode=='choice' else 'queued')
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
                        transition(job,str(error) if isinstance(error,ValueError) else '下載服務暫時失敗，請管理員重試',state='failed')
                except Exception as failure:_report_worker_error('Download failure checkpoint',failure)
                _report_worker_error('Download worker',error)
            stop.wait(2 if c.is_busy(error) else 5)


def _ensure_workers(workers):
    from . import admin_notifications
    specs=[('telegram-poll',poll,(STOP,))]
    specs += [('telegram-translation',_translation_loop,())]
    specs += [('admin-notifications',admin_notifications.run,(telegram,STOP))]
    specs += [('telegram-inbox-'+str(i),inbox.loop,(partial(handle_update,from_inbox=True),STOP)) for i in range(4)]
    specs += [('download-'+p,platform_loop,(p,STOP)) for p in accounts.DOWNLOAD_PLATFORMS]
    for name,target,args in specs:
        previous=workers.get(name)
        if previous is not None and previous.is_alive():continue
        if previous is not None:logging.getLogger(__name__).warning('%s thread stopped; restarting',name)
        thread=threading.Thread(target=target,args=args,daemon=True,name=name)
        thread.start();workers[name]=thread


def maintenance(workers):
    _ensure_workers(workers)
    for name,action in (('media cleanup',c.cleanup),('inbox cleanup',inbox.cleanup),
                        ('pool test cleanup',pool_checks.cleanup_pending)):
        try:action()
        except Exception as error:
            logging.getLogger(__name__).warning('%s failed (%s); retry next pass',name,type(error).__name__)


def main():
    storage.check(download=True)
    STOP.clear()
    c.initialize()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous[sig] = signal.signal(sig,lambda *_: STOP.set())
    try:
        with c.db_keeper(),links.serving():_run_main()
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)


def _recover_playback_scratch():
    # Recover killed encodes before disk_ok() can block the next capture. Only
    # generated job and batch directories can own encoder staging files.
    for job_dir in (c.DATA/'media').iterdir():
        if (not re.fullmatch(r'[0-9a-f]{16}', job_dir.name)
                or job_dir.is_symlink() or not job_dir.is_dir()):
            continue
        video.cleanup_playback_scratch(job_dir)
        for item_dir in job_dir.glob('item-*'):
            if (re.fullmatch(r'item-[0-9]+', item_dir.name)
                    and not item_dir.is_symlink() and item_dir.is_dir()):
                video.cleanup_playback_scratch(item_dir)


def _run_main():
    _recover_playback_scratch()
    pool_checks.recover()
    # Completed local files survive restart; uncertain Telegram sends need review.
    with c.db() as db:
        db.execute("UPDATE jobs SET state='partial',message='上次傳送結果未明，請管理員確認' WHERE id IN (SELECT job FROM files WHERE state='uncertain')")
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
