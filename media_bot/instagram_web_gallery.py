"""Compatibility import for :mod:`media_bot.instagram.web_gallery`."""
import sys

from .instagram import web_gallery

sys.modules[__name__] = web_gallery
