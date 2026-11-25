"""Previous platform import paths refer to the canonical module and its state."""
import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


class PlatformImports(unittest.TestCase):
    def test_legacy_import_paths(self):
        aliases = {
            'ig_cookie_refresh': 'instagram.cookie_refresh',
            'instagram_cookie_maintenance': 'instagram.maintenance',
            'instagram_policy': 'instagram.policy',
            'instagram_quality': 'instagram.quality',
            'instagram_story': 'instagram.story',
            'instagram_web_client': 'instagram.web_client',
            'instagram_web_gallery': 'instagram.web_gallery',
            'instagram_web_timeline': 'instagram.timeline',
            'threads_runner': 'threads.runner',
            'twitter_runner': 'twitter.runner',
        }
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'MEDIA_BOT_DATA': data}):
            for previous, canonical in aliases.items():
                with self.subTest(module=previous):
                    actual = importlib.import_module('media_bot.' + previous)
                    self.assertIs(actual, importlib.import_module('media_bot.' + canonical))
