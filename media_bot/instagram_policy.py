"""Compatibility import for :mod:`media_bot.instagram.policy`."""
import sys

from .instagram import policy

sys.modules[__name__] = policy
