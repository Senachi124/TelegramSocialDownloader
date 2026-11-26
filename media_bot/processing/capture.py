"""Run download adapters in isolated processes and enforce media storage limits."""

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from .. import core as c, limits, storage, video


class CaptureTimeout(ValueError):
    pass


class CaptureStorageError(ValueError):
    pass


def disk_ok():
    if shutil.disk_usage(c.DATA).free < 5 * 1024**3:
        raise CaptureStorageError('可用空間不足 5 GiB，暫停新增下載')
    storage.check(download=True)
    size = 0
    for path in (c.DATA / 'media').rglob('*'):
        try:
            if not path.is_symlink() and path.is_file():
                size += path.stat().st_size
        except FileNotFoundError:
            continue
    if size >= 3 * 1024**3:
        raise ValueError('Bot 暫存已達 3 GiB，請管理員處理未交付工作')


def _oversized_download(root):
    # Never total the directory: split video/audio and their merged output coexist.
    for path in root.rglob('*'):
        # Reserved scratch from a previous release is never delivered.
        if video.is_playback_scratch(path):
            continue
        try:
            if not path.is_symlink() and path.is_file() and path.stat().st_size > limits.MAX_DOWNLOAD_BYTES:
                return True
        except FileNotFoundError:
            pass
    return False


def run(job, cookies, *, root, should_cancel, stop, timeout):
    if root is None:
        root = c.DATA / 'media' / job['id']
        if '_batch_position' in job:
            root = root / ('item-' + str(job['_batch_position']))
    root.mkdir(parents=True, exist_ok=True)
    video.cleanup_playback_scratch(root)
    result_path = root / 'result.json'
    result_path.unlink(missing_ok=True)
    runner = 'media_bot.threads.runner' if job['platform'] == 'threads' else 'media_bot.gallery_runner'
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        [sys.executable, '-m', runner],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )
    try:
        snapshot = json.loads(job.get('preferences') or '{}')
        preferences = snapshot.get(job['platform'], {}) if isinstance(snapshot, dict) else {}
        payload = {
            'platform': job['platform'], 'mode': job['mode'], 'url': job['url'],
            'root': str(root), 'cookies': cookies, 'preferences': preferences,
        }
        if job['platform'] == 'youtube' and cookies and job.get('account_revision') is not None:
            payload['youtube_account'] = {'id': job['account_id'], 'revision': job['account_revision']}
        process.stdin.write(json.dumps(payload))
        process.stdin.close()
        while process.poll() is None:
            if _oversized_download(root):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)
                return {
                    'code': 1,
                    'retry_at': 0,
                    'auth_required': False,
                    'files': [],
                    'failure': 'too_large',
                    'oversize': True,
                }
            if stop.is_set():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=10)
                return None
            if should_cancel():
                return None
            if time.monotonic() > deadline:
                raise CaptureTimeout('下載逾時，已保留檔案')
            if shutil.disk_usage(c.DATA).free < 5 * 1024**3:
                raise CaptureStorageError('可用空間不足 5 GiB，已保留檔案')
            time.sleep(0.2)
        if should_cancel():
            return None
        result = json.loads(result_path.read_text())
        if _oversized_download(root):
            result.update(code=1, failure='too_large', oversize=True, files=[])
        return result
    finally:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        video.cleanup_playback_scratch(root)
