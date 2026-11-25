"""Compatibility import for :mod:`media_bot.instagram.story`."""
import sys

from .instagram import story

sys.modules[__name__] = story
