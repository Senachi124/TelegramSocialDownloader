"""Select eligible text and image sources from Telegram replies."""
from .. import command_reply

DOCUMENT_TYPES = {'image/jpeg', 'image/png', 'image/webp'}


def actor(message):
    actor = message.get('from', {})
    if isinstance(actor, dict) and not (actor.get('is_bot') or message.get('sender_chat') or message.get('via_bot')):
        return actor.get('id')
    return None


def other_group_member_reply(message, parent):
    """Allow only the visible, unprotected message from a human in the same group and topic."""
    chat, source_chat = message['chat'], parent.get('chat')
    if (chat.get('type') not in ('group', 'supergroup') or not isinstance(source_chat, dict)
            or source_chat.get('id') != chat['id'] or source_chat.get('type') != chat['type']
            or chat.get('has_protected_content') or source_chat.get('has_protected_content')):
        return False
    sender, mid = parent.get('from'), parent.get('message_id')
    if (not isinstance(sender, dict) or sender.get('is_bot') is not False
            or type(sender.get('id')) is not int or sender['id'] <= 0 or sender['id'] == actor(message)
            or type(mid) is not int or mid <= 0 or 'sender_chat' in parent or 'via_bot' in parent
            or parent.get('is_automatic_forward') or parent.get('has_protected_content')):
        return False
    if 'quote' in message:
        # Telegram can add a TextQuote to an ordinary reply without a user selection.
        quote = message['quote']
        if (not isinstance(quote, dict) or quote.get('is_manual', False) is not False
                or not isinstance(quote.get('text'), str) or type(quote.get('position')) is not int
                or quote['position'] < 0):
            return False
    if message.get('is_topic_message') or parent.get('is_topic_message'):
        topic = message.get('message_thread_id')
        return (message.get('is_topic_message') is True and parent.get('is_topic_message') is True
                and type(topic) is int and topic > 0 and type(parent.get('message_thread_id')) is int
                and parent['message_thread_id'] == topic)
    if chat.get('is_forum') or source_chat.get('is_forum'):
        return message.get('message_thread_id') == parent.get('message_thread_id')
    return True


def source(message, match):
    original = message.get('text') or message.get('caption') or ''
    text = original[match.end() + 1:] if match else original
    source = message
    if match and not text.strip() and not (message.get('photo') or message.get('document')):
        source = command_reply.local_parent(message)
        if other_group_member_reply(message, source):
            text = source.get('text')
            return text if isinstance(text, str) else '', None
        if 'quote' in message:
            quote = message['quote']
            if isinstance(quote, dict) and (quote.get('is_manual') is True or actor(source) == actor(message)):
                return command_reply.quote_text(quote), None
            return '', None
        if actor(source) != actor(message):
            return '', None
        text = source.get('text') or source.get('caption') or ''
    image = None
    if source.get('photo'):
        photo = max(source['photo'], key=lambda p: p['width'] * p['height'])
        image = (photo, 'photos', 'image/jpeg')
    elif (document := source.get('document', {})).get('mime_type') in DOCUMENT_TYPES:
        image = (document, 'documents', document['mime_type'])
    return text, image
