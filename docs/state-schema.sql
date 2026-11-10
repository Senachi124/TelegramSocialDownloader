-- Reference DDL for state.sqlite3, derived from core.initialize().
-- Application startup creates these objects; this is not a migration script.
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, update_key TEXT UNIQUE, chat INTEGER,
  url TEXT, platform TEXT, mode TEXT, state TEXT, message TEXT DEFAULT '', status_id INTEGER,
  retry_at REAL DEFAULT 0, created REAL, delivered REAL, cancel INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,job TEXT REFERENCES jobs(id), path TEXT,
  name TEXT,size INTEGER,state TEXT DEFAULT 'pending', telegram_id TEXT, UNIQUE(job,path));
CREATE TABLE IF NOT EXISTS batch_requests(job TEXT PRIMARY KEY REFERENCES jobs(id),
  message_id INTEGER);
CREATE TABLE IF NOT EXISTS batch_items(job TEXT NOT NULL REFERENCES jobs(id), position INTEGER NOT NULL,
  original_url TEXT NOT NULL, url TEXT NOT NULL, platform TEXT NOT NULL, mode TEXT NOT NULL,
  state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', error_notified INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(job,position));
CREATE TABLE IF NOT EXISTS telegram_inbox(id INTEGER PRIMARY KEY,chat INTEGER NOT NULL,
  payload TEXT NOT NULL,received REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
  started REAL,finished REAL);
CREATE INDEX IF NOT EXISTS telegram_inbox_order ON telegram_inbox(chat,state,id);
CREATE TABLE IF NOT EXISTS media_budget(platform TEXT,item TEXT,time REAL,PRIMARY KEY(platform,item));
CREATE INDEX IF NOT EXISTS media_budget_time ON media_budget(platform,time);
