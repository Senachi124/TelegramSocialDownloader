"""Manage bot access and protect explicit administrator decisions."""
import time
from . import core as c

def allowed(user_id,db=None):
    if not isinstance(user_id,int) or isinstance(user_id,bool) or user_id<=0:return False
    if db is None:
        with c.db() as db:return allowed(user_id,db)
    row=db.execute('SELECT state FROM bot_users WHERE id=?',(user_id,)).fetchone()
    return bool(row and row['state'] in ('owner','approved'))

def observe(user):
    """Approve new or pending applicants; preserve explicit administrator decisions."""
    user_id=user.get('id')
    if not isinstance(user_id,int) or isinstance(user_id,bool) or user_id<=0 or user.get('is_bot'):return None
    name=' '.join(str(user.get(k,'')) for k in ('first_name','last_name')).strip()[:160]
    username=str(user.get('username',''))[:64]
    now=time.time()
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        automatic = db.execute("SELECT value FROM settings WHERE key='auto_approve_users'").fetchone()
        initial = 'approved' if automatic and automatic[0] == 'true' else 'pending'
        db.execute("""
          INSERT INTO bot_users(id,name,username,state,requested,last_seen) VALUES (?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET name=excluded.name,username=excluded.username,
            last_seen=excluded.last_seen,state=CASE WHEN bot_users.state='pending' THEN excluded.state ELSE bot_users.state END
        """,(user_id,name,username,initial,now,now))
        row=dict(db.execute('SELECT * FROM bot_users WHERE id=?',(user_id,)).fetchone())
        row['notify']=row['notice_at']<=now-60
        if row['notify'] and row['state'] not in ('owner','approved'):
            db.execute('UPDATE bot_users SET notice_at=? WHERE id=?',(now,user_id))
    return row

def decide(user_id,action):
    states={'approve':'approved','reject':'rejected','revoke':'revoked'}
    if action not in states:raise ValueError('無效的用戶操作')
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT state FROM bot_users WHERE id=?',(user_id,)).fetchone()
        if not row:raise ValueError('找不到用戶；請對方先私訊 Bot /start')
        if row['state']=='owner':raise ValueError('本人帳號受保護，不能從朋友名單撤銷')
        db.execute('UPDATE bot_users SET state=?,notice_at=0 WHERE id=?',(states[action],user_id))
        if action!='approve':
            db.execute("UPDATE jobs SET cancel=1,state='cancelled',message='使用權限已撤銷；檔案保留' WHERE chat=? AND state IN ('choice','queued','running','cooldown','sending','paused')",(user_id,))
            db.execute('DELETE FROM links WHERE file IN (SELECT f.id FROM files f JOIN jobs j ON j.id=f.job WHERE j.chat=?)',(user_id,))

def set_owner(user_id,name=''):
    """Deployment-only owner assignment, never derived from first arrival."""
    if not isinstance(user_id,int) or isinstance(user_id,bool) or user_id<=0:raise ValueError('Invalid owner ID')
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        owner=db.execute("SELECT id FROM bot_users WHERE state='owner'").fetchone()
        if owner and owner['id']!=user_id:raise ValueError('Owner already configured')
        now=time.time()
        db.execute("INSERT INTO bot_users(id,name,state,requested,last_seen) VALUES (?,?,'owner',?,?) ON CONFLICT(id) DO UPDATE SET state='owner'",(user_id,name,now,now))
