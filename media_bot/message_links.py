"""Offline extraction of message links; URL policy stays with the caller."""
import re
import unicodedata

# Consume whole URL tokens (including unsupported schemes/hosts) so a nested
# HTTPS URL can never be promoted into an independent download request.
_START = re.compile(
    r'(?<![A-Za-z0-9_@/%=.+-])(?:[A-Za-z][A-Za-z0-9+.-]*://|(?:vt|vm)\.tiktok\.com/)')
_DELIMITERS = set("<>\"'`，。；！？、：（）【】「」『』“”‘’")
_TRAILING = '.,;!?:'
_PAIRS = {')': '(', ']': '[', '}': '{'}
_SHORTLINK_DECORATIONS = re.compile(
    r'^(?P<link>(?:https://)?(?:vt|vm)\.tiktok\.com/[A-Za-z0-9_-]+/?)'
    r'(?P<suffix>[^\x00-\x7f]+)$')


def _clean_shortlink(value):
    # TikTok short codes are ASCII. Only remove a trailing decorative sequence
    # after a complete short code; never alter queries, fragments or other hosts.
    match = _SHORTLINK_DECORATIONS.fullmatch(value)
    if match:
        suffix = match['suffix']
        if (unicodedata.category(suffix[0]).startswith('S')
                and all(unicodedata.category(char).startswith('S')
                        or char in ('\ufe0e', '\ufe0f', '\u200d') for char in suffix)):
            return match['link']
    return value


_SCHEME_PREFIX = re.compile(r'(?:javascript|data|file|http|https|ftp|mailto|tel):$', re.I)


def _visible_links(text):
    cursor = 0
    while match := _START.search(text, cursor):
        end = match.end()
        brackets = []
        while end < len(text):
            char = text[end]
            if char.isspace() or char in _DELIMITERS:
                break
            if char in _PAIRS:
                if not brackets or brackets[-1] != _PAIRS[char]:
                    break
                brackets.pop()
            elif char in _PAIRS.values():
                brackets.append(char)
            end += 1
        cursor = end
        # A prose label such as link: is fine; a known non-HTTPS scheme is not.
        if not _SCHEME_PREFIX.search(text[:match.start()]):
            yield match.start(), end, _clean_token(text[match.start():end])


def _clean_token(value):
    while value:
        before = value
        value = value.rstrip(_TRAILING)
        if value and value[-1] in _PAIRS:
            close = value[-1]
            if value.count(close) > value.count(_PAIRS[close]):
                value = value[:-1]
        if value == before:
            break
    return _clean_shortlink(value)


def _https(value):
    return 'https://' + value if value.startswith(('vt.tiktok.com/', 'vm.tiktok.com/')) else value


def candidates(text):
    """Visible candidates in order, retaining duplicates for legacy callers."""
    return [_https(value) for _, _, value in _visible_links(text)
            if value.startswith(('https://', 'vt.tiktok.com/', 'vm.tiktok.com/'))]


def _entity_links(text, entities):
    # Telegram offsets/lengths count UTF-16 code units, not Python characters.
    boundaries = {0: 0}
    offset = 0
    for index, char in enumerate(text):
        offset += 2 if ord(char) > 0xffff else 1
        boundaries[offset] = index + 1
    for entity in entities if isinstance(entities, list) else ():
        if not isinstance(entity, dict) or entity.get('type') not in ('url', 'text_link'):
            continue
        start, length = entity.get('offset'), entity.get('length')
        if type(start) is not int or type(length) is not int or length <= 0:
            continue
        if start not in boundaries or start + length not in boundaries:
            continue
        begin, end = boundaries[start], boundaries[start + length]
        value = entity.get('url') if entity['type'] == 'text_link' else _clean_shortlink(text[begin:end])
        # Even an unsupported entity masks its label: a hidden target must not
        # cause a URL-looking label to download a different resource.
        yield begin, end, value


def extract(text, entities, validate, classify):
    """Return unique, policy-approved URLs in visual order, without any I/O.

    Syntactic eligibility is not a promise that remote media is accessible;
    workers still resolve approved short links and apply normal download rules.
    """
    if not isinstance(text, str):
        return []
    links = list(_entity_links(text, entities))
    spans = [(start, end) for start, end, _ in links]
    for begin, finish, value in _visible_links(text):
        if any(begin < end and start < finish for start, end in spans):
            continue
        links.append((begin, finish, value))
    result, seen = [], set()
    for _, _, value in sorted(links, key=lambda link: link[0]):
        if not isinstance(value, str):
            continue
        try:
            url = validate(_https(value))
            classify(url)
        except (ValueError, UnicodeError):
            continue
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result
