"""Send captured files, record Telegram receipts and fence uncertain deliveries."""

from collections import deque
import contextlib
import json
import mimetypes
from pathlib import Path
import time

from .. import core as c, limits, links, video
from ..translations import albums as translation_albums
from .telegram_api import TelegramError


def _save_file_receipts(pairs):
    def save(db):
        for file_id, message in pairs:
            kind = next((k for k in ('video', 'document') if isinstance(message.get(k), dict)), '')
            db.execute(
                "UPDATE files SET state='sent',telegram_id=?,telegram_media_kind=? WHERE id=?",
                (str(message['message_id']), kind, file_id),
            )
        if pairs:
            c.update_album_index(db, translation_albums.record_delivery, pairs, time.time())

    c.retry_write(save)


def _mark_file_states(files, state):
    def save(db):
        for file in files:
            db.execute('UPDATE files SET state=? WHERE id=?', (state, file['id']))

    c.retry_write(save)


def _save_file_rejection(job, files, error):
    retry_at = time.time() + max(5, error.retry)

    def save(db):
        for file in files:
            db.execute("UPDATE files SET state='pending' WHERE id=?", (file['id'],))
        db.execute(
            "UPDATE jobs SET state=?,retry_at=?,message='Telegram 拒絕傳送，檔案保留待重試' WHERE id=? AND cancel=0 AND state!='cancelled'",
            ('sending' if error.code == 429 else 'paused', retry_at, job['id']),
        )

    c.retry_write(save)


def _mark_delivery_uncertain(job):
    c.retry_write(
        lambda db: db.execute(
            "UPDATE jobs SET state='partial',message='傳送結果未明，檔案已保留。' WHERE id=? AND cancel=0 AND state!='cancelled'",
            (job['id'],),
        )
    )


def _delivery_batches(files, dimensions):
    # Telegram can mix photos and videos. Documents require their own album.
    pending = [(f, _album_kind(f, dimensions)) for f in files if f['state'] == 'pending']
    groups = {'visual': deque(), 'document': deque()}
    for f, kind in pending:
        if kind:
            groups[kind].append(f)
    for f, kind in pending:
        if kind is None:
            yield [f]
            continue
        group = groups[kind]
        if group and f is group[0]:
            # Leave two items for the last album instead of a lone eleventh item.
            count = 9 if len(group) == 11 else min(10, len(group))
            yield [group.popleft() for _ in range(count)]


def _video_dimensions(files):
    # Telegram mobile/forwarded messages rely on sender-supplied dimensions.
    # Probe the original in its own container. Unknown video attributes use document delivery.
    dimensions = {
        f['id']: video.attributes(c.safe_file(f['path']))
        for f in files
        if f['state'] == 'pending'
        and not f['force_document']
        and Path(f['path']).suffix.lower() in video.VIDEO_DEMUXERS
        and f['size'] <= limits.telegram_file_limit()
    }
    for f in files:
        if f['supports_streaming'] and dimensions.get(f['id']):
            dimensions[f['id']]['supports_streaming'] = True
    return dimensions


def _delivery_kind(f, dimensions):
    if not f['force_document']:
        if dimensions.get(f['id']):
            return 'video'
        if Path(f['path']).suffix.lower() in ('.jpg', '.jpeg', '.png') and f['size'] < 9 * 1024**2:
            return 'photo'
    return 'document'


def _album_kind(f, dimensions):
    if f['size'] > limits.telegram_file_limit():
        return None
    if _delivery_kind(f, dimensions) == 'document':
        return None if f['supports_streaming'] else 'document'
    return 'visual'


def _send_album(job, batch, dimensions, destination, send):
    _mark_file_states(batch, 'uncertain')
    try:
        with contextlib.ExitStack() as stack:
            attachments = {}
            media = []
            for n, f in enumerate(batch):
                name = 'file' + str(n)
                path = c.safe_file(f['path'])
                stream = stack.enter_context(path.open('rb'))
                mime_type = mimetypes.guess_type(f['name'])[0] or 'application/octet-stream'
                item = {'type': _delivery_kind(f, dimensions), 'media': 'attach://' + name}
                if item['type'] == 'video':
                    mime_type = video.VIDEO_MIME_TYPES[path.suffix.lower()]
                if item['type'] == 'document':
                    item['disable_content_type_detection'] = True
                    mime_type = 'application/octet-stream'
                attachments[name] = (f['name'], stream, mime_type)
                if item['type'] == 'video':
                    item.update(dimensions[f['id']])
                media.append(item)
            results = send('sendMediaGroup', {**destination, 'media': json.dumps(media)}, attachments)
        if (
            not isinstance(results, list)
            or len(results) != len(batch)
            or any(not isinstance(r, dict) or not r.get('message_id') for r in results)
        ):
            raise ValueError('Incomplete album response')
        _save_file_receipts([(f['id'], result) for f, result in zip(batch, results)])
        return True
    except TelegramError as error:
        if error.code >= 500 or error.code < 400:
            _mark_delivery_uncertain(job)
            raise
        if error.code in (400, 413):
            _mark_file_states(batch, 'pending')
            return False
        _save_file_rejection(job, batch, error)
        raise
    except Exception:
        for f in batch:
            f['state'] = 'uncertain'
        _mark_delivery_uncertain(job)
        raise


