"""Compatibility import for :mod:`media_bot.instagram.quality`."""
import sys

from .instagram import quality

sys.modules[__name__] = quality
