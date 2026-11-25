"""Compatibility import for :mod:`media_bot.instagram.maintenance`."""
import sys

from .instagram import maintenance

if __name__ == "__main__":
    maintenance.main()
else:
    sys.modules[__name__] = maintenance
