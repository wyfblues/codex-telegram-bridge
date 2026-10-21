"""Render a small Markdown subset into independently safe Telegram messages.

Only bold, inline/fenced code and HTTP(S) links create HTML. Other syntax stays
readable text; raw HTML is always escaped. Each part contains matching visible
plain text and HTML, so callers can safely retry rejected HTML as plain text.
"""
import html
import re
from urllib.parse import urlsplit

_BOLD = ('<b>', '</b>')
_CODE = ('<code>', '</code>')
_PRE = ('<pre>', '</pre>')
_ESCAPABLE = frozenset('\\`*_{}[]()#+-.!|>~')
_FENCE = re.compile(r'^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)(?:\r?\n)?$')
_TABLE_SEPARATOR = re.compile(r'^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$')


def _append(runs, text, tags=()):
    if not text:
        return
    if runs and runs[-1][1] == tags and len(runs[-1][0]) + len(text) <= 8192:
        runs[-1] = (runs[-1][0] + text, tags)
    else:
        runs.append((text, tags))


def _find(text, marker, start):
    """Find an unescaped exact delimiter, leaving unmatched syntax literal."""
    pos = text.find(marker, start)
    while pos >= 0:
        slash, before = 0, pos - 1
        while before >= 0 and text[before] == '\\':
            slash += 1
            before -= 1
        if slash % 2 == 0:
            return pos
        pos = text.find(marker, pos + len(marker))
    return -1


def _code_end(text, marker, start):
    end = _find(text, marker, start)
    while end >= 0:
        if ((end == 0 or text[end - 1] != '`')
                and (end + len(marker) == len(text) or text[end + len(marker)] != '`')):
            return end
        end = _find(text, marker, end + len(marker))
    return -1


def _bold_end(text, marker, start):
    cursor = start
    while cursor < len(text):
        if text[cursor] == '\\':
            cursor += 2
            continue
        if text[cursor] == '`':
            size = 1
            while cursor + size < len(text) and text[cursor + size] == '`':
                size += 1
            end = _code_end(text, '`' * size, cursor + size)
            if end >= 0:
                cursor = end + size
                continue
            cursor += size
            continue
        if text.startswith(marker, cursor):
            return cursor
        cursor += 1
    return -1


def _http_url(value):
    if (not value or len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value)
            or '\\' in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        # Accessing port also validates malformed ports and bracketed hosts.
        parsed.port
    except (ValueError, TypeError):
        return None
    return value


def _link_end(text, start):
    """Return the closing parenthesis of a bounded balanced link destination."""
    depth = 1
    cursor = start
    while cursor < len(text) and cursor - start <= 4096:
        char = text[cursor]
        if char == '\\':
            cursor += 2
            continue
        if char in '\r\n':
            return -1
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if not depth:
                return cursor
        cursor += 1
    return -1


def _inline(text, tags=(), depth=0):
    if depth >= 8:
        return [(text, tags)]
    runs, literal = [], []
    cursor = 0
    has_links = '](' in text

    def flush():
        if literal:
            _append(runs, ''.join(literal), tags)
            literal.clear()

    while cursor < len(text):
        char = text[cursor]
        if char == '\\' and cursor + 1 < len(text) and text[cursor + 1] in _ESCAPABLE:
            literal.append(text[cursor + 1])
            cursor += 2
            continue
        if char == '`':
            size = 1
            while cursor + size < len(text) and text[cursor + size] == '`':
                size += 1
            marker = '`' * size
            end = _code_end(text, marker, cursor + size)
            if end > cursor + size:
                flush()
                # Code entities cannot overlap other Telegram formatting.
                _append(runs, text[cursor + size:end], (_CODE,))
                cursor = end + size
                continue
            literal.append(marker)
            cursor += size
            continue
        if text.startswith(('**', '__'), cursor):
            marker = text[cursor:cursor + 2]
            end = _bold_end(text, marker, cursor + 2)
            if (end > cursor + 2 and not text[cursor + 2].isspace()
                    and not text[end - 1].isspace()):
                flush()
                bold_tags = tags if _BOLD in tags else tags + (_BOLD,)
                for value, nested in _inline(text[cursor + 2:end], bold_tags, depth + 1):
                    _append(runs, value, nested)
                cursor = end + 2
                continue
        if char == '[' and has_links and (cursor == 0 or text[cursor - 1] != '!'):
            middle = _find(text, '](', cursor + 1)
            if middle >= 0:
                end = _link_end(text, middle + 2)
                if end >= 0:
                    label = text[cursor + 1:middle]
                    destination = text[middle + 2:end]
                    url = _http_url(destination)
                    if label and url:
                        flush()
                        anchor = ('<a href="' + html.escape(url, quote=True) + '">', '</a>')
                        for value, nested in _inline(label, tags + (anchor,), depth + 1):
                            _append(runs, value, nested)
                        # Both representations retain the destination, including
                        # plain fallback when Telegram rejects an HTML entity.
                        _append(runs, ' (' + url + ')', tags)
                    else:
                        # Unsupported destinations, including local paths, stay
                        # literal. No local path is turned into a public URL.
                        literal.append(text[cursor:end + 1])
                    cursor = end + 1
                    continue
                # Once a candidate destination is malformed, preserve its
                # remaining literal text in one pass. This also bounds work
                # for adversarial runs of opening brackets without a close.
                literal.append(text[cursor:])
                cursor = len(text)
                continue
            has_links = False
        literal.append(char)
        cursor += 1
    flush()
    return runs


