"""Pure phone settings validation and bounded, read-only usage snapshots.

This module never calls a model, changes Codex configuration, or returns rollout
messages. Available options come from the connected server's model catalog.
"""
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from progress_card import clean

USAGE_FRESH_SECONDS = 15 * 60
MAX_SNAPSHOT_BYTES = 256 * 1024
MAX_METADATA_BYTES = 256 * 1024
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$')


def _number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    except OverflowError:
        return False


def _counter(value):
    # Protocol token counters are integers. Fractions are unknown, not zero.
    if _number(value) and 0 <= value <= 2 ** 63 - 1 and value % 1 == 0:
        return int(value)
    return None


def _identifier(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _stamp(value):
    if _number(value) and value >= 0:
        return float(value)
    if isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if result.tzinfo is not None:
                return result.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _unique_options(values, field):
    if not isinstance(values, list):
        return []
    result = []
    for item in values[:100]:
        value = item.get(field) if isinstance(item, dict) else item
        if _identifier(value) and value not in result:
            result.append(value)
    return result


def catalog_models(result):
    """Normalize a ``model/list`` result without inventing capabilities.

    Returned rows contain ``model``, ``id``, ``displayName``, ``efforts``,
    ``defaultEffort``, ``serviceTiers``, and ``isDefault``. Hidden/invalid rows
    are omitted. Fast tiers are available only when explicitly advertised.
    """
    result = _dict(result)
    if isinstance(result.get('result'), dict):
        result = result['result']
    data = result.get('data')
    if not isinstance(data, list):
        return []
    models = []
    seen = set()
    for row in data[:200]:
        if not isinstance(row, dict) or row.get('hidden') is True:
            continue
        model = _identifier(row.get('model')) or _identifier(row.get('id'))
        if not model or model in seen:
            continue
        efforts = _unique_options(row.get('supportedReasoningEfforts'), 'reasoningEffort')
        default = row.get('defaultReasoningEffort')
        # Some future servers can omit a default; do not guess one.
        if default not in efforts:
            default = None
        tiers = _unique_options(row.get('serviceTiers'), 'id')
        if not tiers:
            tiers = _unique_options(row.get('supportedServiceTiers'), 'serviceTier')
        default_tier = _identifier(row.get('defaultServiceTier'))
        models.append({'model': model, 'id': _identifier(row.get('id')) or model,
                       'displayName': clean(row.get('displayName'), limit=100) or model,
                       'efforts': efforts, 'defaultEffort': default,
                       'serviceTiers': tiers, 'defaultServiceTier': default_tier,
                       'isDefault': row.get('isDefault') is True})
        seen.add(model)
    return models


def validate_options(selection, catalog):
    """Return canonical per-thread options or raise ``ValueError``.

    The caller must deny changes while a turn is busy and persist the result
    for that thread only. This function performs no configuration writes.
    ``serviceTier=None`` clears only the caller's local override; send ``default``
    to request standard speed explicitly rather than inheriting a prior tier.
    Missing effort uses
    the model's advertised default when one exists; no effort ceiling is baked
    into this validator.
    """
    if not isinstance(selection, dict) or not selection:
        raise ValueError('请选择模型或运行设置。')
    if set(selection) - {'model', 'effort', 'serviceTier'}:
        raise ValueError('包含不支持的运行设置。')
    if not isinstance(catalog, list):
        catalog = catalog_models(catalog)
    model = selection.get('model')
    selected = next((item for item in catalog if isinstance(item, dict)
                     and item.get('model') == model and _identifier(model)), None)
    if selected is None:
        raise ValueError('模型不在当前可用列表中，请刷新后重试。')
    result = {'model': model}
    effort = selection.get('effort', selected.get('defaultEffort'))
    if effort is not None:
        if effort not in selected.get('efforts', []):
            raise ValueError('该模型不支持这个推理强度，请重新选择。')
        result['effort'] = effort
    if 'serviceTier' in selection:
        tier = selection['serviceTier']
        if tier not in (None, 'default') and tier not in selected.get('serviceTiers', []):
            raise ValueError('当前模型未提供这个速度选项。')
        result['serviceTier'] = tier
    return result


def options_text(current, overrides=None):
    """Describe current versus next-turn, thread-scoped runtime settings."""
    current, overrides = _dict(current), _dict(overrides)
    def fields(value):
        model = clean(value.get('model'), limit=100) or '未知'
        effort = clean(value.get('effort', value.get('reasoning_effort')), limit=40) or '默认'
        has_tier = 'serviceTier' in value or 'service_tier' in value
        tier = value.get('serviceTier', value.get('service_tier'))
        if not has_tier:
            tier = '未知（沿用原会话）'
        elif tier is None or tier == 'default':
            tier = '标准（默认）'
        else:
            tier = clean(tier, limit=40) or '未知（沿用原会话）'
        return f'模型：{model}\n推理强度：{effort}\n速度档位：{tier}'
    text = '当前会话运行设置\n' + fields(current)
    if overrides:
        effective = dict(current)
        # Normalize legacy metadata so an explicit clear is not hidden by it.
        if 'effort' not in effective and 'reasoning_effort' in effective:
            effective['effort'] = effective['reasoning_effort']
        if 'serviceTier' not in effective and 'service_tier' in effective:
            effective['serviceTier'] = effective['service_tier']
        effective.update(overrides)
        text += '\n\n下一轮使用\n' + fields(effective)
    text += '\n\n设置仅作用于此会话的下一轮及后续手机提交，不修改全局配置。运行中请先等本轮结束。'
    return text


def _context(value):
    """Normalize allowlisted token counters; never copy raw event fields."""
    value = _dict(value)
    event_stamp = _stamp(value.get('timestamp'))
    if value.get('type') == 'event_msg':
        value = _dict(value.get('payload'))
    if value.get('method') == 'thread/tokenUsage/updated':
        value = _dict(value.get('params'))
    sampled = _stamp(value.get('sampled_at'))
    if sampled is None:
        sampled = event_stamp
    info = _dict(value.get('info'))
    if not info:
        info = _dict(value.get('tokenUsage')) or value
    last = _dict(info.get('last_token_usage')) or _dict(info.get('last'))
    total = last.get('total_tokens', last.get('totalTokens'))
    if total is None:
        total = info.get('used_tokens')
    window = info.get('model_context_window', info.get('modelContextWindow', info.get('window_tokens')))
    result = {}
    total, window = _counter(total), _counter(window)
    if total is not None:
        result['used_tokens'] = total
    if window is not None and window > 0:
        result['window_tokens'] = window
    if sampled is not None:
        result['sampled_at'] = sampled
    turn = _identifier(value.get('turnId') or value.get('turn_id') or value.get('turn'))
    if turn:
        result['turn'] = turn
    return result


def _time_text(value, tz):
    stamp = _stamp(value)
    if stamp is None:
        return '未知'
    try:
        return datetime.fromtimestamp(stamp, ZoneInfo(tz)).strftime('%m-%d %H:%M')
    except (ValueError, OverflowError, OSError, KeyError, TypeError):
        return '未知'


def _percent(value):
    value = round(float(value), 1)
    return str(int(value)) if value.is_integer() else str(value)


def _window_label(window, fallback):
    minutes = window.get('windowDurationMins', window.get('window_minutes'))
    if not _number(minutes) or minutes <= 0:
        return fallback
    if minutes % 1440 == 0:
        return f'{_percent(float(minutes / 1440))} 天窗口'
    if minutes % 60 == 0:
        return f'{_percent(float(minutes / 60))} 小时窗口'
    return f'{_percent(float(minutes))} 分钟窗口'


def usage_text(rate_limits, context=None, sampled_at=None, now=None, tz='Asia/Shanghai'):
    """Render quota/context metadata without making a model request.

    ``usedPercent`` is consumed quota. Missing values remain unknown; an expired
    reset time or an old/missing sample is shown as a previous observation.
    The multi-bucket view takes precedence over the legacy single bucket.
    Context uses the latest *last* token usage, never cumulative session usage.
    """
    now = float(now) if _number(now) else time.time()
    sampled = _stamp(sampled_at)
    fresh = sampled is not None and 0 <= now - sampled <= USAGE_FRESH_SECONDS
    data = _dict(rate_limits)
    if isinstance(data.get('result'), dict):
        data = data['result']
    buckets = data.get('rateLimitsByLimitId')
    if isinstance(buckets, dict) and buckets:
        rows = [(key, item) for key, item in buckets.items() if isinstance(item, dict)]
    else:
        legacy = data.get('rateLimits', data)
        rows = [(legacy.get('limitId', 'Codex'), legacy)] if isinstance(legacy, dict) and any(
            key in legacy for key in ('primary', 'secondary')) else []
    lines = ['额度与上下文', f'额度采样：{_time_text(sampled, tz)}']
    if not rows:
        lines.append('额度：暂时无法读取，未按 0% 处理。')
    for name, bucket in rows[:6]:
        label = clean(bucket.get('limitName') or bucket.get('limitId') or name, limit=70) or 'Codex'
        lines.append('\n' + label)
        for key, fallback in (('primary', '主窗口'), ('secondary', '次窗口')):
            window = _dict(bucket.get(key))
            if not window:
                if key == 'primary':
                    lines.append('主窗口：剩余额度未知')
                continue
            used = window.get('usedPercent', window.get('used_percent'))
            remaining = max(0, min(100, 100 - float(used))) if _number(used) else None
            resets = _stamp(window.get('resetsAt', window.get('resets_at')))
            stale = not fresh or (resets is not None and resets <= now)
            amount = ('上次剩余 ' if stale else '剩余 ') + _percent(remaining) + '%' if remaining is not None else '剩余额度未知'
            line = f'{_window_label(window, fallback)}：{amount}'
            if resets is not None:
                line += f' · 重置 {_time_text(resets, tz)}'
            else:
                line += ' · 重置时间未知'
            if resets is not None and resets <= now:
                line += '（已过重置时间，待刷新）'
            elif not fresh:
                line += '（缓存待刷新）'
            lines.append(line)
    if len(rows) > 6:
        lines.append(f'另有 {len(rows) - 6} 个额度分组未展开。')
    counters = _context(context)
    used, window = counters.get('used_tokens'), counters.get('window_tokens')
    lines.append('\n当前会话上下文')
    if used is not None and window is not None:
        percent = min(1.0, max(0.0, used / window)) * 100
        lines.append(f'最近记录：{used:,} / {window:,} tokens（占用 {_percent(percent)}%）')
    elif used is not None:
        lines.append(f'最近记录：{used:,} tokens · 上下文上限未知')
    else:
        lines.append('暂时没有可用的上下文用量记录。')
    if counters.get('sampled_at') is not None:
        lines.append(f'上下文采样：{_time_text(counters["sampled_at"], tz)}')
    lines.append('额度为账号共享；上下文来自最近用量记录，压缩后会变化。查询不会调用模型。')
    text = '\n'.join(lines)
    encoded = text.encode('utf-16-le')
    if len(encoded) > 7600:
        return encoded[:7598].decode('utf-16-le', errors='ignore') + '…'
    return text


def safe_read_snapshot(rollout_path, thread=None, max_bytes=MAX_SNAPSHOT_BYTES):
    """Read a bounded regular-file tail and return metadata-only snapshots.

    If ``thread`` is supplied, the first committed session metadata record must
    identify that exact thread. Partial trailing records and invalid JSON are
    ignored. At most 256 KiB is read for the first metadata record and 256 KiB
    for the tail. No prompt, assistant text, tool arguments, or reasoning is returned.
    """
    empty = {'thread': None, 'sampled_at': None, 'current': {}, 'context': {}, 'rate_limits': None}
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
        max_bytes = MAX_SNAPSHOT_BYTES
    max_bytes = min(MAX_SNAPSHOT_BYTES, max(1024, max_bytes))
    try:
        fd = os.open(Path(rollout_path), os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as source:
            size = os.fstat(source.fileno()).st_size
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return empty
            # Session metadata includes instructions and can exceed 16 KiB.
            # Read one bounded, committed record rather than an arbitrary prefix.
            header = source.readline(MAX_METADATA_BYTES)
            if not header.endswith(b'\n'):
                return empty
            metadata = json.loads(header)
            identity = _dict(metadata.get('payload')).get('id') if (
                isinstance(metadata, dict) and metadata.get('type') == 'session_meta') else None
            if not _identifier(identity) or (thread is not None and identity != thread):
                return empty
            start = max(0, size - max_bytes)
            source.seek(start)
            tail = source.read(max_bytes)
    except (OSError, ValueError, TypeError, OverflowError):
        return empty
    result = dict(empty)
    result['thread'] = identity if isinstance(identity, str) else None
    lines = tail.split(b'\n')
    if start:
        lines = lines[1:]
    lines = lines[:-1]  # Never consume an uncommitted trailing record.
    have_context = have_current = have_limits = False
    for line in reversed(lines):
        if not line or len(line) > max_bytes:
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        payload = _dict(event.get('payload'))
        stamp = _stamp(event.get('timestamp'))
        if event.get('type') == 'turn_context' and not have_current:
            current = {}
            settings = _dict(_dict(payload.get('collaboration_mode')).get('settings'))
            for field, value in (
                ('model', payload.get('model') or settings.get('model')),
                ('effort', payload.get('effort') or payload.get('reasoning_effort') or settings.get('reasoning_effort')),
                ('serviceTier', payload.get('service_tier', payload.get('serviceTier'))),
            ):
                if _identifier(value):
                    current[field] = value
            result['current'] = current
            have_current = True
        if event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
            continue
        if not have_context:
            context = _context(payload)
            if stamp is not None:
                context['sampled_at'] = stamp
            result['context'], result['sampled_at'] = context, stamp
            # A newer explicit unknown sample must not be filled with old data.
            have_context = True
        if not have_limits:
            # Keep quota from this exact sample; never freshen an older bucket
            # with the timestamp of a newer token-only observation.
            limits = {}
            for key in ('primary', 'secondary'):
                window = _dict(_dict(payload.get('rate_limits')).get(key))
                if window:
                    limits[key] = {field: value for field, value in window.items()
                                   if field in ('used_percent', 'usedPercent', 'window_minutes',
                                                'windowDurationMins', 'resets_at', 'resetsAt')
                                   and (_number(value) or value is None)}
            result['rate_limits'] = limits or None
            have_limits = True
        if have_context and have_current and have_limits:
            break
    return result
