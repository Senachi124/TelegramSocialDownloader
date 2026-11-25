"""Compatibility import for :mod:`media_bot.instagram.web_client`."""
import sys

from .instagram import web_client

sys.modules[__name__] = web_client
