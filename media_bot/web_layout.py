"""Backward-compatible import for the web console views."""
import sys

from .admin import views

sys.modules[__name__] = views
