"""Compatibility import for :mod:`media_bot.translations.image_cache`."""
import sys

from .translations import image_cache

sys.modules[__name__] = image_cache
