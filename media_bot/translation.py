"""Compatibility import for :mod:`media_bot.translations.commands`."""
import sys

from .translations import commands

sys.modules[__name__] = commands
