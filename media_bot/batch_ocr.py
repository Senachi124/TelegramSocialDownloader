"""Compatibility import for :mod:`media_bot.translations.ocr`."""
import sys

from .translations import ocr

sys.modules[__name__] = ocr
