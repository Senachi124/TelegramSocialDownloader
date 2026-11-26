"""Run the enabled platform session checks independently and report only status counts."""
import json

from . import youtube_maintenance
from .instagram import maintenance as instagram_maintenance


def main():
    result = {}
    for platform, run in (('instagram', instagram_maintenance.run), ('youtube', youtube_maintenance.run)):
        try:
            result[platform] = run()
        except Exception:
            result[platform] = {'maintenance_error': 1}
    print(json.dumps(result, sort_keys=True))
    handled = {'updated', 'unchanged', 'superseded', 'rate_limited', 'deferred', 'login_required'}
    if any(set(counts) - handled for counts in result.values()):
        raise SystemExit(1)
