"""Compatibility import for :mod:`media_bot.threads.runner`."""
import sys

from .threads import runner

if __name__ == "__main__":
    runner.main()
else:
    sys.modules[__name__] = runner
