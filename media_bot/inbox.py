"""Durable reception, with concurrent handling across chats and ordering within one chat."""
import json
import threading
import time
from . import core as c
from . import config

READY=threading.Event()
CAPACITY=1000


def capacity():
    with c.db() as db:
        return max(0,CAPACITY-db.execute("SELECT count(*) FROM telegram_inbox WHERE state IN ('pending','working')").fetchone()[0])


def store(updates):
    if not updates:return
    now=time.time()
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        for u in updates:
            callback=u.get('callback_query')
            message=callback.get('message',{}) if callback else u.get('message',{})
            chat=message.get('chat',{})
            actor=callback.get('from',{}) if callback else message.get('from',{})
            if chat.get('type')=='private' and chat.get('id')==actor.get('id') and not actor.get('is_bot') and config.allowed(chat.get('id')):
                db.execute('INSERT OR IGNORE INTO telegram_inbox(id,chat,payload,received) VALUES (?,?,?,?)',
                           (u['update_id'],chat['id'],json.dumps(u),now))
        old=db.execute("SELECT value FROM settings WHERE key='telegram_offset'").fetchone()
        offset=max(json.loads(old[0]) if old else 0,max(u['update_id'] for u in updates)+1)
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
        db.execute("UPDATE telegram_inbox SET state=?,finished=?,payload='' WHERE id=?",(state,finished,key))
    # The handler may already have sent a reply; retry persistence only.
    c.retry_write(save)
    READY.set()


def recover():
    # The command may already have sent a reply. Avoid an uncertain duplicate.
    with c.db() as db:
        db.execute("UPDATE telegram_inbox SET state='uncertain',finished=?,payload='' WHERE state='working'",(time.time(),))


def cleanup():
    with c.db() as db:
        db.execute("DELETE FROM telegram_inbox WHERE state NOT IN ('pending','working') AND finished<?",(time.time()-86400,))


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
