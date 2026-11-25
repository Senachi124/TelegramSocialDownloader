"""Offline upgrade controller tests using a disposable release tree."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

MODULE = Path(__file__).resolve().parents[1]/'deploy/upgrade.py'
spec = importlib.util.spec_from_file_location('upgrade', MODULE)
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name)
        self.root=base/'releases'
        self.old=self.root/'telegram-1.0.0'
        self.old.mkdir(parents=True)
        self.current=base/'current'
        self.current.symlink_to(self.old)
        self.tag='v1.0.1'
        self.sha='1'*40
        (self.old/upgrade.MANIFEST).write_text(json.dumps({'version':'1.0.0','commit':'0'*40}))
        self.wheel=base/'telegram_social_downloader-1.0.1-py3-none-any.whl'
        with zipfile.ZipFile(self.wheel,'w') as f:
            f.writestr('telegram_social_downloader-1.0.1.dist-info/METADATA',
                       'Name: telegram-social-downloader\nVersion: 1.0.1\n')
        backup=base/'backups'
        backup.mkdir(mode=0o700)
        self.config=dict(current=str(self.current),releases=str(self.root),data=str(base),
            worker='worker.service',broker='broker.service',web='web.service',push='push.service',
            backups=str(backup),web_socket='/run/example/web.sock',health_path='/healthz')
        self.enabled='true'
        self.phase=[]
        self.backup=backup
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(upgrade,'trusted'))
        self.stack.enter_context(patch.object(upgrade,'prepare',side_effect=self.prepare))
        self.stack.enter_context(patch.object(upgrade,'database',side_effect=self.db))
        self.control=self.stack.enter_context(patch.object(upgrade,'systemctl'))
        self.health=self.stack.enter_context(patch.object(upgrade,'check'))
        self.stack.enter_context(patch.object(upgrade,'free_of_work',return_value=True))

    def prepare(self,old,root,wheel,tag,sha,digest):
        target=root/'candidate'
        target.mkdir(exist_ok=True)
        (target/upgrade.MANIFEST).write_text(json.dumps(dict(version=tag[1:],commit=sha)))
        return target

    def db(self,cfg,op,*args,output=None):
        if op=='enabled':return self.enabled
        if op=='set':
            self.enabled=args[0]
            self.phase.append(self.enabled)
        if op=='busy':return '0'
        if op=='snapshot':output.write(b'fixture')

    def run_upgrade(self):
        upgrade.upgrade(self.config,self.wheel,self.tag,self.sha)

    def test_install_pauses_admission_and_idempotent_retry_preserves_running_release(self):
        self.run_upgrade()
        self.assertEqual(self.current.resolve(), self.root/'candidate')
        self.assertEqual(self.phase,['false','true'])
        self.assertEqual(len(list(self.backup.glob('*.sqlite3'))),1)
        self.control.reset_mock()
        self.run_upgrade()
        self.control.assert_not_called()

    def test_health_failure_restores_previous_code_and_admission(self):
        self.health.side_effect=[None,RuntimeError('unhealthy candidate'),None]
        with self.assertRaisesRegex(RuntimeError,'unhealthy candidate'):
            self.run_upgrade()
        self.assertEqual(self.current.resolve(),self.old)
        self.assertEqual(self.enabled,'true')
        self.assertFalse((self.backup/'upgrade-in-progress.json').exists())
        self.assertEqual(next(self.backup.glob('*.sqlite3')).read_bytes(),b'fixture')

    def test_restore_retains_operator_disabled_setting(self):
        self.enabled='false'
        self.run_upgrade()
        self.assertEqual(self.enabled,'false')

    def test_wrong_tag_and_distribution_metadata_rejected(self):
        with self.assertRaises(ValueError):
            upgrade.wheel_info(self.wheel,'v1.0.2')
        with self.assertRaises(ValueError):
            upgrade.version('v1.0.1;true')
        with zipfile.ZipFile(self.wheel,'w') as f:
            f.writestr('telegram_social_downloader-1.0.1.dist-info/METADATA',
                       'Name: wrong\nVersion: 1.0.1\n')
        with self.assertRaises(ValueError):self.run_upgrade()
        self.control.assert_not_called()

    def test_snapshot_contains_committed_wal_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            filename=Path(temp)/'state.sqlite3'
            db=sqlite3.connect(filename)
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE pending(id INTEGER PRIMARY KEY)')
                db.execute('INSERT INTO pending VALUES (12)')
                db.commit()
            finally:
                db.close()
            snapshot=Path(temp)/'checkpoint.sqlite3'
            with snapshot.open('wb') as dst:
                subprocess.run([sys.executable,'-I','-c',upgrade.DB_PROGRAM,temp,'snapshot'],
                               stdout=dst,check=True)
            saved=sqlite3.connect(snapshot)
            try:
                self.assertEqual(saved.execute('SELECT * FROM pending').fetchone(),(12,))
            finally:
                saved.close()
