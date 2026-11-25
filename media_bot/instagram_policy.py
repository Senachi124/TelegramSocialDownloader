"""Persistent pacing for all Instagram API requests, including worker restarts."""
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_RATE_POLICY = {'windowMinutes': 1, 'maxRequests': 12, 'minIntervalSeconds': 5}
RATE_BOUNDS = {'windowMinutes': (1, 60), 'maxRequests': (1, 600), 'minIntervalSeconds': (1, 3600)}
HISTORY_SECONDS = 3600.05


def validate_rate_policy(value=None):
    if value is None:
        return dict(DEFAULT_RATE_POLICY)
    if not isinstance(value, dict) or set(value) != set(RATE_BOUNDS):
        raise ValueError('請求速率需包含時段、次數上限及最短間隔')
    for key, (minimum, maximum) in RATE_BOUNDS.items():
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise ValueError(f'{key} 必須是 {minimum} 至 {maximum} 的整數')
    return dict(value)


def describe_rate_policy(policy):
    return f"每 {policy['windowMinutes']} 分鐘最多 {policy['maxRequests']} 次；間隔至少 {policy['minIntervalSeconds']} 秒"


class RequestWindow:
    def __init__(self, path, clock=time.time, sleep=time.sleep, policy=None):
        self.policy = validate_rate_policy(policy)
        self.window = self.policy['windowMinutes'] * 60 + 0.05
        self.limit = self.policy['maxRequests']
        self.interval = self.policy['minIntervalSeconds'] + 0.05
        self.description = describe_rate_policy(self.policy)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock, self.sleep = clock, sleep
        with self.connect() as db:
            legacy = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='requests'").fetchone() is not None
            db.execute("CREATE TABLE IF NOT EXISTS requests (sent_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), blocked_until REAL NOT NULL)")
            db.execute("INSERT OR IGNORE INTO state VALUES (1,0)")
            db.execute('CREATE TABLE IF NOT EXISTS history_coverage(id INTEGER PRIMARY KEY CHECK(id=1), since REAL NOT NULL)')
            # Old versions retained only ten minutes. A longer new policy must
            # wait for known history rather than interpreting missing rows as zero.
            db.execute('INSERT OR IGNORE INTO history_coverage VALUES(1,?)',
                       (self.clock() - (600.05 if legacy else HISTORY_SECONDS),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self):
        announced = False
        while True:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                stamp = self.clock()
                blocked = db.execute("SELECT blocked_until FROM state WHERE id=1").fetchone()[0]
                if blocked > stamp:
                    raise ValueError("429 冷卻尚未結束；本次未發送請求")
                db.execute("DELETE FROM requests WHERE sent_at<=?", (stamp - HISTORY_SECONDS,))
                times = [row[0] for row in db.execute("SELECT sent_at FROM requests ORDER BY sent_at")]
                recent = [sent for sent in times if sent > stamp - self.window]
                known_since = db.execute('SELECT since FROM history_coverage WHERE id=1').fetchone()[0]
                due = max(stamp, times[-1] + self.interval if times else stamp,
                          recent[-self.limit] + self.window if len(recent) >= self.limit else stamp,
                          known_since + max(self.window, self.interval))
                delay = due - stamp
                if delay <= 0:
                    db.execute("INSERT INTO requests VALUES (?)", (stamp,))
                    return
            if not announced:
                print(f"[instaloader][info] 限速等待約 {math.ceil(delay)} 秒（{self.description}；沿用歷史請求紀錄）", flush=True)
                announced = True
            self.sleep(min(delay, 30))

    def block(self, seconds=900):
        with self.connect() as db:
            db.execute("UPDATE state SET blocked_until=max(blocked_until,?) WHERE id=1", (self.clock() + max(900, seconds),))
