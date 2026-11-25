"""Compatibility import for :mod:`media_bot.translations.albums`."""
import sys

from .translations import albums

sys.modules[__name__] = albums
