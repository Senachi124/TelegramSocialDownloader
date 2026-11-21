"""Probe original video streams for Telegram and recover legacy encoder scratch."""
import fcntl
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess


def attributes(path):
    """Return Bot API video attributes, or {} to send an unprobed file as a document.

    Only use known local demuxers. Legacy callers can supply extensionless MP4
    files. Exclude cover art when selecting the first actual video stream.
    """
    demuxer = VIDEO_DEMUXERS.get(Path(path).suffix.lower(), 'mov')
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-f', demuxer,
            '-select_streams', 'V:0', '-show_entries',
            'stream=width,height,sample_aspect_ratio,duration:stream_tags=rotate:'
            'stream_side_data=rotation:format=duration', '-of', 'json',
            str(Path(path).resolve()),
        ], capture_output=True, text=True, timeout=10, check=True)
        data = json.loads(result.stdout)
        stream = data['streams'][0]
        return _display_attributes(stream, data.get('format', {}).get('duration'))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
        return {}


def _display_attributes(stream, format_duration):
    width, height = int(stream['width']), int(stream['height'])
    sar = stream.get('sample_aspect_ratio', '1:1')
    if sar not in ('N/A', '0:1'):
        ratio = Fraction(sar.replace(':', '/'))
        if ratio <= 0:return {}
        width = round(width * ratio)
    rotation = stream.get('tags', {}).get('rotate', 0)
    for side in stream.get('side_data_list', []):
        if 'rotation' in side:
            rotation = side['rotation'];break
    rotation = float(rotation) % 360
    # Arbitrary-angle transforms cannot be described by width/height alone.
    if not math.isfinite(rotation) or abs(rotation / 90 - round(rotation / 90)) > 0.001:
        return {}
    if round(rotation / 90) % 2:width, height = height, width
    if not (0 < width <= 32768 and 0 < height <= 32768):return {}
    out = {'width': width, 'height': height}
    duration = stream.get('duration')
    if duration in (None, 'N/A'):duration = format_duration
    if duration is not None:
        seconds = float(duration)
        if math.isfinite(seconds) and 0 <= seconds < 2**31:
            out['duration'] = math.ceil(seconds)
    return out


# Only self-contained video demuxers. Never let an untrusted file select a
# playlist, concat script or other demuxer that can open additional inputs.
VIDEO_DEMUXERS = {
    '.mp4': 'mov', '.m4v': 'mov', '.mov': 'mov', '.3gp': 'mov', '.3g2': 'mov',
    '.mkv': 'matroska', '.webm': 'matroska', '.avi': 'avi', '.flv': 'flv',
    '.ts': 'mpegts', '.m2ts': 'mpegts', '.mts': 'mpegts', '.mpg': 'mpeg',
    '.mpeg': 'mpeg', '.wmv': 'asf', '.asf': 'asf', '.ogv': 'ogg',
}


# Some systems map .ts to TypeScript and .mts to model/vnd.mts.
VIDEO_MIME_TYPES = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
    '.3gp': 'video/3gpp', '.3g2': 'video/3gpp2', '.mkv': 'video/x-matroska',
    '.webm': 'video/webm', '.avi': 'video/x-msvideo', '.flv': 'video/x-flv',
    '.ts': 'video/mp2t', '.m2ts': 'video/mp2t', '.mts': 'video/mp2t',
    '.mpg': 'video/mpeg', '.mpeg': 'video/mpeg', '.wmv': 'video/x-ms-wmv',
    '.asf': 'video/x-ms-asf', '.ogv': 'video/ogg',
}


def is_playback_derivative(path):
    return Path(path).name.lower().endswith(('.h265.mp4', '.encoding.mp4'))


def is_playback_scratch(path):
    return bool(re.fullmatch(r'\.playback-[0-9a-f]{32}\.encoding\.mp4', Path(path).name))


def cleanup_playback_scratch(root):
    """Recover only our abandoned named encoder files on fallback filesystems."""
    for path in Path(root).glob('.playback-*.encoding.mp4'):
        if not is_playback_scratch(path):
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            opened = os.fstat(fd)
            if stat.S_ISREG(opened.st_mode) and os.path.samestat(opened, path.lstat()):
                path.unlink()
        except (BlockingIOError, FileNotFoundError):
            pass
        finally:
            os.close(fd)
