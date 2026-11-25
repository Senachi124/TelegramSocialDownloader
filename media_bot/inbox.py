"""Durable reception, with concurrent handling across chats and ordering within one chat."""
import json
import threading
import time
from . import access, core as c, group_download, translation, translation_albums

READY=threading.Event()
CAPACITY=1000


def capacity():
    with c.db() as db:
        return max(0,CAPACITY-db.execute("SELECT count(*) FROM telegram_inbox WHERE state IN ('pending','working')").fetchone()[0])


def _private_update(update_id, message, callback, actor, accepted):
    identity = {key:actor[key] for key in ('id','is_bot','first_name','last_name','username') if key in actor}
    contact = {'id':actor['id'],'type':'private'}
    if accepted and callback:
        return {'update_id':update_id,'callback_query':{'from':identity,'message':{'chat':contact},
                'id':callback['id'],'data':callback.get('data','')}}
    if accepted and group_download.command(message):
        return {'update_id':update_id,'message':group_download.inbox_message(message)}
    if accepted and message.get('media_group_id') and not (message.get('text') or message.get('caption')):
        return None
    text = '/start'
    if accepted:
        raw = message.get('text','').strip()
        if raw.startswith('/'):
            # Preserve the settings command and its two menu arguments.
            words = raw.split()
            text = ' '.join(words[:3]) if words[0].split('@')[0]=='/settings' else words[0]
        else:
            text = ' '.join(c.extract_message_urls(message))
    return {'update_id':update_id,'message':{'chat':contact,'from':identity,'text':text,
                                               'message_id':message.get('message_id')}}


def store(updates, bot_username=None):
    if not updates:return
    now=time.time()
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute("SELECT value FROM settings WHERE key='telegram_offset'").fetchone()
        previous_offset=json.loads(old[0]) if old else 0
        automatic = db.execute("SELECT value FROM settings WHERE key='auto_approve_users'").fetchone()
        allowed_states = ('owner','approved','pending') if automatic and automatic[0]=='true' else ('owner','approved')
        for u in updates:
            if u['update_id'] < previous_offset:continue
            incoming=u.get('message',{})
            if incoming.get('media_group_id') and u['update_id'] >= previous_offset:
                if not c.update_album_index(db, translation_albums.observe, incoming, now):
                    c.update_album_index(db, translation_albums.invalidate, incoming, now)
            if translation.command(incoming) or translation.prompt_reply(incoming):
                continue  # Advance the offset without storing translation content.
            callback=u.get('callback_query')
            message=callback.get('message',{}) if callback else u.get('message',{})
            chat=message.get('chat',{})
            actor=callback.get('from',{}) if callback else message.get('from',{})
            if chat.get('type')=='private' and chat.get('id')==actor.get('id') and not actor.get('is_bot'):
                previous=db.execute('SELECT state FROM bot_users WHERE id=?',(actor['id'],)).fetchone()
                accepted = (previous[0] if previous else 'pending') in allowed_states
                payload=_private_update(u['update_id'],message,callback,actor,accepted)
                if payload is None:continue
            elif (not callback and bot_username and group_download.target(message)==bot_username.lower()
                  and access.allowed(actor.get('id'),db)):
                payload={'update_id':u['update_id'],'message':group_download.inbox_message(message)}
            else:
                continue
            db.execute('INSERT OR IGNORE INTO telegram_inbox(id,chat,payload,received) VALUES (?,?,?,?)',
                       (u['update_id'],chat['id'],json.dumps(payload),now))
        offset=max(previous_offset,max(u['update_id'] for u in updates)+1)
        db.execute("INSERT OR REPLACE INTO settings VALUES ('telegram_offset',?)",(json.dumps(offset),))
    READY.set()


def claim():
    eligible="""SELECT i.* FROM telegram_inbox i WHERE i.state='pending'
      AND NOT EXISTS(SELECT 1 FROM telegram_inbox earlier WHERE earlier.chat=i.chat
        AND earlier.state IN ('pending','working') AND earlier.id<i.id)
      ORDER BY i.id LIMIT 1"""
    # Idle consumers need no writer lock. Reselect after acquiring it because
    # another consumer may claim the candidate between these two connections.
    with c.db() as db:
        if db.execute(eligible).fetchone() is None:return None
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute(eligible).fetchone()
        if not row:return None
        db.execute("UPDATE telegram_inbox SET state='working',started=? WHERE id=?",(time.time(),row['id']))
    return dict(row)


def complete(key,state):
    finished=time.time()
    def save(db):
        if state == 'done':
            db.execute('DELETE FROM telegram_inbox WHERE id=?',(key,))
            return
        db.execute("UPDATE telegram_inbox SET state=?,finished=?,chat=0,payload='' WHERE id=?",(state,finished,key))
    # The handler may already have sent a reply; retry persistence only.
    c.retry_write(save)
    READY.set()


def recover():
    # The command may already have sent a reply. Avoid an uncertain duplicate.
    with c.db() as db:
        db.execute("UPDATE telegram_inbox SET state='uncertain',finished=?,chat=0,payload='' WHERE state='working'",(time.time(),))


def cleanup():
    now = time.time()
    with c.db() as db:
        db.execute("DELETE FROM telegram_inbox WHERE state NOT IN ('pending','working') AND finished<?",(now-86400,))
        c.update_album_index(db, translation_albums.cleanup, now)


def run_one(handler):
    row=claim()
    if not row:return False
    try:handler(json.loads(row['payload']))
    except Exception:
        # Network timeouts are not proof that Telegram did not send the reply.
        # One broken command cannot stall reception or every other chat.
        complete(row['id'],'uncertain')
    else:complete(row['id'],'done')
    return True


def loop(handler,stop=None):
    stop=stop or threading.Event()
    while not stop.is_set():
        READY.clear()
        try:
            if c.get_setting('enabled',False) and run_one(handler):continue
        except Exception:pass
        READY.wait(0.5)
