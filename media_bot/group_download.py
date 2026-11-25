"""Recognize download commands and retain only their links for durable reception."""
import re

from . import command_reply, core as c

_ADDRESS = re.compile(r'^(?ai:(?:/download)?@([a-z0-9_]{1,32}))(?=\s|$)')
_PRIVATE_COMMAND = re.compile(r'^(?ai:/download(?:@([a-z0-9_]{1,32}))?)(?=\s|$)')
_TRAILING_ADDRESS = re.compile(r'(?<!\S)(?ai:@([a-z0-9_]{1,32}))(?=\s*\Z)')


def _entities(message):
    entities = message.get('entities' if message.get('text') else 'caption_entities', [])
    return entities if isinstance(entities, list) else []


def _visible_mention(message, match):
    before = match.string[:match.start()]
    start = len(before.encode('utf-16-le', errors='surrogatepass')) // 2
    end = start + len(match.group())  # The mention itself is ASCII.
    for entity in _entities(message):
        if not isinstance(entity, dict):
            continue
        offset, length = entity.get('offset'), entity.get('length')
        if type(offset) is not int or type(length) is not int or length <= 0:
            continue
        if offset < end and offset + length > start:
            kind = entity.get('type')
            if kind in ('bold', 'italic', 'underline', 'strikethrough'):
                continue
            if kind != 'mention' or offset != start or length != end - start:
                return False  # A link label or code sample cannot address the bot.
    return True


def _trailing_urls(message, match):
    # The unchanged prefix preserves Telegram's UTF-16 entity coordinates.
    # The common extractor ignores entities outside that prefix.
    return c.extract_message_urls({'text': match.string[:match.start()], 'entities': _entities(message)})


def command(message):
    kind = message.get('chat', {}).get('type')
    text = message.get('text') or message.get('caption')
    if not isinstance(text, str):
        return None
    if kind == 'private':
        return _PRIVATE_COMMAND.match(text)
    if kind in ('group', 'supergroup'):
        prefix = _ADDRESS.match(text)
        if text.lstrip().startswith('/'):
            return prefix
        if prefix:
            return prefix if _visible_mention(message, prefix) else None
        suffix = _TRAILING_ADDRESS.search(text)
        if suffix and _visible_mention(message, suffix) and _trailing_urls(message, suffix):
            return suffix
    return None


def target(message):
    chat = message.get('chat', {})
    actor = message.get('from', {})
    if (chat.get('type') not in ('group', 'supergroup')
            or type(chat.get('id')) is not int or chat['id'] >= 0
            or type(message.get('message_id')) is not int or message['message_id'] <= 0
            or type(actor.get('id')) is not int or actor['id'] <= 0
            or actor.get('is_bot') or message.get('sender_chat') or message.get('via_bot')
            or message.get('forward_origin')):
        return None
    match = command(message)
    return match.group(1).lower() if match else None


def command_urls(message, match):
    if match.re is _TRAILING_ADDRESS:
        return _trailing_urls(message, match)
    if not match.string[match.end():].strip():
        return c.extract_message_urls(command_reply.reply_content(message) or {})
    # Hidden links applied to the command or mention are not arguments. Keep
    # offsets relative to the full command text for Telegram's UTF-16 entities.
    arguments = [item for item in _entities(message) if isinstance(item, dict)
                 and type(item.get('offset')) is int and item['offset'] >= match.end()]
    return c.extract_message_urls({'text': match.string, 'entities': arguments})


def inbox_message(message):
    match = command(message)
    urls = command_urls(message, match)
    prefix = match.group()
    result = {'message_id': message.get('message_id'), 'text': ' '.join([prefix, *urls]),
              'entities': [], 'chat': {key: message['chat'][key] for key in ('id', 'type')}}
    if message.get('is_topic_message') and message.get('message_thread_id'):
        result.update(is_topic_message=True, message_thread_id=message['message_thread_id'])
    actor = message['from']
    result['from'] = {'id': actor['id'], 'is_bot': False}
    if result['chat']['type'] == 'private':
        result['from'].update({key: actor[key] for key in ('first_name', 'last_name', 'username') if key in actor})
    # Preserve exact targets (including punctuation in hidden links) across
    # replay without persisting the parent, quote, or unrelated conversation.
    offset = len(prefix) + 1  # Both command patterns only accept ASCII.
    for url in urls:
        length = sum(2 if ord(char) > 0xffff else 1 for char in url)
        result['entities'].append({'type': 'text_link', 'offset': offset, 'length': length, 'url': url})
        offset += length + 1
    return result
