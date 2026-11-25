"""Resolve the Telegram content selected by a reply command."""


def local_parent(message):
    """Only reply_to_message carries the original from the current chat."""
    parent = message.get('reply_to_message')
    if not isinstance(parent, dict) or 'external_reply' in message:
        return {}
    if 'chat' in parent:
        chat = parent['chat']
        if not isinstance(chat, dict) or chat.get('id') != message.get('chat', {}).get('id'):
            return {}
    return parent


def _rich_text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return ''.join(_rich_text(part) for part in node)
    if not isinstance(node, dict):
        return ''
    if node.get('type') == 'custom_emoji':
        value = node.get('alternative_text')
    elif node.get('type') == 'mathematical_expression':
        value = node.get('expression')
    else:
        return _rich_text(node.get('text'))
    return value if isinstance(value, str) else ''


def quote_text(quote):
    if not isinstance(quote, dict):
        return ''
    text = quote.get('text')
    if isinstance(text, str) and text:
        return text
    blocks = quote.get('rich_texts')
    return '\n'.join(_rich_text(block) for block in blocks) if isinstance(blocks, list) else ''


def _selected_links(text, position, original, entities):
    # Telegram omits link entities in quotes. Its approximate UTF-16 position
    # is usable only when the quoted text still matches that exact span.
    if not text or type(position) is not int or position < 0:
        return []
    selection = text.encode('utf-16-le', errors='surrogatepass')
    if not original.encode('utf-16-le', errors='surrogatepass').startswith(selection, 2 * position):
        return []
    end = position + len(selection) // 2
    links = []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get('type') not in ('url', 'text_link'):
            continue
        start, length = entity.get('offset'), entity.get('length')
        if type(start) is not int or type(length) is not int or length <= 0:
            continue
        stop = start + length
        left, right = max(start, position), min(stop, end)
        if left >= right:
            continue
        if start == left and stop == right:
            links.append({'type': entity['type'], 'offset': start - position, 'length': length,
                          'url': entity.get('url')})
        else:
            # A clipped URL or URL-looking link label is not a complete link.
            links.append({'type': 'text_link', 'offset': left - position, 'length': right - left})
    return links


def reply_content(message):
    """Return text and relative link entities for downloading, without media."""
    parent = local_parent(message)
    key = 'text' if isinstance(parent.get('text'), str) else 'caption'
    original = parent.get(key, '')
    entities = parent.get('entities' if key == 'text' else 'caption_entities', [])
    original = original if isinstance(original, str) else ''
    entities = entities if isinstance(entities, list) else []
    if 'quote' not in message:
        return {'text': original, 'entities': entities} if parent else None

    quote = message['quote']
    if not isinstance(quote, dict) or (not parent and quote.get('is_manual') is not True):
        return None
    text = quote_text(quote)
    links = _selected_links(text, quote.get('position'), original, entities)
    # Some transports may supply relative link entities directly on the quote.
    return {'text': text, 'entities': links or quote.get('entities', [])}
