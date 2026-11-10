import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_bot import config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.env = patch.dict(os.environ, {'MEDIA_BOT_DATA': str(self.data), 'MEDIA_BOT_ALLOWED_USERS': '123,456'}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_no_implicit_data_directory(self):
        os.environ.pop('MEDIA_BOT_DATA')
        with self.assertRaises(config.ConfigurationError):
            config.data_dir()

    def test_missing_and_relative_data_rejected(self):
        for value in ('relative', str(self.root / 'absent')):
            os.environ['MEDIA_BOT_DATA'] = value
            with self.assertRaises(config.ConfigurationError):
                config.data_dir()

    def test_allowlist_is_explicit_and_fail_closed(self):
        self.assertTrue(config.allowed(123))
        for value in (True, -123, '123', 789):
            self.assertFalse(config.allowed(value))
        for value in ('', '123,', '*', '123,other', '-1'):
            os.environ['MEDIA_BOT_ALLOWED_USERS'] = value
            self.assertFalse(config.allowed(123))

    def test_optional_cookie_file_is_not_read_from_any_default(self):
        for platform in config.PLATFORMS:
            self.assertEqual(config.cookies(platform), {})

    def test_cookie_file_validation_and_permissions(self):
        path = self.root / 'fixture.json'
        path.write_text(json.dumps({'sessionid': 'fixture', 'csrftoken': 'fixture'}))
        path.chmod(0o600)
        os.environ['INSTAGRAM_COOKIES_FILE'] = str(path)
        self.assertEqual(config.cookies('instagram')['sessionid'], 'fixture')
        path.chmod(0o644)
        with self.assertRaises(config.ConfigurationError):
            config.cookies('instagram')

    def test_secret_locations_and_symlinks_rejected(self):
        path = self.data / 'fixture.json'
        path.write_text('{}')
        path.chmod(0o600)
        os.environ['TIKTOK_COOKIES_FILE'] = str(path)
        with self.assertRaises(config.ConfigurationError):
            config.cookies('tiktok')
        link = self.root / 'link'
        link.symlink_to(path)
        os.environ['TIKTOK_COOKIES_FILE'] = str(link)
        with self.assertRaises(config.ConfigurationError):
            config.cookies('tiktok')

    def test_cookie_errors_do_not_expose_contents_or_path(self):
        path = self.root / 'sensitive-filename'
        path.write_text('private-fixture-not-json')
        path.chmod(0o600)
        os.environ['TWITTER_COOKIES_FILE'] = str(path)
        with self.assertRaises(config.ConfigurationError) as caught:
            config.cookies('twitter')
        self.assertNotIn(str(path), str(caught.exception))
        self.assertNotIn('private-fixture', str(caught.exception))

    def test_validate_sets_private_temp_directory(self):
        old_temp, old_mask = tempfile.tempdir, os.umask(0o077)
        try:
            with patch.object(config, 'token', return_value='fixture'):
                config.validate()
            self.assertEqual(Path(os.environ['TMPDIR']), self.data / 'tmp')
            self.assertEqual((self.data / 'tmp').stat().st_mode & 0o777, 0o700)
        finally:
            tempfile.tempdir = old_temp
            os.umask(old_mask)


if __name__ == '__main__':
    unittest.main()
