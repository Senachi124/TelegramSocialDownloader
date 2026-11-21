"""Probe Telegram video attributes and prepare a playback copy for incompatible videos."""
from contextlib import contextmanager
import errno
import fcntl
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time
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


def is_playback_derivative(path):
    return Path(path).name.lower().endswith(('.h265.mp4', '.encoding.mp4'))


class SourceProbeError(ValueError):
    """Source could not be identified as the expected local video type."""


def _playback_timeout(maximum):
    # The worker and its capture subprocess share the same monotonic clock.
    # Leave time to close FFmpeg and record the original in result.json.
    deadline = os.environ.get('MEDIA_BOT_CAPTURE_DEADLINE')
    if deadline is None:
        return maximum
    remaining = float(deadline) - time.monotonic() - 5
    if remaining <= 0:
        raise TimeoutError('Playback preparation deadline reached')
    return min(maximum, remaining)


def _conversion_probe(path, container, pass_fds=()):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-protocol_whitelist', 'file',
        '-f', container, '-show_streams', '-show_format', '-of', 'json', str(path),
    ], capture_output=True, text=True, timeout=_playback_timeout(15), check=True, pass_fds=pass_fds)
    data = json.loads(result.stdout)
    picture = next(stream for stream in data['streams'] if stream['codec_type'] == 'video')
    audio = next((stream for stream in data['streams'] if stream['codec_type'] == 'audio'), None)
    duration = float(data['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid media duration')
    return picture, audio, duration


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


@contextmanager
def _playback_output(output):
    """Stage on the same mount. Child processes inherit the locked descriptor."""
    directory = os.open(output.parent, os.O_DIRECTORY)
    scratch = '.playback-' + uuid.uuid4().hex + '.encoding.mp4'
    fd = None
    named = False
    try:
        try:
            fd = os.open('.', os.O_RDWR | os.O_TMPFILE, 0o600, dir_fd=directory)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.EISDIR, errno.EOPNOTSUPP, errno.ENOENT):
                raise
            # If a reaper wins the initial create/lock race, recreate the name.
            while True:
                fd = os.open(scratch, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                fcntl.flock(fd, fcntl.LOCK_EX)
                if os.fstat(fd).st_nlink:
                    break
                os.close(fd)
                fd = None
            named = True
        # ffmpeg faststart reopens its output, so use a seekable local fd path.
        path = Path(f'/proc/self/fd/{fd}')
        yield path, fd
        if not named:
            # Publish new outputs directly. An update stages its fully verified
            # link under the same lock so a killed publisher can be reaped.
            try:
                os.link(path, output.name, dst_dir_fd=directory, follow_symlinks=True)
                return
            except FileExistsError:
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.link(path, scratch, dst_dir_fd=directory, follow_symlinks=True)
                named = True
        os.replace(scratch, output.name, src_dir_fd=directory, dst_dir_fd=directory)
        named = False
    finally:
        if named:
            try:
                os.unlink(scratch, dir_fd=directory)
            except FileNotFoundError:
                pass
        if fd is not None:
            os.close(fd)
        os.close(directory)


def telegram_playback(path):
    """Return a compatible original or publish a verified HEVC playback copy.

    The dedicated .h265.mp4 derivative is regenerated on retries. The worker's
    existing process-group cancellation and disk checks also cover FFmpeg.
    """
    from . import download_size

    source = Path(path)
    container = VIDEO_DEMUXERS.get(source.suffix.lower())
    if not container or source.is_symlink() or not source.is_file():
        raise ValueError('Invalid media source')
    source = source.resolve()
    output = source.with_name(source.name + '.h265.mp4')
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise ValueError('Invalid conversion output')
    try:
        download_size.check(source.stat().st_size)
        try:
            picture, audio, duration = _conversion_probe(source, container)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, StopIteration):
            raise SourceProbeError('Unrecognized video source') from None
        if (source.suffix.lower() == '.mp4' and picture['codec_name'] in ('h264', 'hevc')
                and (audio is None or audio['codec_name'] == 'aac')):
            return source
        command = [
            'ffmpeg', '-v', 'error', '-nostdin', '-y',
            '-xerror', '-noautorotate', '-protocol_whitelist', 'file', '-f', container, '-i', str(source),
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
        with _playback_output(output) as (temporary, fd):
            command += [
                '-tag:v', 'hvc1', '-movflags', '+faststart',
                '-fs', str(download_size.limits.MAX_DOWNLOAD_BYTES + 1), '-f', 'mp4', str(temporary),
            ]
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=_playback_timeout(1500), check=True, pass_fds=(fd,))
            download_size.check(temporary.stat().st_size)
            converted, converted_audio, output_duration = _conversion_probe(temporary, 'mov', (fd,))
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
            if not _display_attributes(converted, output_duration):
                raise ValueError('Conversion video attributes unavailable')
        return output
    except SourceProbeError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, StopIteration, ZeroDivisionError):
        raise ValueError('Video conversion failed; source retained') from None


def prepare_playback_files(result):
    """Record each completed original once and add a separate preview when needed."""
    from . import download_size, media_preferences

    if result.get('code') or result.get('oversize'):
        return
    files = []
    for item in result['files']:
        path = Path(item['path'])
        if path.suffix.lower() not in VIDEO_DEMUXERS:
            files.append(item)
            continue
        try:
            preview = telegram_playback(path)
        except (ValueError, download_size.DownloadTooLarge) as error:
            files.append(dict(item, force_document=True))
            if isinstance(error, SourceProbeError) and path.suffix.lower() == '.mp4':
                media_preferences.record_notice(result, '未能識別 MP4 影音格式，將以文件傳送原檔。')
            else:
                result.update(code=1, failure='conversion_failed')
                media_preferences.record_notice(result, '影片預覽轉換失敗，將以文件傳送原檔。')
            continue
        if preview == path.resolve():
            files.append(item)
        else:
            files.append({'path': str(preview), 'name': preview.name, 'size': preview.stat().st_size,
                          'supports_streaming': True})
            files.append(dict(item, force_document=True))
            media_preferences.record_notice(result, '已產生 H.265 MP4 播放版，並另附原始檔案。')
    result['files'] = files
