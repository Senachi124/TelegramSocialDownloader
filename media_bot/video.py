"""Probe Telegram video attributes and convert downloaded MKV to HEVC MP4."""
from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess
import uuid


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


def _conversion_probe(path, container):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-protocol_whitelist', 'file',
        '-f', container, '-show_streams', '-show_format', '-of', 'json', str(path),
    ], capture_output=True, text=True, timeout=15, check=True)
    data = json.loads(result.stdout)
    picture = next(stream for stream in data['streams'] if stream['codec_type'] == 'video')
    audio = next((stream for stream in data['streams'] if stream['codec_type'] == 'audio'), None)
    duration = float(data['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid media duration')
    return picture, audio, duration


def mkv_to_hevc(path):
    """Publish a verified HEVC MP4 atomically, retaining the downloaded MKV.

    The dedicated .h265.mp4 derivative is regenerated on retries. The worker's
    existing process-group cancellation and disk checks also cover FFmpeg.
    """
    from . import download_size

    source = Path(path)
    if source.suffix.lower() != '.mkv' or source.is_symlink() or not source.is_file():
        raise ValueError('Invalid MKV source')
    source = source.resolve()
    output = source.with_suffix('.h265.mp4')
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise ValueError('Invalid conversion output')
    temporary = source.with_name(source.stem + '.' + uuid.uuid4().hex + '.encoding.mp4')
    try:
        picture, audio, duration = _conversion_probe(source, 'matroska')
        download_size.check(source.stat().st_size)
        command = [
            'ffmpeg', '-v', 'error', '-nostdin', '-n',
            '-protocol_whitelist', 'file', '-f', 'matroska', '-i', str(source),
            '-map', '0:v:0', '-map', '0:a:0?', '-map_metadata', '0',
        ]
        if picture['codec_name'] == 'hevc':
            command += ['-c:v', 'copy']
        else:
            transfer = picture.get('color_transfer')
            deep = (transfer in ('smpte2084', 'arib-std-b67')
                    or any(bits in picture.get('pix_fmt', '') for bits in ('10', '12', '16')))
            command += [
                '-c:v', 'libx265', '-preset', 'fast', '-crf', '20',
                '-pix_fmt', 'yuv420p10le' if deep else 'yuv420p',
                '-x265-params', 'pools=2:frame-threads=2:log-level=error',
            ]
            for key, flag in (
                ('color_primaries', '-color_primaries'),
                ('color_transfer', '-color_trc'),
                ('color_space', '-colorspace'),
                ('color_range', '-color_range'),
            ):
                value = picture.get(key)
                if value and value not in ('unknown', 'unspecified'):
                    command += [flag, str(value)]
        command += ['-c:a', 'copy' if audio and audio['codec_name'] == 'aac' else 'aac']
        if audio and audio['codec_name'] != 'aac':
            command += ['-b:a', '192k']
        command += [
            '-tag:v', 'hvc1', '-movflags', '+faststart',
            '-fs', str(download_size.limits.MAX_DOWNLOAD_BYTES + 1), str(temporary),
        ]
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=1500, check=True)
        download_size.check(temporary.stat().st_size)
        converted, converted_audio, output_duration = _conversion_probe(temporary, 'mov')
        if (converted.get('codec_name') != 'hevc'
                or converted.get('codec_tag_string') != 'hvc1'
                or (converted.get('width'), converted.get('height')) !=
                   (picture.get('width'), picture.get('height'))
                or abs(output_duration - duration) > 0.5
                or abs(float(Fraction(converted['avg_frame_rate'])) -
                       float(Fraction(picture['avg_frame_rate']))) > 0.01
                or (audio is not None and
                    (converted_audio is None or converted_audio.get('codec_name') != 'aac'))):
            raise ValueError('Conversion validation failed')
        for key in ('color_transfer', 'color_primaries', 'color_space'):
            original = picture.get(key)
            if original and original not in ('unknown', 'unspecified') and converted.get(key) != original:
                raise ValueError('Conversion color metadata changed')
        if not attributes(temporary):
            raise ValueError('Conversion video attributes unavailable')
        temporary.replace(output)
        return output
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, StopIteration, ZeroDivisionError):
        raise ValueError('MKV conversion failed; source retained') from None
    finally:
        temporary.unlink(missing_ok=True)