def _line_body(line):
    if line.endswith('\r\n'):
        return line[:-2], '\r\n'
    if line.endswith(('\n', '\r')):
        return line[:-1], line[-1:]
    return line, ''


def _table_row(line):
    body, ending = _line_body(line)
    stripped = body.strip()
    if stripped.startswith('|'):
        stripped = stripped[1:]
    if stripped.endswith('|') and not stripped.endswith('\\|'):
        stripped = stripped[:-1]
    # Retain cell text and its order. A simple separator avoids relying on
    # monospace table alignment in Telegram's proportional message font.
    return stripped.strip() + ending


def _runs(text):
    lines = text.splitlines(keepends=True)
    runs = []
    cursor = 0
    while cursor < len(lines):
        line = lines[cursor]
        opening = _FENCE.match(line)
        if opening:
            marker = opening[1]
            closing = cursor + 1
            close_pattern = re.compile(r'^[ \t]{0,3}' + re.escape(marker[0])
                                       + '{' + str(len(marker)) + r',}[ \t]*(?:\r?\n)?$')
            while closing < len(lines) and not close_pattern.match(lines[closing]):
                closing += 1
            if closing < len(lines):
                content = ''.join(lines[cursor + 1:closing])
                _append(runs, content, (_PRE,))
                # An empty block still separates the surrounding paragraphs.
                if not content:
                    _append(runs, _line_body(lines[closing])[1])
                cursor = closing + 1
                continue
            # A broken fence is preserved literally, rather than interpreting
            # fragments of its intended code as formatting or links.
            _append(runs, ''.join(lines[cursor:]))
            break
        if (cursor + 1 < len(lines) and '|' in line
                and _TABLE_SEPARATOR.match(_line_body(lines[cursor + 1])[0])):
            for value, tags in _inline(_table_row(line)):
                _append(runs, value, tags)
            cursor += 2
            while cursor < len(lines) and '|' in lines[cursor] and lines[cursor].strip():
                for value, tags in _inline(_table_row(lines[cursor])):
                    _append(runs, value, tags)
                cursor += 1
            continue
        for value, tags in _inline(line):
            _append(runs, value, tags)
        cursor += 1
    return runs


def render_parts(text, limit=3400):
    """Return matching plain/HTML parts, bounded by displayed UTF-16 units.

    Every part is independently valid HTML, with no split entity or surrogate
    pair. Concatenating ``text`` fields reproduces all rendered visible text.
    A positive integer limit is required; limit=1 cannot contain astral Unicode
    and raises ValueError for such input. Empty/non-string input returns [].
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError('limit must be a positive integer')
    if not isinstance(text, str) or not text:
        return []
    parts, plain, markup = [], [], []
    current_tags = ()
    units = 0

    def close_part():
        nonlocal units, current_tags
        if not plain:
            return
        markup.extend(tag[1] for tag in reversed(current_tags))
        parts.append({'text': ''.join(plain), 'html': ''.join(markup)})
        plain.clear()
        markup.clear()
        current_tags = ()
        units = 0

    for value, tags in _runs(text):
        for char in value:
            width = 2 if ord(char) > 0xFFFF else 1
            if width > limit:
                raise ValueError('limit is too small for an astral Unicode character')
            if units + width > limit:
                close_part()
            if current_tags != tags:
                common = 0
                while common < min(len(current_tags), len(tags)) and current_tags[common] == tags[common]:
                    common += 1
                markup.extend(tag[1] for tag in reversed(current_tags[common:]))
                markup.extend(tag[0] for tag in tags[common:])
                current_tags = tags
            plain.append(char)
            markup.append(html.escape(char, quote=False))
            units += width
    close_part()
    return parts
