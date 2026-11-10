"""Read display dimensions locally before uploading a Telegram video."""
from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess


def attributes(path):
    """Return Bot API video attributes, or {} to send an unprobed file as a document.

    No re-encoding: account for non-square pixels and display-matrix rotation.
    Restrict ffprobe to local MP4 input; never follow media network references.
    """
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-f', 'mov',
            '-select_streams', 'v:0', '-show_entries',
            'stream=width,height,sample_aspect_ratio,duration:stream_tags=rotate:'
            'stream_side_data=rotation:format=duration', '-of', 'json',
            str(Path(path).resolve()),
        ], capture_output=True, text=True, timeout=10, check=True)
        data = json.loads(result.stdout)
        stream = data['streams'][0]
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
        if duration in (None, 'N/A'):duration = data.get('format', {}).get('duration')
        if duration is not None:
            seconds = float(duration)
            if math.isfinite(seconds) and 0 <= seconds < 2**31:
                out['duration'] = math.ceil(seconds)
        return out
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
        return {}
