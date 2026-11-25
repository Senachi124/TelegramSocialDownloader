"""Offline checks for optional provider storage provisioning."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'
spec = importlib.util.spec_from_file_location('takaneko_install', DEPLOY / 'install_takaneko.py')
install = importlib.util.module_from_spec(spec)
with patch.object(sys, 'path', [str(DEPLOY), *sys.path]):
    spec.loader.exec_module(install)


class InstallerStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.mount = Path(temporary.name)
        self.root = dict(target='/', uuid='root-volume', fsroot='/', **{'maj:min': '8:1'})
        self.external = dict(target=str(self.mount), uuid='media-volume', fsroot='/downloads',
                             **{'maj:min': '8:2'})

    def identity(self, records):
        results = [json.dumps({'filesystems': [record]}) for record in records]
        with patch.object(install.upgrade, 'trusted'), patch.object(
                install.subprocess, 'check_output', side_effect=results), patch.object(
                install.shutil, 'disk_usage') as disk_usage:
            disk_usage.return_value.free = 6 * 1024**3
            return install.storage_identity(self.mount)

    def test_missing_mount_never_provisions_underlying_directory(self):
        with self.assertRaisesRegex(ValueError, 'active mount point'):
            self.identity([self.root])

    def test_bind_mount_of_root_filesystem_is_rejected(self):
        bound_root = dict(self.root, target=str(self.mount), fsroot='/system/downloads')
        with self.assertRaisesRegex(ValueError, 'separate from /'):
            self.identity([bound_root, self.root])

    def test_root_subvolume_uuid_is_rejected_even_with_different_device_id(self):
        root_subvolume = dict(self.external, uuid=self.root['uuid'])
        with self.assertRaisesRegex(ValueError, 'separate from /'):
            self.identity([root_subvolume, self.root])

    def test_external_volume_and_bound_directory_pin_correct_filesystem_root(self):
        for filesystem_root in ('/', '/downloads'):
            with self.subTest(filesystem_root=filesystem_root):
                volume = dict(self.external, fsroot=filesystem_root)
                self.assertEqual(self.identity([volume, self.root]), ('media-volume', Path(filesystem_root)))
