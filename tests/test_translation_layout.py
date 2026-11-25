"""Translation callers and the worker share command state and module-level hooks."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class TranslationLayoutTests(unittest.TestCase):
    def test_legacy_imports_share_commands_and_in_memory_album_index(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'MEDIA_BOT_DATA': data}):
            from media_bot import batch_ocr, translation, translation_albums, translation_cache
            from media_bot.translations import albums, commands, image_cache, ocr

            self.assertIs(translation, commands)
            self.assertIs(translation_albums, albums)
            self.assertIs(translation_cache, image_cache)
            self.assertIs(batch_ocr, ocr)
            with patch.object(translation, '_request_output', return_value='result') as request:
                self.assertEqual(commands.translate(['test-key'], 'example'), 'result')
                request.assert_called_once()

    def test_importing_ocr_schema_needs_no_bot_configuration(self):
        env = os.environ.copy()
        env.pop('MEDIA_BOT_DATA', None)
        subprocess.run([sys.executable, '-c',
                        'from media_bot import batch_ocr; from media_bot.translations import ocr; '
                        'assert batch_ocr is ocr'], check=True, env=env, cwd=Path(__file__).resolve().parents[1])

    def test_chunking_respects_utf16_limit_and_emoji_boundaries(self):
        from media_bot.translations.replies import reply_chunks
        text = 'x' * 4094 + '👩‍🔬' + ' end'
        self.assertEqual(list(reply_chunks(text)), ['x' * 4094, '👩‍🔬 end'])
