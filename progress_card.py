"""Pure rendering and public-commentary extraction for one progress message."""
import math
import re
import time
import unicodedata

CARD_INTERVAL = 10
TERMINAL_STATES = frozenset({'ended', 'failed', 'cancelled', 'interrupted'})
_STATES = {
    'queued': '等待执行',
    'submitting': '正在提交',
    'accepted': '已交给 Codex，等待开始',
    'running': '正在执行',
    'waiting': '等待确认',
    'uncertain': '提交状态待确认',
    'ended': '本轮已结束',
    'failed': '执行未完成',
    'cancelled': '已取消',
    'interrupted': '执行中断，未自动重跑',
}
_ANSI = re.compile(r'\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])')


def _plain(text):
    text = _ANSI.sub('', text)
    text = ''.join(' ' if char.isspace() else char
                   for char in text if not unicodedata.category(char).startswith('C')
                   or char.isspace())
    return ' '.join(text.split())


def _bounded(text, limit):
    parts, units = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            # Make room for one ellipsis without splitting an astral character.
            while parts and units + 1 > limit:
                removed = parts.pop()
                units -= 2 if ord(removed) > 0xFFFF else 1
            return ''.join(parts) + ('…' if limit else '')
        parts.append(char)
        units += width
    return text


def clean(text, secrets=(), limit=320):
    """Return bounded one-line display text, redacting known credentials first.

    Limits count UTF-16 units, matching Telegram's text-size convention. ANSI
    escapes, control characters, direction overrides, and invalid surrogates
    are removed. Non-text input becomes empty text. A single secret string is
    accepted as one secret rather than interpreted as individual characters.
    """
    if not isinstance(text, str):
        return ''
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = 320
    limit = min(1000, max(0, limit))
    text = _plain(text)
    if isinstance(secrets, str):
        secrets = (secrets,)
    try:
        known = {_plain(value) for value in secrets if isinstance(value, str) and value.strip()}
    except TypeError:
        known = set()
    for secret in sorted(known - {''}, key=len, reverse=True):
        text = text.replace(secret, '[凭据已隐藏]')
    return _bounded(text, limit)


def _output_text(content):
    if not isinstance(content, list):
        return ''
    return ' '.join(part['text'] for part in content
                    if isinstance(part, dict) and part.get('type') == 'output_text'
                    and isinstance(part.get('text'), str))


def event_detail(event, secrets=()):
    """Extract only explicit public assistant commentary from known event shapes.

    Final answers, user messages, reasoning, tool/command events, and messages
    without an explicit commentary phase never enter the progress card.
    """
    if not isinstance(event, dict):
        return None
    text = ''
    event_type = event.get('type')
    if event_type == 'response_item':
        payload = event.get('payload')
        if (isinstance(payload, dict) and payload.get('type') == 'message'
                and payload.get('role') == 'assistant' and payload.get('phase') == 'commentary'):
            text = _output_text(payload.get('content'))
    elif event_type == 'event_msg':
        payload = event.get('payload')
        if (isinstance(payload, dict) and payload.get('type') == 'agent_message'
                and payload.get('phase') == 'commentary'
                and payload.get('role', 'assistant') == 'assistant'):
            text = payload.get('message')
    elif event_type in ('item.started', 'item.updated', 'item.completed'):
        item = event.get('item')
        if (isinstance(item, dict) and item.get('type') == 'agent_message'
                and item.get('phase') == 'commentary'
                and item.get('role', 'assistant') == 'assistant'):
            text = item.get('text')
    return clean(text, secrets=secrets) or None


def _timestamp(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    except OverflowError:
        return False


def _elapsed(started, finished):
    if not _timestamp(started) or not _timestamp(finished):
        return '待确认'
    seconds = int(max(0, finished - started))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f'{days} 天 {hours} 小时 {minutes} 分 {seconds} 秒'
    if hours:
        return f'{hours} 小时 {minutes} 分 {seconds} 秒'
    if minutes:
        return f'{minutes} 分 {seconds} 秒'
    return f'{seconds} 秒'


def render(card, now=None):
    """Render one plain-text card, bounded to 1000 UTF-16 units.

    Input fields are project, title, thread, state, started, finished, and detail.
    Times are Unix seconds. Terminal cards use only their stored finished time;
    missing or malformed timestamps display a pending duration rather than a
    changing or invented elapsed time. Optional ``secrets`` redacts every field
    and need not be persisted in card storage.
    """
    card = card if isinstance(card, dict) else {}
    secrets = card.get('secrets', ())
    project = clean(card.get('project'), secrets, 100) or '未指定项目'
    title = clean(card.get('title'), secrets, 160) or '未命名会话'
    thread = clean(card.get('thread'), secrets, 80)
    # Sanitize the complete identifier before taking its short display form.
    thread = thread if thread == '[凭据已隐藏]' else thread[:8]
    state = card.get('state')
    state = state if isinstance(state, str) else ''
    finish = card.get('finished') if state in TERMINAL_STATES else (time.time() if now is None else now)
    lines = ['【Codex 执行进度】', f'项目：{project}', f'任务：{title}',
             f'会话：{thread or "待分配"}', f'状态：{_STATES.get(state, "状态待确认")}',
             '耗时：' + _elapsed(card.get('started'), finish)]
    detail = clean(card.get('detail'), secrets)
    if detail:
        lines.append('进展：' + detail)
    return _bounded('\n'.join(lines), 1000)
