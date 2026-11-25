"""Decode web console forms in memory without retaining uploaded credentials."""
from email.parser import BytesParser
from email.policy import default
from urllib.parse import parse_qs


class FormError(ValueError):
    """A form error with a fixed user-facing message."""

    status = 400


class ServiceUnavailable(FormError):
    status = 503


def parse_form(content_type, raw, *, allow_upload=False):
    if not content_type.lower().startswith('multipart/form-data'):
        try:
            return {name: values[-1] for name, values in parse_qs(raw.decode('utf-8')).items()}
        except UnicodeError:
            raise FormError('Supply a UTF-8 encoded form.') from None
    if not allow_upload:
        raise FormError('This form does not accept uploads.')

    fields = {}
    upload = None
    allowed_fields = {'csrf', 'platform', 'action', 'id', 'label', 'cookie', 'policy'}
    try:
        headers = f'Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n'.encode('ascii')
        message = BytesParser(policy=default).parsebytes(headers + raw)
        if message.defects or not message.is_multipart():
            raise ValueError()
        for index, part in enumerate(message.iter_parts()):
            name = part.get_param('name', header='content-disposition')
            if index >= 12 or part.is_multipart() or part.defects:
                raise ValueError()
            if part.get_content_disposition() != 'form-data' or not name or name in fields:
                raise ValueError()
            data = part.get_payload(decode=True)
            if not isinstance(data, bytes) or len(data) > 131072:
                raise ValueError()
            value = data.decode('utf-8-sig')
            if part.get_filename() is not None:
                if name != 'youtube_cookie_file' or upload is not None:
                    raise ValueError()
                upload = value
            elif name in allowed_fields:
                fields[name] = value
            else:
                raise ValueError()
        if fields.get('platform') != 'youtube':
            raise ValueError()
    except (UnicodeError, TypeError):
        raise FormError('Supply a UTF-8 cookies.txt file.') from None
    except ValueError:
        raise FormError('Invalid cookie import (128 KiB maximum).') from None

    if upload and upload.strip():
        if fields.get('cookie', '').strip():
            raise FormError('Choose either an upload or pasted cookies.')
        fields['cookie'] = upload
    return fields
