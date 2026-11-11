"""Shared per-media download ceiling; never interpret cache/merge copies as media."""
from . import limits


class DownloadTooLarge(BaseException):
    """Bypass downloader retry/fallback handlers and stop the current capture."""


class DownloadStopped(BaseException):
    """A classified provider stop that must not trigger an extractor retry."""


def check(size):
    if isinstance(size,(int,float)) and size > limits.MAX_DOWNLOAD_BYTES:
        raise DownloadTooLarge()


def declared(headers, *, offset=0):
    """Content-Range includes total media size; Content-Length may only be a tail."""
    content_range=headers.get('Content-Range',headers.get('content-range','')) or ''
    total=content_range.rpartition('/')[2]
    if total.isdecimal():
        check(int(total))
        return int(total)
    value=headers.get('Content-Length',headers.get('content-length','')) or ''
    if str(value).isdecimal():
        total=int(value)+offset
        check(total)
        return total


def mark(result):
    result.update(code=1,failure='too_large',oversize=True)
