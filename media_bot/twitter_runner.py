"""Compatibility import for :mod:`media_bot.twitter.runner`."""
import sys

from .twitter import runner

sys.modules[__name__] = runner
