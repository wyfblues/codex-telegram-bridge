"""Pure rendering and public-commentary extraction for one progress message."""
import math
import json
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


def _integer(value, maximum=10**15):
    return (value if isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= maximum else None)


def _plan(value, secrets=()):
    if not isinstance(value, list):
        return None
    result = []
    for entry in value[:32]:
        if not isinstance(entry, dict):
            continue
        status = entry.get('status')
        status = 'in_progress' if status == 'inProgress' else status
        step = clean(entry.get('step'), secrets, 100)
        if step and status in ('pending', 'in_progress', 'completed'):
            result.append({'step': step, 'status': status})
    return result


def _path(value, secrets=()):
    return clean(value, secrets, 180)


def _files(value, secrets=()):
    if not isinstance(value, list):
        return None
    result = []
    for entry in value[:64]:
        if not isinstance(entry, dict):
            continue
        path = _path(entry.get('path'), secrets)
        if not path:
            continue
        item = {'path': path}
        for key in ('added', 'removed'):
            count = _integer(entry.get(key))
            if count is not None:
                item[key] = count
        result.append(item)
    return result


def _diff_path(value):
    value = value.split('\t', 1)[0]
    if value.startswith('"'):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return ''
    if value == '/dev/null':
        return ''
    return value[2:] if value.startswith(('a/', 'b/')) else value


def _diff_files(diff, secrets=()):
    """Read only unified-diff paths and hunk counts; never return diff content."""
    if not isinstance(diff, str) or len(diff) > 2_000_000:
        return None
    files, old_path, current, old_header = {}, '', None, False
    old_remaining = new_remaining = 0
    for line in diff.splitlines()[:50000]:
        if old_remaining or new_remaining:
            if line.startswith('+'):
                if current is not None and new_remaining:
                    current['added'] += 1
                    new_remaining -= 1
            elif line.startswith('-'):
                if current is not None and old_remaining:
                    current['removed'] += 1
                    old_remaining -= 1
            elif line.startswith(' '):
                old_remaining = max(0, old_remaining - 1)
                new_remaining = max(0, new_remaining - 1)
            elif not line.startswith('\\'):
                # A malformed hunk cannot make later arbitrary text into paths.
                old_remaining = new_remaining = 0
                current = None
            continue
        if line.startswith('diff --git '):
            old_path, current, old_header = '', None, False
        elif line.startswith('--- '):
            old_path = _diff_path(line[4:])
            current, old_header = None, True
        elif line.startswith('+++ ') and old_header:
            path = _path(_diff_path(line[4:]) or old_path, secrets)
            old_header = False
            if path and len(files) < 64:
                current = files.setdefault(path, {'path': path, 'added': 0, 'removed': 0})
        elif current is not None and line.startswith('@@ '):
            hunk = re.match(r'^@@ -\d{1,10}(?:,(\d{1,10}))? \+\d{1,10}(?:,(\d{1,10}))? @@', line)
            if hunk:
                old_remaining = int(hunk[1]) if hunk[1] is not None else 1
                new_remaining = int(hunk[2]) if hunk[2] is not None else 1
    return list(files.values())


def _patch_files(changes, secrets=()):
    if not isinstance(changes, list):
        return None
    result = []
    for change in changes[:64]:
        if not isinstance(change, dict):
            continue
        path = _path(change.get('path'), secrets)
        if not path:
            continue
        entry = {'path': path}
        # Per-file app-server diffs may omit file headers. Add synthetic headers
        # for counting only; the displayed path always comes from structured data.
        diff = change.get('diff')
        if isinstance(diff, str) and len(diff) <= 2_000_000:
            parsed = _diff_files('--- a/file\n+++ b/file\n' + diff)
            if parsed:
                entry.update(added=sum(item['added'] for item in parsed),
                             removed=sum(item['removed'] for item in parsed))
        result.append(entry)
    return result


def _usage(value, camel=False):
    if not isinstance(value, dict):
        return None
    last = value.get('last' if camel else 'last_token_usage')
    used = _integer(last.get('totalTokens' if camel else 'total_tokens')) if isinstance(last, dict) else None
    window = _integer(value.get('modelContextWindow' if camel else 'model_context_window'))
    if used is None:
        return None
    result = {'used': used}
    if window:
        result['window'] = window
    return result


