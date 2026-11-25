import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('preview', Path(__file__).resolve().parents[1] / 'deploy/preview.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class OwnerPreviewTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE release_preview_outbox(event_key TEXT PRIMARY KEY,payload TEXT,created REAL,
              state TEXT DEFAULT 'pending',message_id TEXT);
            CREATE TABLE bot_users(id INTEGER,state TEXT);
            INSERT INTO bot_users VALUES (123,'owner'), (456,'approved');
        """)
        self.core = SimpleNamespace(db=lambda: self.db)
        self.version, self.message = preview.render({'version':'1.0.1','changes':['管理員可部署新版。']})
        self.event = preview.event_id(self.version, self.message)

    def test_bundled_notice_matches_package_version(self):
        from importlib import resources
        from media_bot import __version__
        notice = resources.files('media_bot').joinpath('release_notice.json')
        version, _ = preview.render(json.loads(notice.read_text(encoding='utf-8')))
        self.assertEqual(version, __version__)

    def test_ambiguous_send_replay_preserves_checkpoint_and_approval(self):
        for expected in ('pending', 'uncertain'):
            preview.enqueue(self.core, self.version, self.message)
            self.assertEqual(preview.status(self.core, self.event), expected)
            self.db.execute("UPDATE release_preview_outbox SET state='uncertain'")
            self.db.commit()
        pending = json.loads(self.db.execute('SELECT value FROM settings').fetchone()[0])
        self.assertEqual((pending['owner_id'], pending['broadcast_authorized']), (123, False))
        self.assertEqual(pending['text'], 'v1.0.1 部署完成\n1. 管理員可部署新版。')
        self.assertEqual(self.db.execute('SELECT count(*) FROM release_preview_outbox').fetchone()[0], 1)

    def test_changed_content_requires_fresh_preview(self):
        for text in (self.message, self.message + '新增項目'):
            preview.enqueue(self.core, self.version, text)
        self.assertEqual(self.db.execute('SELECT count(*) FROM release_preview_outbox').fetchone()[0], 2)
        self.assertTrue(all(not json.loads(row[0])['broadcast_authorized']
                            for row in self.db.execute('SELECT value FROM settings')))

    def test_absent_owner_cannot_substitute_authorized_user(self):
        self.db.execute("DELETE FROM bot_users WHERE state='owner'")
        self.db.commit()
        with self.assertRaises(ValueError):
            preview.enqueue(self.core, self.version, self.message)
        self.assertEqual(preview.status(self.core, self.event), 'not_queued')
