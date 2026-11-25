"""Split translated text at Telegram message boundaries."""
import unicodedata

TELEGRAM_TEXT_UNITS = 4096


def _emoji_continues(text, cut):
    char = text[cut]
    if '🇦' <= char <= '🇿' and '🇦' <= text[cut-1] <= '🇿':
        start = cut - 1
        while start and '🇦' <= text[start-1] <= '🇿':
            start -= 1
        return (cut - start) % 2 == 1
    return (unicodedata.category(char).startswith('M') or char == '\u200d' or text[cut-1] == '\u200d'
            or '\U0001f3fb' <= char <= '\U0001f3ff' or '\U000e0020' <= char <= '\U000e007f')


def reply_chunks(text):
    """Keep ordinary paragraphs intact and keep each send within 4096 UTF-16 units."""
    byte_limit = 2 * TELEGRAM_TEXT_UNITS
    while len(text.encode('utf-16-le')) > byte_limit:
        prefix = text.encode('utf-16-le')[:byte_limit].decode('utf-16-le', errors='ignore')
        limit = len(prefix)
        cut = limit
        for separator in ('\n\n', '\n', ' '):
            index = prefix.rfind(separator, limit // 2)
            if index >= 0:
                cut = index + len(separator)
                break
        while cut > 0 and _emoji_continues(text, cut):
            cut -= 1
        if cut == 0:
            cut = limit  # A single sequence can exceed Telegram's message limit.
        if text[:cut].strip():
            yield text[:cut]
        text = text[cut:]
    if text.strip():
        yield text
