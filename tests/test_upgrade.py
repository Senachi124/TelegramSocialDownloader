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
        filename = Path(self.temp.name)/'config.json'
        filename.write_text(json.dumps(self.config))
        upgrade.upgrade(filename,self.wheel,self.tag,self.sha)

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

    def register_provider(self):
        lock = Path(self.temp.name) / 'provider.lock'
        lock.touch()
        self.config.update(takaneko_worker='provider.service', takaneko_broker='secrets.service',
                           takaneko_links='links.service', takaneko_data=str(Path(self.temp.name)/'provider'),
                           takaneko_deploy_lock=str(lock))
        return lock

    def test_provider_is_checkpointed_and_restarted_with_same_release(self):
        self.register_provider()
        self.run_upgrade()
        self.assertEqual(len(list(self.backup.glob('*.sqlite3'))), 2)
        self.assertEqual(next(self.backup.glob('*-takaneko.sqlite3')).read_bytes(), b'fixture')
        self.control.assert_any_call('start', 'provider.service')
        self.control.assert_any_call('stop', *reversed(upgrade.units(self.config)))

    def test_active_provider_send_aborts_before_pausing_bot(self):
        lock = self.register_provider()
        with lock.open('rb') as running, patch.object(upgrade.time, 'monotonic', side_effect=[0, 200]):
            upgrade.fcntl.flock(running, upgrade.fcntl.LOCK_SH)
            with self.assertRaisesRegex(RuntimeError, 'Takaneko operations still active'):
                self.run_upgrade()
        self.control.assert_not_called()
        self.assertFalse(self.phase)
        self.assertFalse((self.backup/'upgrade-in-progress.json').exists())

    def test_locked_config_reads_registration_under_the_same_lock(self):
        filename = Path(self.temp.name)/'config.json'
        filename.write_text(json.dumps(self.config))
        original = upgrade.config
        def read_while_locked(path):
            with (self.backup/'upgrade.lock').open('a') as concurrent:
                with self.assertRaises(BlockingIOError):
                    upgrade.fcntl.flock(concurrent, upgrade.fcntl.LOCK_EX | upgrade.fcntl.LOCK_NB)
            return original(path)
        with patch.object(upgrade, 'config', side_effect=read_while_locked):
            with upgrade.locked_config(filename) as cfg:
                self.assertEqual(cfg, self.config)

    def test_updated_provider_requires_positive_control_and_links_responses(self):
        self.register_provider()
        self.config.update(takaneko_control_socket='/run/provider/control.sock',
                           takaneko_links_socket='/run/provider-links/web.sock',
                           takaneko_broker_socket='/run/broker/secret.sock')
        with patch.object(upgrade, 'provider_probe', return_value=True) as probe, patch.object(
                upgrade, 'property_of', return_value='root'), patch.object(upgrade, 'UnixHTTP') as http:
            reply = http.return_value.getresponse.return_value
            (self.old/upgrade.MANIFEST).write_text('{"version":"1.1.1"}')
            reply.status = 404
            self.assertFalse(upgrade.provider_ready(self.config))
            probe.assert_called_once_with(self.config, self.config['takaneko_control_socket'],
                                         'provider.service', 'web.service', 'health')
            reply.status = 200
            reply.read.return_value = b'{"status":"ok"}'
            self.assertTrue(upgrade.provider_ready(self.config))
            probe.return_value = False
            self.assertFalse(upgrade.provider_ready(self.config))
            probe.return_value = True
            # Existing v1.1.0 providers return a guarded 503 on unknown link routes.
            (self.old/upgrade.MANIFEST).write_text('{"version":"1.1.0"}')
            reply.status = 503
            self.assertTrue(upgrade.provider_ready(self.config))
            probe.assert_any_call(self.config, self.config['takaneko_control_socket'],
                                  'provider.service', 'web.service', 'membership')
            probe.assert_any_call(self.config, self.config['takaneko_broker_socket'],
                                  'secrets.service', 'provider.service', 'legacy-broker')

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
