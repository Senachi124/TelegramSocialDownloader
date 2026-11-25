"""Polling and platform workers. Telegram delivery uncertainty is never auto-retried."""
from urllib.parse import urlsplit
import contextlib
from datetime import datetime
from functools import partial
import json
import logging
import os
from queue import Empty, Full, Queue
import re
import shutil
import signal
import subprocess
import threading
import time
from zoneinfo import ZoneInfo
from . import core as c
from . import access,tiktok,video,inbox,accounts,twitter,threads,pool_checks
from . import features,links,group_download,group_menu,limits,storage
from .translations import commands as translation
from .processing import capture as capture_process
from .processing.capture import CaptureStorageError, CaptureTimeout, disk_ok
from .processing.delivery import send_files
from .processing.telegram_api import TelegramError, _local_file_payload, _transport, telegram

STOP = threading.Event()
WAKE={platform:threading.Event() for platform in accounts.DOWNLOAD_PLATFORMS}
# Striped locks bound memory while preventing duplicate initial status messages.
_STATUS_LOCKS=[threading.RLock() for _ in range(64)]
_TRANSLATIONS=Queue(maxsize=8)
_TRANSLATION_BUSY=Queue(maxsize=1)


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
            reply='已加入佇列' if changed else '此工作已處理'
            if changed and not features.download_enabled(job['platform'], db):
                reply='已儲存選擇，等待 '+accounts.LABELS[job['platform']]+' 下載重新啟用。'
        with contextlib.suppress(Exception):telegram('answerCallbackQuery',{'callback_query_id':q['id'],'text':reply})
        if changed:
            status(job,reply);WAKE[job['platform']].set()
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
    admitted = features.enabled('translation')
    # Notices need the actor and command scope for authorization at dispatch.
    notice = {'chat': {'id': reply['chat_id'], 'type': message['chat']['type']},
              'from': {'id': message['from']['id']}, 'text': match[0] if match else ''}
    try:_TRANSLATIONS.put_nowait((message if admitted else notice,reply,admitted))
    except Full:
        # Report one burst from the translation consumer, outside the polling thread.
        with contextlib.suppress(Full):
            _TRANSLATION_BUSY.put_nowait((notice,reply,admitted))


def _translation_loop():
    while True:
        message,reply,admitted=_TRANSLATIONS.get()
        try:
            handled=translation.handle(message,telegram,lambda: _transport.token,reply,
                                       bot_username=bot_username,admitted=admitted)
            match=translation.command(message)
            if handled and match and match[1] and not translation.prompt_reply(message) and message['chat']['type'] in ('group','supergroup'):
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
            notice,busy_reply,admitted=_TRANSLATION_BUSY.get_nowait()
        except Empty:
            continue
        try:
            translation.handle(notice,telegram,lambda: _transport.token,busy_reply,
                               bot_username=bot_username,admitted=admitted,busy=True)
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

def _cancel_oversized(job,result):
    if not (result.get('oversize') or result.get('failure')=='too_large'
            or any(f.get('size',0)>limits.MAX_DOWNLOAD_BYTES for f in result.get('files',[]))):
        return False
    c.update(job['id'],state='cancelled',cancel=1,retry_at=0)
    status(job,'單一檔案超過 2000 MB，已立即取消下載；不會傳送，現有檔案保留待管理員處理。')
    return True


def capture(job, cookies, *, root=None, should_cancel=None, timeout=1800):
    if should_cancel is None:
        should_cancel = partial(cancelled, job)
    return capture_process.run(job, cookies, root=root, should_cancel=should_cancel,
                               stop=STOP, timeout=timeout)


def _select_account(job, platform):
    credential = c.rpc(c.SECRET_SOCKET, '/select', {'platform': platform, 'id': job.get('account_id', '')})
    if platform in ('threads', 'youtube') and credential.get('public') and not job.get('account_id'):
        return None, {}
    cookies = credential.get('cookies', {})
    if cookies:
        c.update(job['id'], account_id=credential['id'])
        job['account_id'] = credential['id']
    return credential, cookies


def _report_account(platform, credential, result):
    if credential is None or (result['retry_at'] and result.get('wait_reason') == 'quota'):
        return
    failure = result.get('failure')
    if result['retry_at']:
        outcome = 'rate_limited'
    elif result.get('auth_required') or failure == 'login_required':
        outcome = 'auth_required'
    elif failure == 'forbidden':
        outcome = 'access_error'
    elif result['code'] and failure != 'conversion_failed':
        outcome = 'failed'
    else:
        outcome = 'success'
    with contextlib.suppress(Exception):
        c.rpc(c.SECRET_SOCKET, '/report', {
            'platform': platform, 'id': credential['id'], 'revision': credential['revision'], 'outcome': outcome,
        })


