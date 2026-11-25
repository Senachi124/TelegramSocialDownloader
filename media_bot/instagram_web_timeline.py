"""Compatibility import for :mod:`media_bot.instagram.timeline`."""
import sys

from .instagram import timeline

sys.modules[__name__] = timeline