def event_info(event, secrets=()):
    """Extract bounded structured public progress, without tool or reasoning text.

    The caller must bind each event to its exact thread and turn before storing
    these fields. In particular rollout token-count events have no turn ID.
    Returned fields are optional: plan, files, model, effort, token_usage. Plans
    and files retain at most 32/64 entries; rendering shows only 4/3 entries.
    """
    if not isinstance(event, dict):
        return {}
    result = {}
    method, params = event.get('method'), event.get('params')
    if isinstance(params, dict):
        if method == 'turn/plan/updated':
            plan = _plan(params.get('plan'), secrets)
            if plan is not None:
                result['plan'] = plan
        elif method == 'turn/diff/updated':
            files = _diff_files(params.get('diff'), secrets)
            if files is not None:
                result['files'] = files
        elif method == 'item/fileChange/patchUpdated':
            files = _patch_files(params.get('changes'), secrets)
            if files is not None:
                result['files'] = files
        elif method == 'item/completed':
            item = params.get('item')
            if (isinstance(item, dict) and item.get('type') == 'fileChange'
                    and item.get('status') != 'failed'):
                files = _patch_files(item.get('changes'), secrets)
                if files is not None:
                    result['files'] = files
        elif method == 'thread/tokenUsage/updated':
            usage = _usage(params.get('tokenUsage'), camel=True)
            if usage is not None:
                result['token_usage'] = usage
        return result
    kind, payload = event.get('type'), event.get('payload')
    if kind in ('item.started', 'item.updated', 'item.completed'):
        item = event.get('item')
        if not isinstance(item, dict):
            return result
        if item.get('type') == 'todo_list' and isinstance(item.get('items'), list):
            plan = []
            for step in item['items'][:32]:
                if (isinstance(step, dict) and isinstance(step.get('completed'), bool)
                        and isinstance(step.get('text'), str)):
                    plan.append({'step': step['text'],
                                 'status': 'completed' if step['completed'] else 'pending'})
            result['plan'] = _plan(plan, secrets)
        elif (item.get('type') == 'file_change' and kind == 'item.completed'
              and item.get('status') != 'failed'):
            files = _files(item.get('changes'), secrets)
            if files is not None:
                result['files'] = files
        return result
    if not isinstance(payload, dict):
        return result
    if kind == 'turn_context':
        for key in ('model', 'effort'):
            value = clean(payload.get(key), secrets, 80)
            if value:
                result[key] = value
    elif kind == 'event_msg' and payload.get('type') == 'token_count':
        usage = _usage(payload.get('info'))
        if usage is not None:
            result['token_usage'] = usage
    elif (kind == 'response_item' and payload.get('type') == 'function_call'
          and payload.get('name') in ('update_plan', 'functions.update_plan')):
        arguments = payload.get('arguments')
        if isinstance(arguments, str) and len(arguments) <= 32768:
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError, RecursionError):
                arguments = None
        if isinstance(arguments, dict):
            plan = _plan(arguments.get('plan'), secrets)
            if plan is not None:
                result['plan'] = plan
    elif (kind == 'event_msg' and payload.get('type') == 'patch_apply_end'
          and payload.get('success') is True and isinstance(payload.get('changes'), dict)):
        # Only confirmed patch completion may supply paths. The patch body and
        # tool output are deliberately ignored, even when available.
        result['files'] = _files([{'path': path} for path in payload['changes']][:64], secrets)
    return result


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
    Optional public fields: plan [{step,status}], files [{path,added,removed}],
    model, effort, token_usage {used,window}, and queue_count. File counts and
    window are optional. Status values are pending/in_progress/completed.
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
    model = clean(card.get('model'), secrets, 80)
    effort = clean(card.get('effort'), secrets, 40)
    if model or effort:
        lines.append('运行：' + ' · '.join(value for value in (model, effort) if value))
    queue_count = _integer(card.get('queue_count'))
    if queue_count:
        lines.append(f'等待任务：{queue_count}')
    usage = card.get('token_usage')
    if isinstance(usage, dict):
        used, window = _integer(usage.get('used')), _integer(usage.get('window'))
        if used is not None:
            lines.append('上下文：' + f'{used:,}' + (f' / {window:,} tokens' if window else ' tokens'))
    plan = _plan(card.get('plan'), secrets)
    if plan:
        lines.append('计划：')
        symbols = {'completed': '✓', 'in_progress': '→', 'pending': '○'}
        lines.extend(symbols[step['status']] + ' ' + clean(step['step'], secrets, 80)
                     for step in plan[:4])
        if len(plan) > 4:
            lines.append(f'另有 {len(plan) - 4} 项')
    files = _files(card.get('files'), secrets)
    if files:
        lines.append('文件：')
        for item in files[:3]:
            counts = []
            if 'added' in item:
                counts.append(f'+{item["added"]}')
            if 'removed' in item:
                counts.append(f'-{item["removed"]}')
            lines.append(clean(item['path'], secrets, 90) + ('  ' + ' '.join(counts) if counts else ''))
        if len(files) > 3:
            lines.append(f'另有 {len(files) - 3} 个文件')
    return _bounded('\n'.join(lines), 1000)