def _start_download(job):
    # Serialize the start of an item with the admin switch. A pause after this
    # commit leaves this item in flight. A pause before it preserves its checkpoint.
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not features.download_enabled(job['platform'], db):
            return False
        return bool(db.execute("""UPDATE jobs SET state='running'
            WHERE id=? AND platform=? AND state IN ('queued','running','cooldown')
            AND cancel=0 AND retry_at<=?""", (job['id'], job['platform'], time.time())).rowcount)


def download(job):
    if cancelled(job) or not _start_download(job):return False
    platform=job['platform'];label=accounts.LABELS[platform]
    retry=c.get_setting(platform+'_cooldown',0)
    if retry>time.time():
        c.update(job['id'],state='cooldown',retry_at=retry);status(job,label+' 冷卻至 '+datetime.fromtimestamp(retry,ZoneInfo('Asia/Hong_Kong')).isoformat(timespec='minutes'));return False
    credential, cookies = _select_account(job, platform)
    if credential is not None and not cookies:
        transition(job,label+' 帳號池沒有可用 Cookie，請管理員更新或啟用已綁定帳號',state='paused');return False
    disk_ok();status(job,label+' 下載中（使用已保存 Cookie）' if platform=='tiktok' else label+' 下載中')
    result=capture(job,cookies)
    if result is None or cancelled(job):return False
    from . import preferences
    preferences.record_notice(job['id'],result.get('preference_notice'))
    if _cancel_oversized(job,result):return False
    _report_account(platform, credential, result)
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

def _resolve_download(url):
    try:
        url = c.resolve(url)
    except c.ResolutionDeferred as hop:
        return hop.url, hop.platform, 'resolve'
    platform, mode = c.classify(url)
    if mode == 'resolve':
        raise ValueError('不支援的分享連結')
    return url, platform, mode


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
    if not _start_download(job):
        return
    try:
        if item['mode']=='resolve':
            status(job,'正在解析分享連結')
            retry=c.get_setting(item['platform']+'_cooldown',0)
            if retry>time.time():raise c.RateLimited(retry)
            url,platform,mode=_resolve_download(item['url'])
            from . import preferences
            mode,notice=preferences.route(platform,mode,url,json.loads(job.get('preferences') or '{}').get(platform,{}))
            preferences.record_notice(job['id'],notice)
            if mode=='choice':mode='post'
            with c.db() as db:db.execute('UPDATE batch_items SET url=?,platform=?,mode=? WHERE job=? AND position=?',(url,platform,mode,job['id'],item['position']))
            _batch_next(job);return
        platform=item['platform'];label=accounts.LABELS[platform]
        retry=c.get_setting(platform+'_cooldown',0)
        if retry>time.time():raise c.RateLimited(retry)
        credential, cookies = _select_account(job, platform)
        if credential is not None and not cookies:
            if platform in ('threads','youtube'):
                transition(job,label+' 此工作帳號不可用，請更新或啟用已綁定帳號',state='paused');return
            raise ValueError(label+' 帳號池沒有可用 Cookie，請管理員更新')
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
        _report_account(platform, credential, result)
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
    if not send_files(job, files, _job_reply(job), send=telegram, status=status, cancelled=cancelled):
        return
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

def _next_job(db, platform, now):
    # A disabled capture must not block outstanding sends, batch finalization
    # or a handoff left between item completion and the next worker after a crash.
    eligible = ''
    if not features.download_enabled(platform, db):
        eligible = """AND (state='sending' OR (mode='batch' AND platform IS NOT (
            SELECT b.platform FROM batch_items b WHERE b.job=jobs.id AND b.state='pending'
            ORDER BY b.position LIMIT 1)))"""
    return db.execute("""SELECT * FROM jobs WHERE platform=? AND retry_at<=?
        AND state IN ('queued','running','cooldown','sending') """ + eligible +
        ' ORDER BY created LIMIT 1', (platform, now)).fetchone()


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
                row=_next_job(db, platform, time.time())
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
                process_batch(job);continue
            if job['state']=='sending':deliver(job);continue
            if job['mode']=='resolve':
                if not _start_download(job):continue
                status(job,'正在解析分享連結')
                if c.get_setting(platform+'_cooldown',0)>time.time():
                    retry=c.get_setting(platform+'_cooldown');c.update(job['id'],state='cooldown',retry_at=retry)
                    status(job,'平台冷卻中，稍後自動續作');continue
                url,provider,mode=_resolve_download(job['url'])
                from . import preferences
                mode,notice=preferences.route(provider,mode,url,json.loads(job.get('preferences') or '{}').get(provider,{}))
                if job.get('group_chat_id') and mode=='choice':mode='post'
                if mode!='resolve' and '/share/' in urlsplit(job['url']).path and provider=='instagram':
                    notice=notice or '已將 Instagram 分享連結識別為'+('限時動態' if mode=='stories' else '貼文分享連結')+'，按可確認內容下載。'
                preferences.record_notice(job['id'],notice)
                c.update(job['id'],url=url,platform=provider,mode=mode,bridge_id=None,state='choice' if mode=='choice' else 'queued')
                if mode=='choice':choice(c.job_by_id(job['id']))
                continue
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
