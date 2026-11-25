"""Exercise upgrades and lifecycle boundaries with synthetic jobs only."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from test_runtime import _TEST_ROOT
from media_bot import bot_ui, core as c, inbox, links, retention, translation_albums


class RetentionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=_TEST_ROOT)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for substitute in (patch.object(c, 'DATA', self.root),
                           patch.dict(os.environ, {'MEDIA_BOT_DATA': folder.name,
                                                   'MEDIA_BOT_ALLOWED_USERS': '123'})):
            substitute.start()
            self.addCleanup(substitute.stop)
        c.initialize()

    def job(self, key, state):
        job_id = c.enqueue(key, 123, 'https://x.com/example/status/123')
        c.update(job_id, state=state)
        path = self.root / 'media' / job_id / 'fixture.bin'
        path.parent.mkdir()
        path.write_bytes(b'fixture')
        return job_id, path

    def add_file(self, job_id, path, state):
        with c.db() as db:
            return db.execute('INSERT INTO files(job,path,name,size,state) VALUES (?,?,?,?,?)',
                              (job_id,str(path),path.name,path.stat().st_size,state)).lastrowid

    def test_upgrade_preserves_queues_and_leases_and_erases_old_records(self):
        done, done_path = self.job('complete', 'completed')
        self.add_file(done, done_path, 'sent')
        c.setting('preference_notice:' + done, ['synthetic source detail'])
        manifest = done_path.with_name('result.json')
        manifest.write_text(json.dumps({'files':[str(done_path)]}))
        active, active_path = self.job('active', 'queued')
        self.add_file(active, active_path, 'pending')
        partial, partial_path = self.job('ambiguous', 'partial')
        self.add_file(partial, partial_path, 'uncertain')
        trace = self.root / 'tmp/llm-trace/traces.sqlite3'
        trace.parent.mkdir(parents=True)
        with sqlite3.connect(trace) as db:
            db.execute('CREATE TABLE traces (original TEXT)')
            db.execute("INSERT INTO traces VALUES ('synthetic text')")
        with c.db() as db:
            translation_albums.initialize(db)  # Simulate the earlier on-disk schema.
            db.execute("INSERT INTO telegram_inbox VALUES (9,123,'synthetic payload',1,'done',1,1)")
            db.execute("INSERT INTO telegram_inbox VALUES (10,123,'active payload',1,'pending',NULL,NULL)")
        c.initialize()
        c.initialize()
        self.assertFalse(trace.exists())
        self.assertFalse(manifest.exists())
        self.assertIsNone(c.job_by_id(done))
        self.assertIsNone(c.get_setting('preference_notice:' + done))
        self.assertEqual(c.job_by_id(active)['state'],'queued')
        self.assertEqual(c.job_by_id(partial)['state'],'partial')
        self.assertTrue(all(p.is_file() for p in (done_path,active_path,partial_path)))
        with c.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM media_expiry').fetchone()[0],1)
            self.assertEqual(list(map(tuple, db.execute('SELECT id,payload FROM telegram_inbox'))),
                             [(10,'active payload')])
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='translation_album_members'").fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM files WHERE job=?',(done,)).fetchone()[0],0)

    def test_large_file_survives_until_its_capability_expires(self):
        job, path = self.job('link', 'sending')
        file_id = self.add_file(job, path, 'sent')
        with patch.object(c, 'ORIGIN', 'https://example.test'):
            url = links.create(file_id)
        c.update(job, state='completed', delivered=time.time())
        retention.sweep()
        with c.db() as db:
            current = db.execute('SELECT f.path,j.chat,j.url,f.telegram_id FROM files f JOIN links l ON f.id=l.file '
                                 'JOIN jobs j ON f.job=j.id WHERE l.digest=?',
                                 (c.digest(url.rsplit('/',1)[1]),)).fetchone()
            self.assertEqual(tuple(current), (str(path),123,'',None))
            db.execute('UPDATE links SET expires=1')
            db.execute('UPDATE files SET link_retention_until=1')
        retention.sweep()
        self.assertFalse(path.exists())
        self.assertIsNone(c.job_by_id(job))

    def test_queue_failures_deliver_an_actionable_reason_without_the_source(self):
        job, path = self.job('failure', 'running')
        c.update(job, state='failed', message='Cookie unavailable for https://example.test/private')
        retention.sweep()
        self.assertIsNone(c.job_by_id(job))
        self.assertTrue(path.is_file())
        with c.db() as db:
            reason, platform = db.execute('SELECT reason,platform FROM admin_alert_queue WHERE platform=?',
                                          ('twitter',)).fetchone()
            self.assertNotIn('example',reason)
            self.assertNotIn('private',reason)
            self.assertNotEqual(reason,'failed')
            self.assertEqual(platform,'twitter')

    def test_unknown_batch_send_keeps_checkpoint_without_source(self):
        url = 'https://x.com/fixture/status/246012345'
        job = c.enqueue_batch('uncertain-source',123,[url],7)
        with c.db() as db:
            db.execute("UPDATE batch_items SET state='failed',error_notified=1,error='fixture-error' WHERE job=?", (job,))
        c.update(job,state='partial')
        c.initialize()
        with c.db() as db:
            item = db.execute('SELECT original_url,url,error,error_notified FROM batch_items WHERE job=?',(job,)).fetchone()
            self.assertEqual(tuple(item),('','','',1))
        self.assertEqual(c.job_by_id(job)['url'],'')
        for path in self.root.glob('state.sqlite3*'):
            self.assertNotIn(url.encode(),path.read_bytes())

    def test_legacy_terminal_and_paused_states(self):
        rows = dict((state,self.job(state,state)) for state in ('delivered','expired','cancelled','paused'))
        for state,(job,path) in rows.items():
            self.add_file(job,path,'expired' if state=='expired' else 'pending' if state=='paused' else 'sent')
        c.initialize()
        for state,(job,path) in rows.items():
            if state=='paused':
                self.assertEqual(c.job_by_id(job)['state'],'paused')
                self.assertTrue(c.job_by_id(job)['url'])
            else:
                self.assertIsNone(c.job_by_id(job))
            self.assertTrue(path.exists())

    def test_old_buttons_cannot_return_private_downloads(self):
        with patch.object(bot_ui, 'is_owner', return_value=True):
            for action in ('job:abc:0','preview:1','sources:abc:0','downloads:all:0'):
                with self.subTest(action=action), self.assertRaises(ValueError):
                    bot_ui.route(lambda *unused: self.fail('Unexpected reply'),123,action)

    def test_auto_approval_keeps_first_download_and_discards_unrelated_content(self):
        c.setting('auto_approve_users',True)
        url = 'https://x.com/example/status/123'
        update = {'update_id':33,'message':{'message_id':7,'chat':{'id':456,'type':'private'},
                  'from':{'id':456},'text':'unrelated caption ' + url,
                  'reply_to_message':{'text':'a quoted message'},'photo':[{'file_id':'secret file'}]}}
        inbox.store([update])
        queued = inbox.claim()['payload']
        self.assertIn(url,queued)
        for phrase in ('unrelated caption','a quoted message','secret file'):
            self.assertNotIn(phrase,queued)

    def test_completed_inbox_row_is_immediately_released_and_never_replayed(self):
        update = {'update_id': 42, 'message':{'chat':{'id':123,'type':'private'},'from':{'id':123},'text':'/help'}}
        inbox.store([update])
        self.assertEqual(inbox.claim()['id'],42)
        inbox.complete(42,'done')
        inbox.store([update])
        self.assertIsNone(inbox.claim())
        self.assertEqual(c.get_setting('telegram_offset'),43)
