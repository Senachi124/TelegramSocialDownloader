"""Compatibility import for :mod:`media_bot.instagram.cookie_refresh`."""
import sys

from .instagram import cookie_refresh

sys.modules[__name__] = cookie_refresh