def _split_rejected_album(batch):
    # Isolate a lone large upload, or bisect the rejected album while keeping its order.
    large = [file for file in batch if file['size'] >= limits.LARGE_UPLOAD_THRESHOLD]
    if len(large) == 1:
        smaller = [file for file in batch if file is not large[0]]
        return (large, smaller) if batch[0] is large[0] else (smaller, large)
    middle = (len(batch) + 1) // 2
    return batch[:middle], batch[middle:]


def _send_single(job, f, dimensions, destination, send, status, cancelled):
    path = c.safe_file(f['path'])
    _mark_file_states([f], 'uncertain')
    try:
        if f['size'] > limits.telegram_file_limit():
            reason = (
                '檔案大於 500 MB，改提供下載連結。'
                if f['size'] > limits.MAX_TELEGRAM_BYTES
                else '目前雲端傳送介面限制為 49 MiB，改提供下載連結。'
            )
            if not links.enabled():
                _mark_file_states([f], 'too_large')
                f['state'] = 'too_large'
                c.setting_many(
                    {
                        'partial:' + job['id']: True,
                        'partial_reason:' + job['id']: reason.replace('改提供下載連結。', '')
                        + '尚未設定下載網址，檔案保留在本機，未傳送。',
                    }
                )
                return True
            result = send(
                'sendMessage',
                {
                    **destination,
                    'text': f['name'] + '\n' + reason + '\n檔案及連結保留 2 小時：' + links.create(f['id']),
                },
            )
        else:
            kind = _delivery_kind(f, dimensions)
            if f['supports_streaming'] and kind != 'video':
                _mark_file_states([f], 'preview_failed')
                f['state'] = 'preview_failed'
                return True
            data = dict(destination)
            mime_type = mimetypes.guess_type(f['name'])[0] or 'application/octet-stream'
            if kind == 'video':
                data.update(dimensions[f['id']])
                mime_type = video.VIDEO_MIME_TYPES[path.suffix.lower()]
            if kind == 'document' and (f['force_document'] or path.suffix.lower() in video.VIDEO_DEMUXERS):
                data['disable_content_type_detection'] = True
                mime_type = 'application/octet-stream'
            with path.open('rb') as stream:
                try:
                    result = send('send' + kind.title(), data, {kind: (f['name'], stream, mime_type)})
                except TelegramError as e:
                    if e.code != 400 or kind == 'document':
                        raise
                    if f['supports_streaming']:
                        _mark_file_states([f], 'preview_failed')
                        f['state'] = 'preview_failed'
                        return True
                    if cancelled(job):
                        _mark_file_states([f], 'pending')
                        c.update(job['id'], state='cancelled', cancel=1)
                        return False
                    stream.seek(0)
                    document_data = dict(destination)
                    document_data['disable_content_type_detection'] = True
                    result = send(
                        'sendDocument',
                        document_data,
                        {'document': (f['name'], stream, 'application/octet-stream')},
                    )
        if not isinstance(result, dict) or not result.get('message_id'):
            raise ValueError('Missing Telegram receipt')
        _save_file_receipts([(f['id'], result)])
    except TelegramError as e:
        if e.code >= 500 or e.code < 400:
            _mark_delivery_uncertain(job)
            return False
        _save_file_rejection(job, [f], e)
        status(job, 'Telegram 拒絕傳送，檔案保留待重試')
        return False
    except Exception:
        _mark_delivery_uncertain(job)
        status(job, '傳送結果未明，檔案保留；請管理員確認後重試')
        return False
    return True


def send_files(job, files, destination, *, send, status, cancelled):
    """Return True only after every upload has a durable outcome."""
    if any(file['state'] == 'uncertain' for file in files):
        _mark_delivery_uncertain(job)
        return False
    dimensions = _video_dimensions(files)
    deliveries = deque(_delivery_batches(files, dimensions))
    while deliveries:
        if cancelled(job):
            return False
        batch = deliveries.popleft()
        if len(batch) == 1:
            if not _send_single(job, batch[0], dimensions, destination, send, status, cancelled):
                return False
            continue
        try:
            if not _send_album(job, batch, dimensions, destination, send):
                deliveries.extendleft(reversed(_split_rejected_album(batch)))
        except Exception:
            status(job, '媒體分組傳送尚未完成，檔案保留待確認／重試')
            return False
    return True
