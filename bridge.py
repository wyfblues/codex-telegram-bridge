#!/usr/bin/env python3
"""Personal Telegram transport. No model calls for notification delivery."""
import argparse
import fcntl
import getpass
import json
import os
import re
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from contextlib import closing, contextmanager
from zoneinfo import ZoneInfo
import image_delivery as images
import image_receive as received_images
import image_retention as retention
import progress_card as progress
import telegram_controls as controls
import telegram_render

ROOT = Path(__file__).resolve().parent
CODEX = os.environ.get('CODEX_CLI', '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex')
ACTIVE_PROCESS = None
WORKER_THREAD = None
CODEX_HOME = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
THREAD_DB = Path(os.environ.get('CODEX_THREAD_DB', str(CODEX_HOME / 'state_5.sqlite')))
os.umask(0o077)


def clock_text():
    return datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')


def thread_metadata(thread):
    try:
        with closing(sqlite3.connect(f'file:{THREAD_DB}?mode=ro', uri=True, timeout=2)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute('SELECT * FROM threads WHERE id=?', (thread,)).fetchone()
            return dict(row) if row else None
    except sqlite3.Error:
        return None


def config():
    return json.loads((ROOT / 'settings.json').read_text())


def new_thread_arguments(cfg):
    defaults = cfg.get('new_thread_defaults', {})
    args = []
    if defaults.get('model'):
        args += ['-m', defaults['model']]
    if defaults.get('model_reasoning_effort'):
        args += ['-c', 'model_reasoning_effort=' + json.dumps(defaults['model_reasoning_effort'])]
    return args


def new_thread_defaults_text(cfg):
    defaults = cfg.get('new_thread_defaults', {})
    model = defaults.get('model') or '沿用个人 Codex 配置'
    effort = defaults.get('model_reasoning_effort') or '沿用个人 Codex 配置'
    return f'新会话默认模型：{model} · 推理：{effort}'


def save_config(value):
    tmp = ROOT / 'settings.tmp'
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(ROOT / 'settings.json')


@contextmanager
def db():
    c = sqlite3.connect(ROOT / 'state.sqlite3', timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, project TEXT, prompt TEXT,
        state TEXT, thread TEXT);
      CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, text TEXT, state TEXT,
        receipt INTEGER, next_try REAL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY, offset INTEGER);
      CREATE TABLE IF NOT EXISTS completion_cursors(thread TEXT PRIMARY KEY,
        path TEXT NOT NULL, offset INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS outbox_media(id TEXT PRIMARY KEY, path TEXT NOT NULL,
        name TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL,
        size INTEGER NOT NULL, mode TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS media_tokens(token TEXT PRIMARY KEY, media_id TEXT UNIQUE NOT NULL);
      CREATE TABLE IF NOT EXISTS job_photos(job_id INTEGER PRIMARY KEY, file_id TEXT,
        path TEXT, sha256 TEXT, size INTEGER, mime TEXT, received REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS image_retention(path TEXT PRIMARY KEY, kind TEXT NOT NULL,
        last_used REAL NOT NULL, expired_at REAL);
      CREATE TABLE IF NOT EXISTS progress_cards(id TEXT PRIMARY KEY, job_id INTEGER UNIQUE,
        thread TEXT, turn TEXT, project TEXT NOT NULL, title TEXT NOT NULL,
        started REAL NOT NULL, finished REAL, state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
        event_at REAL NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
        last_version INTEGER NOT NULL DEFAULT -1, last_text TEXT NOT NULL DEFAULT '',
        last_sent REAL NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS progress_details(id TEXT PRIMARY KEY, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS control_requests(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
        thread TEXT, cwd TEXT, turn TEXT, args TEXT NOT NULL, created REAL NOT NULL,
        state TEXT NOT NULL, reply_key TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS control_results(request_id TEXT PRIMARY KEY,
        text TEXT NOT NULL, keyboard TEXT, delivered INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS runtime_options(thread TEXT PRIMARY KEY, options TEXT NOT NULL, updated REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS job_receipts(job_id INTEGER PRIMARY KEY, receipt INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS outbox_replies(id TEXT PRIMARY KEY, receipt INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS outbox_formats(id TEXT PRIMARY KEY, html TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS reply_jobs(job_id INTEGER PRIMARY KEY, thread TEXT, cwd TEXT);
      CREATE TABLE IF NOT EXISTS keyboards(id TEXT PRIMARY KEY, markup TEXT);
      CREATE TABLE IF NOT EXISTS choices(token TEXT PRIMARY KEY, project TEXT, thread TEXT,
        menu_id TEXT, expires REAL);
      CREATE TABLE IF NOT EXISTS actions(token TEXT PRIMARY KEY, kind TEXT, data TEXT,
        menu_id TEXT, expires REAL);
      CREATE TABLE IF NOT EXISTS job_diagnostics(job_id INTEGER PRIMARY KEY, code TEXT, updated REAL);
      CREATE TABLE IF NOT EXISTS message_edits(id TEXT PRIMARY KEY, target INTEGER);
      CREATE TABLE IF NOT EXISTS menu_versions(receipt INTEGER PRIMARY KEY, menu_id TEXT);
      CREATE TABLE IF NOT EXISTS execution_targets(job_id INTEGER PRIMARY KEY, cwd TEXT, args TEXT);
      CREATE TABLE IF NOT EXISTS desktop_dispatch(job_id INTEGER PRIMARY KEY,
        marker TEXT NOT NULL, phase TEXT NOT NULL, queue_id TEXT, turn_id TEXT,
        rollout_path TEXT, offset INTEGER DEFAULT 0, created REAL NOT NULL,
        submitted REAL, warned INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS process_diagnostics(job_id INTEGER PRIMARY KEY,
        exit_code INTEGER, stage TEXT, detail TEXT, updated REAL);
    ''')
    try:
        with c:
            yield c
    finally:
        c.close()


def get(c, key, default=''):
    r = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
    return r[0] if r else default


def put(c, key, value):
    c.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, str(value)))


def progress_safe(c, function, *args, **kwargs):
    """A card failure cannot roll back a task, receipt, or final answer."""
    c.execute('SAVEPOINT progress_feature')
    try:
        result = function(c, *args, **kwargs)
    except Exception:
        c.execute('ROLLBACK TO progress_feature')
        result = None
    c.execute('RELEASE progress_feature')
    return result


def progress_secrets():
    try:
        token = (ROOT / 'token').read_text().strip()
        return (token,) if token else ()
    except OSError:
        return ()


def render_progress(c, row, now=None):
    card = dict(row)
    extra = c.execute('SELECT data FROM progress_details WHERE id=?',(card['id'],)).fetchone()
    if extra:
        try:
            card.update(json.loads(extra['data']))
        except (ValueError,TypeError):
            pass
    return progress.render(card,now=now)


def progress_set(c, card_id, *, now=None, extra=None, merge_files=False, **changes):
    row = c.execute('SELECT * FROM progress_cards WHERE id=?', (card_id,)).fetchone()
    if not row or row['finished'] is not None:
        return False
    now = time.time() if now is None else now
    allowed = {'thread','turn','title','state','detail','event_at','finished'}
    changes = {key:value for key,value in changes.items() if key in allowed and row[key] != value}
    if extra:
        old = c.execute('SELECT data FROM progress_details WHERE id=?',(card_id,)).fetchone()
        info = json.loads(old['data']) if old else {}
        if merge_files and 'files' in extra:
            by_path = {item['path']:item for item in info.get('files',[]) if isinstance(item,dict) and item.get('path')}
            by_path.update({item['path']:item for item in extra['files'] if isinstance(item,dict) and item.get('path')})
            extra = dict(extra,files=list(by_path.values())[-64:])
        info.update(extra)
        encoded = json.dumps(info,ensure_ascii=False)
        if not old or encoded != old['data']:
            c.execute('INSERT OR REPLACE INTO progress_details VALUES (?,?)',(card_id,encoded))
            changes.setdefault('event_at',now)
    for key in ('title','detail'):
        if key in changes:
            changes[key] = progress.clean(changes[key], secrets=progress_secrets())
    state = changes.get('state', row['state'])
    if state in progress.TERMINAL_STATES:
        changes.setdefault('finished', now)
    if not changes:
        return False
    fields = ','.join(key+'=?' for key in changes)
    c.execute('UPDATE progress_cards SET '+fields+',version=version+1 WHERE id=?',
              (*changes.values(),card_id))
    updated = c.execute('SELECT * FROM progress_cards WHERE id=?', (card_id,)).fetchone()
    # An unsent acknowledgement can already reflect a very fast task's final state.
    c.execute("UPDATE outbox SET text=? WHERE id=? AND state='pending'",
              (render_progress(c,updated,now=now),card_id))
    return True


def progress_update_job(c, job_id, **changes):
    row = c.execute('SELECT id FROM progress_cards WHERE job_id=?', (job_id,)).fetchone()
    return progress_set(c, row['id'], **changes) if row else False


def progress_begin_job(c, cfg, job_id):
    if not cfg.get('progress_cards_enabled'):
        return None
    job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not job or job['state'] != 'queued':
        return None
    key = 'reply:' + str(job_id)
    meta = thread_metadata(job['thread']) if job['thread'] else None
    title = display_title(meta) if meta else '新会话任务'
    c.execute('''INSERT OR IGNORE INTO progress_cards
        (id,job_id,thread,project,title,started,state) VALUES (?,?,?,?,?,?,?)''',
              (key,job_id,job['thread'] or None,progress.clean(job['project'],secrets=progress_secrets()),
               progress.clean(title,secrets=progress_secrets()),time.time(),'queued'))
    row = c.execute('SELECT * FROM progress_cards WHERE id=?', (key,)).fetchone()
    queue(c,key,render_progress(c,row))
    return key


def queue_task_reply(c, cfg, job_id, text):
    if not progress_safe(c, progress_begin_job, cfg, job_id):
        queue(c,'reply:'+str(job_id),text)


def progress_worker_update(cfg, job_id, event=None, **changes):
    """Isolate the entire optional connection from the executing subprocess."""
    if not cfg.get('progress_cards_enabled'):
        return
    try:
        if event is not None:
            detail = progress.event_detail(event,secrets=progress_secrets())
            info = progress.event_info(event,secrets=progress_secrets())
            if not detail and not info:
                return
            changes.update(extra=info,state='running',merge_files=event.get('method')!='turn/diff/updated')
            if detail:
                changes['detail'] = detail
        with db() as c:
            progress_safe(c,progress_update_job,job_id,**changes)
    except Exception:
        pass  # Progress storage cannot interrupt or terminate a real task.


def progress_event(c, cfg, thread, event):
    """Only current primary turns and explicitly public commentary enter cards."""
    if not cfg.get('progress_cards_enabled'):
        return
    stamp = completion_time(event.get('timestamp'))
    if stamp is None:
        return
    stamp = stamp.timestamp()
    payload = event.get('payload',{})
    if not isinstance(payload,dict):
        return
    kind = payload.get('type') if event.get('type') == 'event_msg' else None
    detail = progress.event_detail(event,secrets=progress_secrets())
    info = progress.event_info(event,secrets=progress_secrets())
    if kind not in ('task_started','task_complete','turn_aborted') and not detail and not info:
        return
    passthrough = payload.get('internal_chat_message_metadata_passthrough') or {}
    turn = payload.get('turn_id') or (passthrough.get('turn_id') if isinstance(passthrough,dict) else None)
    if kind == 'task_started' and isinstance(turn,str):
        if stamp >= float(get(c,'progress_since',str(time.time()))) or c.execute(
                'SELECT 1 FROM progress_cards WHERE thread=? AND turn=?',(thread,turn)).fetchone():
            put(c,'progress-turn:'+thread,json.dumps({'turn':turn,'stamp':stamp}))
    if not turn and info:
        raw = get(c,'progress-turn:'+thread)
        current = json.loads(raw) if raw else {}
        if stamp >= current.get('stamp',float('inf')):
            turn = current.get('turn')
    if not isinstance(turn,str) or not turn:
        return
    row = c.execute('SELECT * FROM progress_cards WHERE thread=? AND turn=?', (thread,turn)).fetchone()
    if not row:
        pending = c.execute('''SELECT p.*,d.turn_id AS matched_turn,d.job_id AS desktop_job
            FROM progress_cards p JOIN jobs j ON j.id=p.job_id
            LEFT JOIN desktop_dispatch d ON d.job_id=j.id
            WHERE j.thread=? AND j.state IN ('queued','running') AND p.turn IS NULL
                AND p.finished IS NULL ORDER BY p.started DESC LIMIT 1''', (thread,)).fetchone()
        if pending and stamp >= pending['started'] - 1:
            if pending['desktop_job'] is not None and pending['matched_turn'] != turn:
                return  # Only the exact Desktop request marker may associate this card.
            if pending['desktop_job'] is None and not (
                    kind == 'task_started' and pending['event_at'] >= pending['started']
                    and stamp >= pending['event_at']):
                return  # A prior turn's completion/commentary cannot claim a new CLI job.
            progress_set(c,pending['id'],thread=thread,turn=turn,state='running')
            row = c.execute('SELECT * FROM progress_cards WHERE id=?', (pending['id'],)).fetchone()
        elif kind == 'task_started':
            since = float(get(c,'progress_since',str(time.time()-1)))
            if stamp < since or c.execute('SELECT 1 FROM outbox WHERE id=?',
                                         (f'notice:{thread}:{turn}',)).fetchone():
                return
            # A new CLI session may have started before its thread ID reached the worker.
            if c.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') AND (thread IS NULL OR thread='')").fetchone():
                return
            if c.execute("SELECT 1 FROM jobs WHERE thread=? AND state IN ('queued','running')", (thread,)).fetchone():
                return
            meta = thread_metadata(thread)
            if not meta or meta.get('archived') or meta['source'] not in ('vscode','cli','exec'):
                return
            project = next((name for name,cwd in cfg.get('projects',{}).items() if cwd==meta['cwd']),Path(meta['cwd']).name)
            key = f'progress:{thread}:{turn}'
            c.execute('''INSERT OR IGNORE INTO progress_cards
                (id,thread,turn,project,title,started,state,event_at) VALUES (?,?,?,?,?,?,?,?)''',
                      (key,thread,turn,progress.clean(project,secrets=progress_secrets()),
                       progress.clean(display_title(meta),secrets=progress_secrets()),stamp,'running',stamp))
            row = c.execute('SELECT * FROM progress_cards WHERE id=?', (key,)).fetchone()
            queue(c,key,render_progress(c,row))
    if not row or row['finished'] is not None or stamp < max(row['started']-1,row['event_at']):
        return
    if kind in ('task_complete','turn_aborted'):
        progress_set(c,row['id'],state='ended' if kind=='task_complete' else 'cancelled',
                     finished=max(stamp,row['started']),event_at=stamp)
    else:
        if detail or info:
            changes = {'state':'running','event_at':stamp,'extra':info,
                       'merge_files':event.get('method')!='turn/diff/updated'}
            if detail:
                changes['detail'] = detail
            progress_set(c,row['id'],**changes)


def refresh_progress_cards(cfg):
    if not cfg.get('progress_cards_enabled'):
        return
    with db() as c:
        rows = c.execute('''SELECT p.*,j.state AS job_state,d.phase AS dispatch_phase
            FROM progress_cards p JOIN jobs j ON j.id=p.job_id
            LEFT JOIN desktop_dispatch d ON d.job_id=j.id WHERE p.finished IS NULL''').fetchall()
        for row in rows:
            state = {'turn-ended':'ended','failed':'failed','cancelled':'cancelled',
                     'interrupted':'interrupted'}.get(row['job_state'])
            if state:
                progress_safe(c,progress_set,row['id'],state=state)
        now = time.time()
        if now - float(get(c,'progress_probe','0')) < 30:
            return
        put(c,'progress_probe',now)
        generic = [dict(r) for r in c.execute('''SELECT * FROM progress_cards
            WHERE job_id IS NULL AND finished IS NULL AND started<?''',(now-30,))]
    if not generic:
        return
    try:
        paths = open_rollout_paths()
    except (OSError,subprocess.SubprocessError):
        return  # Unavailable process metadata does not prove an interruption.
    for row in generic:
        meta = thread_metadata(row['thread'])
        event = find_task_event(meta,('task_started','task_complete','turn_aborted')) if meta else None
        if not event:
            continue
        with db() as c:
            if event['payload'].get('turn_id') != row['turn']:
                stamp = completion_time(event.get('timestamp'))
                if stamp and stamp.timestamp() > max(row['started'],row['event_at']):
                    progress_safe(c,progress_set,row['id'],state='interrupted',
                                  detail='同会话已进入后续回合，未确认本回合最终结果；停止刷新且不自动重跑。')
                continue
            if event['payload']['type'] in ('task_complete','turn_aborted'):
                progress_safe(c,progress_event,cfg,row['thread'],event)
            elif meta['rollout_path'] not in paths and thread_writer_busy(row['thread']) is False:
                progress_safe(c,progress_set,row['id'],state='interrupted',
                              detail='本回合执行进程已退出，未确认最终结果；不会自动重跑。')


def deliver_progress(api, cfg, now=None):
    """Coalesce updates into one known message; never send a replacement card."""
    if not cfg.get('progress_cards_enabled'):
        return False
    now = time.time() if now is None else now
    with db() as c:
        rows = c.execute('''SELECT p.*,o.receipt FROM progress_cards p JOIN outbox o ON o.id=p.id
            WHERE p.disabled=0 AND o.state='sent' AND o.receipt IS NOT NULL
                AND (p.finished IS NULL OR p.version>p.last_version)
                AND p.next_try<=? AND p.last_sent<=? ORDER BY p.last_sent,p.started''',
                         (now,now-progress.CARD_INTERVAL)).fetchall()
        selected = None
        for row in rows:
            text = render_progress(c,row,now=now)
            if text != row['last_text']:
                selected = row
                break
            if row['version'] > row['last_version']:
                c.execute('UPDATE progress_cards SET last_version=? WHERE id=?',
                          (row['version'],row['id']))
        if selected is None:
            return False
        card_id, version = selected['id'],selected['version']
    disabled, retry = False, now + progress.CARD_INTERVAL
    acknowledged = False
    try:
        api.call('editMessageText',chat_id=cfg['chat_id'],message_id=selected['receipt'],text=text)
        acknowledged = True
    except Rejected as error:
        if error.not_modified:
            acknowledged = True
        elif error.code == 429:
            retry = now + max(error.retry,progress.CARD_INTERVAL)
        else:
            disabled = True
    except Exception:
        pass  # Retrying an edit addresses the same message, never creates a duplicate.
    with db() as c:
        if acknowledged:
            c.execute('''UPDATE progress_cards SET last_text=?,last_version=?,last_sent=?,next_try=0
                WHERE id=?''',(text,version,now,card_id))
        else:
            c.execute('UPDATE progress_cards SET disabled=?,next_try=? WHERE id=?',
                      (int(disabled),retry,card_id))
    return True


def message_parts(text, limit=3400):
    """Conservative UTF-16 budget; keep every character, including astral emoji."""
    parts, chars, size = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            parts.append(''.join(chars))
            chars, size = [], 0
        chars.append(char)
        size += width
    if chars:
        parts.append(''.join(chars))
    return parts or ['（无文本内容）']


def image_config():
    try:
        return config().get('image_delivery_enabled') is True
    except (OSError, ValueError):
        return False


def track_image(c, path, kind, used_at=None):
    """Track only our own copies; fresh references renew a shared content hash."""
    info = retention.inspect(ROOT, path, kind)
    if not info['exists']:
        return
    used_at = time.time() if used_at is None else used_at
    c.execute('''INSERT INTO image_retention VALUES (?,?,?,NULL)
                 ON CONFLICT(path) DO UPDATE SET kind=excluded.kind,
                 last_used=MAX(image_retention.last_used,excluded.last_used),expired_at=NULL''',
              (str(path), kind, max(used_at, info['mtime'])))


def original_expired(c, media):
    row = c.execute('SELECT * FROM image_retention WHERE path=?', (media['path'],)).fetchone()
    try:
        info = retention.inspect(ROOT, media['path'], 'media')
    except retention.RetentionError:
        return True
    if not info['exists'] or (row and row['expired_at'] is not None):
        return True
    last_used = max(row['last_used'] if row else info['mtime'], info['mtime'])
    return time.time() - last_used >= retention.RETENTION_DAYS['media'] * retention.DAY


def cleanup_images(cfg, now=None):
    """One serialized local maintenance pass; errors never stop message handling."""
    if not cfg.get('image_cleanup_enabled'):
        return None
    now = time.time() if now is None else now
    active, unknown = running_threads()
    if unknown:
        raise OSError('Cannot establish active image consumers')
    active_ids = {meta['id'] for meta in active}
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        active_ids.update(row[0] for row in c.execute("""SELECT thread FROM jobs
            WHERE state IN ('queued','running') AND thread IS NOT NULL"""))
        protected = {row[0] for row in c.execute('''SELECT p.path FROM job_photos p
            JOIN jobs j ON j.id=p.job_id WHERE j.state IN ('queued','running') AND p.path IS NOT NULL''')}
        protected.update(row[0] for row in c.execute('''SELECT m.path FROM outbox_media m
            JOIN outbox o ON o.id=m.id WHERE o.state IN ('pending','sending','uncertain')'''))
        for row in c.execute('SELECT p.path,j.thread FROM job_photos p JOIN jobs j ON j.id=p.job_id'):
            if row['thread'] in active_ids and row['path']:
                protected.add(row['path'])
        for row in c.execute('SELECT id,path FROM outbox_media'):
            parts = row['id'].split(':')
            if len(parts) >= 3 and parts[0] in ('notice','last','result-backfill') and parts[1] in active_ids:
                protected.add(row['path'])
        # Backfill legacy/orphan snapshots; discovery never leaves the private cache directories.
        for copy in retention.discover(ROOT):
            c.execute('''INSERT INTO image_retention VALUES (?,?,?,NULL)
                ON CONFLICT(path) DO UPDATE SET last_used=MAX(image_retention.last_used,excluded.last_used),
                expired_at=CASE WHEN excluded.last_used>COALESCE(image_retention.expired_at,excluded.last_used)
                    THEN NULL ELSE image_retention.expired_at END''',
                      (copy['path'],copy['kind'],copy['last_used']))
        candidates = [dict(row) for row in c.execute('SELECT * FROM image_retention WHERE expired_at IS NULL')]
        result = retention.clean(ROOT, candidates, protected, now)
        for path in result['expired']:
            c.execute('UPDATE image_retention SET expired_at=? WHERE path=?', (now,path))
        put(c,'cleanup:last',now)
        put(c,'cleanup:deleted',len(result['deleted']))
        put(c,'cleanup:bytes',result['bytes'])
        put(c,'cleanup:errors',len(result['errors']))
    return result


def queue_media(c, key, media, mode, caption):
    if c.execute('SELECT 1 FROM outbox WHERE id=?', (key,)).fetchone():
        return
    track_image(c, media['path'], 'media')
    markup = None
    if mode == 'photo':
        token = secrets.token_urlsafe(18)
        c.execute('INSERT INTO media_tokens VALUES (?,?)', (token, key))
        markup = {'inline_keyboard': [[{'text': '发送原图', 'callback_data': 'original:' + token}]]}
    queue(c, key, caption[:450], markup)
    c.execute('INSERT INTO outbox_media VALUES (?,?,?,?,?,?,?)',
              (key, media['path'], media['name'], media['mime'], media['sha256'], media['size'], mode))


def queue_answer_images(c, key, result, caption, completed=None):
    if not image_config():
        return
    if not c.in_transaction:
        c.execute('BEGIN IMMEDIATE')
    since = float(get(c, 'image_delivery_since', '0'))
    if not since:
        since = time.time() - 1
        put(c, 'image_delivery_since', since)
    if completed is not None and completed.timestamp() < since:
        return
    delivered = set()
    references = list(images.image_references(result))
    for index, (source, requested_mode) in enumerate(references[:images.IMAGE_LIMIT]):
        try:
            media = images.snapshot_image(ROOT, source)
            mode = requested_mode if media['preview'] else 'document'
            identity = (media['sha256'], mode)
            if identity in delivered:
                continue
            delivered.add(identity)
            media_key = key + ':media:' + media['sha256'] + ':' + mode
            label = '图片预览' if mode == 'photo' else '原图文件'
            queue_media(c, media_key, media, mode, f'{caption}\n{label} · {media["name"]}')
        except (images.ImageError, retention.RetentionError) as e:
            detail = str(e) if isinstance(e, images.ImageError) else '图片副本无法安全读取。'
            queue(c, key + ':media-error:' + str(index), '图片未发送：' + detail)
    if len(references) > images.IMAGE_LIMIT:
        queue(c, key + ':media-limit', f'本轮图片超过 {images.IMAGE_LIMIT} 张，其余图片请分批交付。')


def original_callback(c, callback, token):
    stored = c.execute('SELECT media_id FROM media_tokens WHERE token=?', (token,)).fetchone()
    if not stored or not valid_menu(c, stored['media_id'], callback['message'].get('message_id')):
        return '无法验证这张图片，请使用已发送图片下的按钮。'
    media = c.execute('SELECT * FROM outbox_media WHERE id=?', (stored['media_id'],)).fetchone()
    if not media:
        return '原图记录不存在，请在原会话重新交付。'
    key = stored['media_id'].replace(':media:', ':original:', 1)
    previous = c.execute('SELECT state FROM outbox WHERE id=?', (key,)).fetchone()
    if previous and previous['state'] in ('pending','sending','uncertain'):
        return ('原图送达状态待确认；为避免重复发送，请先检查聊天记录。'
                if previous['state'] == 'uncertain' else '原图正在排队或发送，请稍候。')
    if original_expired(c, media):
        return '原图本机副本已过期或已清理（保留 30 天），请在原会话重新提供图片。已发送的原图可在聊天记录中查看。'
    if previous:
        if previous['state'] == 'sent':
            return '原图已发送，请在聊天记录中查看；重复点击不会重发。'
        if previous['state'] == 'failed':
            return '原图发送失败，请在原会话重新交付图片。'
        return '原图正在排队或发送，请稍候。'
    queue_media(c, key, media, 'document', '原图文件 · ' + media['name'])
    return '原图已入队；同一张图片重复点击不会重复发送。'


def queue(c, key, text, markup=None):
    # Never forward the bridge credential even if it appears in a final answer.
    try:
        token = (ROOT / 'token').read_text().strip()
        if token:
            text = text.replace(token, '[Bot Token 已隐藏]')
    except OSError:
        pass
    try:
        rich = config().get('rich_messages_enabled') and str(key).split(':')[0] in ('notice','last','result-backfill')
    except (OSError,ValueError):
        rich = False
    try:
        formatted = telegram_render.render_parts(text,limit=3300) if rich else None
    except Exception:
        formatted = None  # Optional formatting must not lose a final answer.
    parts = [part['text'] for part in formatted] if formatted else message_parts(text)
    for index, part in enumerate(parts):
        message_id = str(key) if index == 0 else f'{key}:part:{index + 1}'
        if len(parts) > 1:
            part = f'【{index + 1}/{len(parts)}】\n' + part
        inserted = c.execute('INSERT OR IGNORE INTO outbox(id,text,state) VALUES (?,?,?)',
                             (message_id, part, 'pending')).rowcount
        # The first row is the idempotency marker for this complete logical notice.
        if index == 0 and not inserted:
            return
        if formatted:
            html = formatted[index]['html']
            if len(parts) > 1:
                html = f'【{index + 1}/{len(parts)}】\n' + html
            c.execute('INSERT OR REPLACE INTO outbox_formats VALUES (?,?)',(message_id,html))
        if markup and index == len(parts) - 1:
            c.execute('INSERT OR REPLACE INTO keyboards VALUES (?,?)',
                      (message_id, json.dumps(markup, ensure_ascii=False)))
            if message_id != str(key):
                for table in ('actions','choices'):
                    c.execute(f'UPDATE {table} SET menu_id=? WHERE menu_id=?',(message_id,str(key)))


def edit_menu(c, key, target):
    # Multi-part status/results retain complete delivery; only compact panels are edited.
    if c.execute('SELECT 1 FROM outbox WHERE id=?',(str(key)+':part:2',)).fetchone():
        return
    c.execute('INSERT OR REPLACE INTO message_edits VALUES (?,?)',(key,target))
    c.execute('INSERT OR REPLACE INTO menu_versions VALUES (?,?)',(target,'pending:'+key))


def valid_menu(c, menu_id, receipt):
    row=c.execute('SELECT menu_id FROM menu_versions WHERE receipt=?',(receipt,)).fetchone()
    if row and row[0] != menu_id:
        return False
    return bool(c.execute("SELECT 1 FROM outbox WHERE id=? AND receipt=? AND state='sent'",
                          (menu_id,receipt)).fetchone())


class Rejected(Exception):
    def __init__(self, code, retry=0, not_modified=False):
        self.code, self.retry = code, retry
        self.not_modified=not_modified


class API:
    def __init__(self, cfg):
        token = (ROOT / 'token').read_text().strip()
        self.base = 'https://api.telegram.org/bot' + token + '/'
        self.file_base = 'https://api.telegram.org/file/bot' + token + '/'
        proxy = cfg.get('proxy')
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler(
            {'https': proxy} if proxy else {}))

    def call(self, method, **payload):
        request = urllib.request.Request(self.base + method,
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        return self.request(request)

    def upload(self, method, field, data, filename, mime, **payload):
        boundary = 'codex-' + secrets.token_hex(24)
        parts = []
        for key, value in payload.items():
            encoded = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                          + encoded + '\r\n').encode())
        filename = re.sub(r'[\x00-\x1f\x7f"\\]', '_', filename)
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                      f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode())
        parts.extend([data, f'\r\n--{boundary}--\r\n'.encode()])
        request = urllib.request.Request(self.base + method, data=b''.join(parts),
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        return self.request(request, timeout=120)

    def request(self, request, timeout=40):
        try:
            with self.http.open(request, timeout=timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as e:
            try:
                data = json.load(e)
            except Exception:
                raise Rejected(e.code) from None
        if not data.get('ok'):
            raise Rejected(data.get('error_code'), data.get('parameters', {}).get('retry_after', 0),
                           'message is not modified' in data.get('description','').lower())
        return data['result']

    def download_photo(self, file_id):
        info = self.call('getFile', file_id=file_id)
        path = info.get('file_path', '')
        if (not isinstance(path, str) or not path or path.startswith('/')
                or any(part in ('.', '..', '') for part in path.split('/'))
                or re.fullmatch(r'[A-Za-z0-9_./-]+', path) is None):
            raise received_images.PhotoError('图片下载地址无效，请重新发送。')
        if info.get('file_size', 0) > received_images.PHOTO_LIMIT:
            raise received_images.PhotoError('图片超过 20 MB，请缩小后再发送。')
        request = urllib.request.Request(self.file_base + path)
        with self.http.open(request, timeout=40) as response:
            length = response.headers.get('Content-Length')
            if length and int(length) > received_images.PHOTO_LIMIT:
                raise received_images.PhotoError('图片超过 20 MB，请缩小后再发送。')
            data = response.read(received_images.PHOTO_LIMIT + 1)
        if len(data) > received_images.PHOTO_LIMIT:
            raise received_images.PhotoError('图片超过 20 MB，请缩小后再发送。')
        return data


def authorized(cfg, message):
    return (message.get('chat', {}).get('type') == 'private'
        and message.get('chat', {}).get('id') == cfg.get('chat_id')
        and message.get('from', {}).get('id') == cfg.get('user_id')
        and not message.get('from', {}).get('is_bot', True))


HELP = ('/projects 点选项目，可加关键词搜索\n'
        '/sessions 查看会话，可翻页或加关键词搜索\n/current 查看当前消息的目标会话\n'
        '/last 补看当前会话最近一次结果\n/new 下一条消息新建会话\n'
        '/status 查看本机正在运行的任务\n/cancel 取消待提交任务或停止 CLI 任务\n'
        '/health 查看机器人连接和通知状态\n'
        '/steer 补充要求：追加到原会话当前回合\n/stop 停止原会话当前回合\n'
        '/settings 调整原会话模型、推理和速度\n/usage 查看额度和上下文\n'
        '回复完成通知会定位原任务；桌面会话交给 Codex 桌面端继续。\n'
        '不回复通知时，选定项目后直接发文字即可执行。只接收绑定用户的私聊。')


def display_title(meta, limit=140):
    title = meta.get('name') or meta.get('title') or '未命名会话'
    if not meta.get('name') and title.startswith('Execution context:'):
        title = 'Telegram 会话'
    title = ' '.join(title.split())
    return title[:limit] + ('…' if len(title) > limit else '')


def recent_threads(cwd, offset=0, query='', limit=5):
    with closing(sqlite3.connect(f'file:{THREAD_DB}?mode=ro', uri=True, timeout=2)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""SELECT * FROM threads WHERE cwd=? AND archived=0
            AND source IN ('vscode','exec','cli')
            AND (?='' OR instr(lower(COALESCE(name,'') || ' ' || title), lower(?))>0)
            ORDER BY COALESCE(NULLIF(recency_at_ms,0),NULLIF(updated_at_ms,0),updated_at*1000) DESC,
            id DESC LIMIT ? OFFSET ?""", (cwd, query, query, limit, offset)).fetchall()
        return [dict(r) for r in rows]


def action_button(c, menu_id, label, kind, **data):
    token = secrets.token_urlsafe(12)
    c.execute('DELETE FROM actions WHERE expires<?', (time.time(),))
    c.execute('INSERT INTO actions VALUES (?,?,?,?,?)',
              (token, kind, json.dumps(data), menu_id, time.time()+1800))
    return {'text': label, 'callback_data': 'action:' + token}


def choose_project(c, project):
    if get(c,'project') != project:
        put(c,'await-session:'+project,'1')
    put(c,'project',project)
    put(c,'project-used:'+project,time.time())


def project_menu(c, cfg, message_key, page=0, query=''):
    names = [name for name in cfg.get('projects', {}) if query.casefold() in name.casefold()]
    names.sort(key=lambda name: -float(get(c,'project-used:'+name,'0')))
    pages = max(1, (len(names)+7)//8)
    page = max(0, min(page, pages-1))
    current = get(c, 'project')
    lines = [f'选择项目（第 {page+1}/{pages} 页，共 {len(names)} 个）']
    if current:
        lines.append('当前项目：' + current)
    if query:
        lines.append('搜索：' + query)
    buttons = []
    for name in names[page*8:page*8+8]:
        buttons.append([action_button(c,message_key,('✓ ' if name == current else '')+name,
                                     'project',project=name)])
    nav = []
    if page:
        nav.append(action_button(c,message_key,'上一页','projects',page=page-1,query=query))
    if page+1 < pages:
        nav.append(action_button(c,message_key,'下一页','projects',page=page+1,query=query))
    if nav:
        buttons.append(nav)
    if query:
        buttons.append([action_button(c,message_key,'清除搜索','projects',page=0,query='')])
    lines.append('点项目后选择会话；也可以发送 /projects 关键词。' if names else '没有匹配的项目，请换个关键词。')
    queue(c,message_key,'\n'.join(lines),{'inline_keyboard':buttons} if buttons else None)


def session_menu(c, cfg, project, message_key, page=0, query=''):
    try:
        rows = recent_threads(cfg['projects'][project],page*5,query,6)
    except (sqlite3.Error, KeyError):
        queue(c, message_key, '暂时无法读取会话列表，请稍后发送 /sessions 重试。')
        return
    c.execute('DELETE FROM choices WHERE expires<?', (time.time(),))
    more, rows = len(rows)>5, rows[:5]
    lines = [f'项目：{project}', f'会话列表 · 第 {page+1} 页（最近使用的在前，每页 5 条）']
    if query:
        lines.append('搜索：' + query)
    if not rows:
        lines.append('没有匹配的会话。' if query else '本页没有会话。')
    buttons = []
    for index, row in enumerate(rows, 1):
        title = display_title(row)
        shown = title
        stamp = datetime.fromtimestamp(row['updated_at'], ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
        selected = row['id'] == (get(c,'selected:'+project) or get(c,'thread:'+project))
        lines.append(f"\n{index}. {'✓ ' if selected else ''}{shown}\n   {stamp} · {row['id'][:8]}")
        token = secrets.token_urlsafe(12)
        c.execute('INSERT INTO choices VALUES (?,?,?,?,?)',
                  (token, project, row['id'], message_key, time.time() + 1800))
        buttons.append([{'text': f'{index}. ' + title[:42], 'callback_data': 'session:' + token}])
    nav = []
    if page:
        nav.append(action_button(c,message_key,'上一页','sessions',project=project,page=page-1,query=query))
    if more:
        nav.append(action_button(c,message_key,'下一页','sessions',project=project,page=page+1,query=query))
    if nav:
        buttons.append(nav)
    if query:
        buttons.append([action_button(c,message_key,'清除搜索','sessions',project=project,page=0,query='')])
    buttons.append([action_button(c,message_key,'返回项目','projects',page=0,query='')])
    token = secrets.token_urlsafe(12)
    c.execute('INSERT INTO choices VALUES (?,?,?,?,?)', (token, project, '', message_key, time.time() + 1800))
    buttons.append([{'text': '＋ 新建会话', 'callback_data': 'session:' + token}])
    lines.append('\n浏览不改变当前选择。点会话按钮才切换；/sessions 关键词 可搜索。')
    queue(c, message_key, '\n'.join(lines), {'inline_keyboard': buttons})


def select_callback(c, cfg, callback):
    message = callback.get('message', {})
    if (not authorized(cfg, {'from': callback.get('from', {}), 'chat': message.get('chat', {})})
            or message.get('from', {}).get('id') != cfg.get('bot_id')):
        return '无权操作。'
    data = callback.get('data', '')
    if data.startswith('original:'):
        return original_callback(c, callback, data[9:])
    if data.startswith('action:'):
        return navigation_callback(c,cfg,callback,data[7:])
    token = data[8:] if data.startswith('session:') else ''
    choice = c.execute('SELECT * FROM choices WHERE token=?', (token,)).fetchone()
    if not choice or choice['expires'] < time.time():
        return '列表已过期，请发送 /sessions 刷新。'
    project = choice['project']
    if get(c, 'project') != project or project not in cfg['projects']:
        return '项目已切换，请在当前项目发送 /sessions。'
    sent = valid_menu(c,choice['menu_id'],message.get('message_id'))
    if not sent:
        return '无法验证这个选择按钮，请发送 /sessions 刷新。'
    if choice['thread']:
        meta = thread_metadata(choice['thread'])
        if (not meta or meta.get('archived') or meta['cwd'] != cfg['projects'][project]
                or meta['source'] not in ('vscode','exec','cli')):
            return '该会话已不可用，请发送 /sessions 刷新。'
        put(c, 'selected:' + project, meta['id'])
        title = display_title(meta)
        answer = f'已选择会话：{title[:200]}\n现在直接发消息，就会继续这条会话。'
    else:
        if c.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running')").fetchone():
            return '请等机器人当前任务结束后再新建会话。'
        put(c, 'selected:' + project, '')
        put(c, 'thread:' + project, '')
        answer = '已选择新建会话，直接发送任务即可。\n' + new_thread_defaults_text(cfg)
    put(c, 'await-session:' + project, '0')
    queue(c, 'selection:' + callback['id'], answer)
    return '已选择。'


def navigation_callback(c,cfg,callback,token):
    action = c.execute('SELECT * FROM actions WHERE token=?',(token,)).fetchone()
    if not action or action['expires'] < time.time():
        return '按钮已过期，请重新发送 /projects 或 /sessions。'
    if not valid_menu(c,action['menu_id'],callback['message'].get('message_id')):
        return '菜单已更新或正在更新，请使用最新按钮。'
    data = json.loads(action['data'])
    if action['kind'] == 'runtime-control':
        return controls.callback(globals(),c,cfg,callback,action)
    key = 'navigation:' + callback['id']
    if action['kind'] == 'projects':
        project_menu(c,cfg,key,data['page'],data['query'])
    elif action['kind'] == 'project':
        project = data['project']
        if project not in cfg.get('projects',{}):
            return '项目已不可用，请发送 /projects 刷新。'
        choose_project(c,project)
        session_menu(c,cfg,project,key)
    elif action['kind'] == 'sessions':
        project = data['project']
        if get(c,'project') != project or project not in cfg.get('projects',{}):
            return '项目已切换，请在当前项目发送 /sessions。'
        session_menu(c,cfg,project,key,data['page'],data['query'])
    elif action['kind'] == 'status':
        status_menu(c,cfg,key)
    elif action['kind'] in ('detail','select-thread','last-thread'):
        meta=thread_metadata(data['thread'])
        if (not meta or meta.get('archived') or meta['cwd'] != data['cwd']
                or meta['source'] not in ('vscode','exec','cli')):
            return '这条会话已不可用，请刷新任务列表。'
        if action['kind']=='select-thread':
            project=data['project']
            if cfg.get('projects',{}).get(project) != meta['cwd']:
                return '项目目录已变化，请重新选择。'
            choose_project(c,project)
            put(c,'selected:'+project,meta['id'])
            put(c,'await-session:'+project,'0')
        if action['kind']=='last-thread':
            queue_last_result(c,meta,'button-'+callback['id'])
            return '已读取最近一次结果。'
        thread_panel(c,cfg,meta,key)
    elif action['kind']=='cancel-job':
        queue(c,key,cancel_job(c,data['job']))
        return '已处理停止请求。'
    else:
        return '未知按钮，请重新打开菜单。'
    edit_menu(c,key,callback['message']['message_id'])
    return '列表已更新。'


def current_target(c,cfg):
    project = get(c,'project')
    if project not in cfg.get('projects',{}):
        return None, '尚未选择项目，请发送 /projects 点选。'
    if get(c,'await-session:'+project) == '1':
        return None, f'项目：{project}\n正在等待选择会话，请点击列表按钮或发送 /new。'
    thread = get(c,'selected:'+project) or get(c,'thread:'+project)
    if not thread:
        job = c.execute("""SELECT j.thread FROM jobs j LEFT JOIN reply_jobs r ON r.job_id=j.id
            WHERE j.project=? AND j.state IN ('queued','running') AND r.job_id IS NULL
            ORDER BY j.id LIMIT 1""",(project,)).fetchone()
        if job:
            thread = job['thread']
            if not thread:
                return None, f'项目：{project}\n机器人任务正在等待启动，新会话尚未就绪。'
    if not thread:
        return None, f'项目：{project}\n新建会话模式：下一条任务会创建新会话。\n' + new_thread_defaults_text(cfg)
    meta = thread_metadata(thread)
    if (not meta or meta.get('archived') or meta['cwd'] != cfg['projects'][project]
            or meta['source'] not in ('vscode','exec','cli')):
        return None, '当前会话已不可用，请发送 /sessions 重新选择。'
    return meta, ''


def current_text(c,cfg):
    meta, message = current_target(c,cfg)
    if not meta:
        return message
    state = STATE_LABELS[thread_activity(meta)]
    continuation = ('直接发送文字将交给 Codex 桌面端继续这条原会话。'
                    if meta['source'] == 'vscode' else
                    ('当前会话仍被其他进程持有，暂时不能续接；可用 /last 查看结果。'
                     if thread_writer_busy(meta['id']) else '直接发送文字将继续这条会话。'))
    return (f"当前项目：{get(c,'project')}\n会话：{display_title(meta)}\n"
            f"会话 ID：{meta['id'][:8]} · {state}\n"
            f"模型：{meta.get('model') or '未知'} · 推理：{meta.get('reasoning_effort') or '默认'}\n"
            + continuation + '回复完成通知时，以被回复的会话为准。')


def last_result(c,cfg,message,uid):
    if 'reply_to_message' in message or 'external_reply' in message:
        meta = resolve_reply(c,cfg,message)
        error = '这条消息没有关联到可读取的原会话，请回复任务完成通知。'
    else:
        meta, error = current_target(c,cfg)
    if not meta:
        queue(c,'reply:'+str(uid),error)
        return
    queue_last_result(c,meta,uid)


def queue_last_result(c,meta,uid):
    event = find_task_event(meta,('task_complete',))
    if not event:
        queue(c,'reply:'+str(uid),'暂未找到可读取的已结束回合，请等任务结束或在本机查看。')
        return
    result = event['payload'].get('last_agent_message')
    if not isinstance(result,str) or not result.strip():
        result = '（该回合没有最终答复文本）'
    stamp = event.get('timestamp','')
    try:
        stamp = datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(
            ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
    except (ValueError,TypeError,AttributeError):
        stamp = '时间未知'
    queue(c,f"last:{meta['id']}:{uid}",
          f"【最近一次结果】{display_title(meta)}\n会话 {meta['id'][:8]} · {stamp}\n\n{result}")


def health_text(c,cfg):
    def age(key):
        value = get(c,key)
        return f'{max(0,int(time.time()-float(value)))} 秒前' if value else '尚未完成'
    lines = ['【机器人健康状态】', '服务在线，已绑定私人账号。',
             '远程执行：'+('已启用' if cfg.get('execution_enabled') else '已停用'),
             '图片发送：'+('已启用' if cfg.get('image_delivery_enabled') else '已停用'),
             '图片接收：'+('已启用（单张图片＋说明）' if cfg.get('image_receive_enabled') else '已停用'),
             '图片清理：'+('已启用（接收 7 天／原图 30 天，每小时检查）' if cfg.get('image_cleanup_enabled') else '已停用'),
             '执行进度卡片：'+('已启用（同一条消息，每 10 秒最多更新一次）' if cfg.get('progress_cards_enabled') else '已停用'),
             '手机控制：'+('已启用（追加、停止、运行设置、额度）' if controls.enabled(cfg) else '已停用'),
             '上次成功接收检查：'+age('health:poll'),
             '上次成功扫描任务：'+age('health:observer'),
             '上次发送检查：'+age('health:sender'),
             '执行线程心跳：'+age('health:worker')]
    for key,label in [('poll','Telegram 接收'),('observer','任务通知扫描'),
                      ('sender','消息发送'),('worker','任务执行')]:
        if get(c,'health:'+key+':error'):
            lines.append(label+'最近遇到异常，正在重试。')
        elif get(c,'health:'+key) and time.time()-float(get(c,'health:'+key))>90:
            lines.append(label+'超过 90 秒未更新，状态待确认。')
    counts = dict(c.execute('SELECT state,count(*) FROM outbox GROUP BY state').fetchall())
    lines.append(f"通知：待发送 {counts.get('pending',0)}，发送中 {counts.get('sending',0)}，"
                 f"失败 {counts.get('failed',0)}，送达待确认 {counts.get('uncertain',0)}")
    if counts.get('failed') or counts.get('uncertain'):
        lines.append('可选中会话后用 /last 补看结果。')
    if get(c,'cleanup:last'):
        lines.append('上次图片清理：'+age('cleanup:last')+'，删除 '+get(c,'cleanup:deleted','0')+' 个副本。')
    if get(c,'cleanup:errors','0') != '0' or get(c,'health:cleanup:error'):
        lines.append('图片清理最近遇到异常，收发服务继续运行，下次定时检查。')
    lines.append('电脑睡眠或离线时无法接收命令；超过 15 分钟的指令不会自动执行。')
    return '\n'.join(lines)


def cancel_job(c, job_id=None):
    job = c.execute("""SELECT id,project,state FROM jobs WHERE state IN ('queued','running')
        AND (? IS NULL OR id=?) ORDER BY id LIMIT 1""",(job_id,job_id)).fetchone()
    if not job:
        return '机器人当前没有可停止的任务；/status 可查看桌面端其他任务。'
    desktop = c.execute('SELECT phase FROM desktop_dispatch WHERE job_id=?',(job['id'],)).fetchone()
    if desktop and desktop['phase'] != 'prepared':
        return ('这条指令已交给 Codex 桌面端，Bot 暂不支持远程停止桌面任务。'
                '请在桌面原会话点击停止；本次没有宣称已取消。')
    put(c,'cancel-job',job['id'])
    if job['state'] == 'queued':
        c.execute("UPDATE jobs SET state='cancelled',prompt='' WHERE id=?",(job['id'],))
        progress_safe(c,progress_update_job,job['id'],state='cancelled')
        return f"已取消尚未启动的任务：{job['project']}。"
    progress_safe(c,progress_update_job,job['id'],state='waiting',detail='已请求停止，等待执行进程确认。')
    return f"已请求停止任务：{job['project']}。停止后会收到结果通知。"


def enqueue_existing(c, cfg, uid, text, target, project=None):
    if not target or target.get('archived') or target['source'] not in ('vscode','cli','exec'):
        return '找不到可继续的原会话，请重新选择；本次没有执行。'
    if c.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running')").fetchone():
        return '机器人已有任务在执行，请等它结束后再发送。'
    if not Path(target['cwd']).is_dir():
        return '原任务的目录已不存在，本次没有执行。'
    if thread_activity(target) not in ('idle','interrupted'):
        return '原任务仍在运行，或暂时无法确认已结束。请等它结束后再发送；本次没有重复启动。'
    writer = thread_writer_busy(target['id'])
    if target['source'] != 'vscode' and writer is not False:
        return FAILURES['WRITER_BUSY' if writer else 'TARGET_UNAVAILABLE'] + ' 本次没有执行。'
    try:
        arguments = continuation_snapshot(target)
    except (ValueError, KeyError, TypeError):
        return '暂时无法完整保留原任务的执行设置，请在 Codex 中继续这条任务；本次没有执行。'
    if target['source'] == 'vscode' and not desktop_available():
        return FAILURES['DESKTOP_OFFLINE'] + ' 本次没有执行。'
    project = project or next((k for k,v in cfg.get('projects', {}).items() if v == target['cwd']), Path(target['cwd']).name)
    c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', (uid, project, text, 'queued', target['id']))
    c.execute('INSERT INTO reply_jobs VALUES (?,?,?)', (uid, target['id'], target['cwd']))
    c.execute('INSERT INTO execution_targets VALUES (?,?,?)',
              (uid,target['cwd'],json.dumps(arguments)))
    if target['source'] == 'vscode':
        c.execute('INSERT INTO desktop_dispatch(job_id,marker,phase,created) VALUES (?,?,?,?)',
                  (uid, 'telegram-request-' + secrets.token_hex(16), 'prepared', time.time()))
        return f"已接收，将交给桌面原任务：{project} · {target['id'][:8]}。完成后会发送结果。"
    return f"已接收，将继续原任务：{project} · {target['id'][:8]}。"


def find_task_event(meta, kinds):
    """Read only lifecycle metadata; never return prompts, reasoning or tool output."""
    try:
        with open(meta['rollout_path'], 'rb') as f:
            size = os.fstat(f.fileno()).st_size
            budget = 64 * 1024
            while True:
                start = max(0, size - budget)
                f.seek(start)
                if start:
                    f.readline()  # Ignore a partial leading JSON record.
                data = f.read()
                lines = data.splitlines()
                if data and not data.endswith(b'\n'):
                    lines = lines[:-1]  # The writer has not committed this final record yet.
                for line in reversed(lines):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict) or event.get('type') != 'event_msg':
                        continue
                    payload = event.get('payload', {})
                    kind = payload.get('type') if isinstance(payload, dict) else None
                    if kind in kinds:
                        return event
                if start == 0 or budget >= 64 * 1024 * 1024:
                    return None
                budget *= 2
    except OSError:
        return None


def latest_task_event(meta):
    event = find_task_event(meta,('task_started','task_complete','turn_aborted'))
    return (event['payload']['type'],event.get('timestamp')) if event else (None,None)


STATE_LABELS = {'active':'运行中','idle':'本轮已结束','interrupted':'异常中断，可发送新指令继续',
                'unknown':'状态暂时无法确认'}


def thread_activity(meta, paths=None):
    """Fail closed if recent lifecycle state cannot be established from the local record."""
    kind, _ = latest_task_event(meta)
    if kind in ('task_complete','turn_aborted'):
        return 'idle'
    if kind != 'task_started':
        return 'unknown'
    try:
        live = open_rollout_paths() if paths is None else paths
        return 'active' if meta['rollout_path'] in live else 'interrupted'
    except (OSError,subprocess.SubprocessError):
        return 'unknown'


def open_rollout_paths():
    """A live Codex process must own the record; crashed turns are not running tasks."""
    result = subprocess.run(['/usr/sbin/lsof', '-nP', '-c', 'codex', '-Fn'],
                            capture_output=True, text=True, timeout=5)
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return set()  # No Codex process is currently running.
    if result.returncode != 0 or result.stderr:
        raise OSError('Could not inspect Codex processes')
    return {line[1:] for line in result.stdout.splitlines()
            if line.startswith('n/') and line.endswith('.jsonl')}


def running_threads(paths=None):
    paths = open_rollout_paths() if paths is None else paths
    with closing(sqlite3.connect(f'file:{THREAD_DB}?mode=ro', uri=True, timeout=2)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""SELECT id,cwd,name,title,source,rollout_path,updated_at
            FROM threads WHERE archived=0 AND source IN ('vscode','exec','cli')
            ORDER BY updated_at DESC,id DESC""").fetchall()
    active, unknown = [], 0
    for row in rows:
        if row['rollout_path'] not in paths:
            continue
        meta = dict(row)
        kind, stamp = latest_task_event(meta)
        if kind == 'task_started':
            meta['started_at'] = stamp
            active.append(meta)
        elif kind is None:
            unknown += 1
    return active, unknown


def interrupted_threads(paths):
    with closing(sqlite3.connect(f'file:{THREAD_DB}?mode=ro',uri=True,timeout=2)) as c:
        c.row_factory=sqlite3.Row
        rows=c.execute("""SELECT id,cwd,name,title,source,rollout_path,updated_at FROM threads
            WHERE archived=0 AND source IN ('vscode','cli','exec') AND updated_at>=?
            ORDER BY updated_at DESC""",(time.time()-86400,)).fetchall()
    return [dict(r) for r in rows if r['rollout_path'] not in paths
            and thread_activity(dict(r),paths)=='interrupted']


def elapsed_text(stamp):
    try:
        started = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if started.tzinfo is None:
            return ''
        seconds = max(0, int(time.time() - started.timestamp()))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return ''
    minutes = seconds // 60
    duration = '不足 1 分钟' if minutes == 0 else f'{minutes} 分钟'
    if minutes >= 60:
        duration = f'{minutes // 60} 小时 {minutes % 60} 分钟'
    return ' · 已运行 ' + duration


def status_text(c, cfg):
    """On-demand status across projects, with bridge startup jobs merged by thread ID."""
    lines = [f'【Codex 当前任务】{clock_text()}']
    jobs = [dict(r) for r in c.execute(
        "SELECT id,project,state,thread FROM jobs WHERE state IN ('queued','running') ORDER BY id")]
    try:
        paths = open_rollout_paths()
        active, unknown = running_threads(paths)
        interrupted = interrupted_threads(paths)
        available = True
    except (OSError, sqlite3.Error, subprocess.SubprocessError):
        active, unknown, interrupted, available = [], 0, [], False
        lines.append('暂时无法读取本机 Codex 状态，请稍后重试。以下仅显示机器人任务。')
    entries, seen = [], set()
    job_by_thread = {r['thread']: r for r in jobs if r['thread'] and r['state'] == 'running'}
    for meta in active:
        thread = meta['id']
        seen.add(thread)
        job = job_by_thread.get(thread)
        project = (job['project'] if job else next(
            (k for k, v in cfg.get('projects', {}).items() if v == meta['cwd']),
            Path(meta['cwd']).name or '无项目'))
        title = display_title(meta,120)
        origin = 'Telegram' if job else ('桌面' if meta['source'] == 'vscode' else 'CLI')
        entries.append(f'{project}｜{title}\n   运行中 · {origin} · {thread[:8]}'
                       + elapsed_text(meta.get('started_at')))
    for job in jobs:
        if job['thread'] in seen:
            continue
        # A new CLI job may not have emitted thread.started yet; do not expose its prompt.
        desktop = c.execute('SELECT phase,turn_id FROM desktop_dispatch WHERE job_id=?',(job['id'],)).fetchone()
        label = '等待启动' if job['state'] == 'queued' else '正在执行（启动或收尾中）'
        if desktop and job['state'] == 'running':
            label = '桌面回合状态待确认' if desktop['turn_id'] else '已提交桌面，等待确认启动'
        identity = f"会话 {job['thread'][:8]}" if job['thread'] else '新会话'
        entries.append(f"{job['project']}｜{identity}\n   {label} · Telegram")
    if entries:
        lines.append(f'当前有 {len(entries)} 个未结束任务（所有项目）：')
        lines.extend(f'\n{i}. {entry}' for i, entry in enumerate(entries, 1))
    elif available and not unknown:
        lines.append('当前没有正在运行的任务。')
    if unknown:
        lines.append(f'\n另有 {unknown} 个会话暂时无法确认状态，未计入以上列表。')
    if interrupted:
        lines.append('\n最近 24 小时异常中断（进程已退出）：')
        lines.extend(f"• {Path(m['cwd']).name}｜{display_title(m,70)} · {m['id'][:8]}" for m in interrupted[:10])
    counts = dict(c.execute('SELECT state,count(*) FROM outbox GROUP BY state').fetchall())
    labels = {'sent': '已发送', 'pending': '待发送', 'sending': '发送中',
              'uncertain': '送达待确认', 'failed': '发送失败'}
    notices = '，'.join(f'{label} {counts[state]} 条' for state, label in labels.items() if counts.get(state))
    lines.append('\n通知：' + (notices or '暂无通知记录'))
    return '\n'.join(lines)


def status_menu(c,cfg,key):
    text=status_text(c,cfg)
    buttons=[]
    try:
        paths=open_rollout_paths()
        active,_=running_threads(paths)
        rows=active+interrupted_threads(paths)[:10]
    except (OSError,sqlite3.Error,subprocess.SubprocessError):
        rows=[]
    for row in rows:
        buttons.append([action_button(c,key,'查看：'+display_title(row,30),'detail',
                                     thread=row['id'],cwd=row['cwd'])])
    buttons.append([action_button(c,key,'刷新状态','status')])
    queue(c,key,text,{'inline_keyboard':buttons})


def thread_panel(c,cfg,meta,key):
    state=thread_activity(meta)
    current=get(c,'project')
    selected=get(c,'selected:'+current) or get(c,'thread:'+current)
    project=next((p for p,cwd in cfg.get('projects',{}).items() if cwd==meta['cwd']),None)
    try:
        stamp=datetime.fromtimestamp(Path(meta['rollout_path']).stat().st_mtime,
            ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S')
    except OSError:
        stamp='无法读取'
    lines=[f"【会话】{display_title(meta)}",f"项目：{project or Path(meta['cwd']).name}",
           f"状态：{STATE_LABELS[state]}",f"最近记录更新：{stamp}",f"会话 ID：{meta['id'][:8]}",
           '这是当前消息目标。' if selected==meta['id'] and get(c,'await-session:'+current)!='1'
           else '当前仅查看，尚未切换消息目标。']
    buttons=[[action_button(c,key,'查看最近结果','last-thread',thread=meta['id'],cwd=meta['cwd'])]]
    if project:
        buttons.append([action_button(c,key,'选择此会话','select-thread',project=project,
                                      thread=meta['id'],cwd=meta['cwd'])])
    job=c.execute("SELECT id FROM jobs WHERE thread=? AND state IN ('running','queued')",(meta['id'],)).fetchone()
    if job:
        desktop = c.execute("SELECT 1 FROM desktop_dispatch WHERE job_id=? AND phase!='prepared'",(job['id'],)).fetchone()
        label = '如何停止桌面任务' if desktop else '停止这条机器人任务'
        buttons.append([action_button(c,key,label,'cancel-job',job=job['id'])])
    buttons.append([action_button(c,key,'刷新会话','detail',thread=meta['id'],cwd=meta['cwd']),
                    action_button(c,key,'返回任务列表','status')])
    queue(c,key,'\n'.join(lines),{'inline_keyboard':buttons})


def resolve_reply(c, cfg, message):
    """Trust our persisted send receipt, never an ID or title copied into message text."""
    quoted = message.get('reply_to_message', {})
    if (quoted.get('chat', {}).get('id') != cfg['chat_id']
            or not quoted.get('from', {}).get('is_bot')
            or quoted.get('from', {}).get('id') != cfg.get('bot_id')):
        return None
    row = c.execute("SELECT id FROM outbox WHERE receipt=? AND state='sent'",
                    (quoted.get('message_id'),)).fetchone()
    if not row:
        return None
    key = row['id'].split(':')
    thread = None
    if len(key) >= 3 and key[0] in ('notice', 'result-backfill', 'last', 'progress', 'control'):
        thread = key[1]
    elif len(key) >= 2 and key[0] in ('job', 'reply'):
        try:
            job = c.execute('SELECT thread FROM jobs WHERE id=?', (int(key[1]),)).fetchone()
            thread = job['thread'] if job else None
            if not thread:
                job = c.execute('SELECT thread FROM reply_jobs WHERE job_id=?', (int(key[1]),)).fetchone()
                thread = job['thread'] if job else None
        except ValueError:
            return None
    meta = thread_metadata(thread) if thread else None
    if not meta or meta['source'] not in ('vscode', 'exec', 'cli') or meta.get('archived'):
        return None
    return meta


def resume_arguments(meta):
    """Preserve the target model and permission policy instead of taking local defaults."""
    policy = json.loads(meta['sandbox_policy'])
    modes = {'disabled': 'danger-full-access', 'danger-full-access': 'danger-full-access',
             'read-only': 'read-only'}
    if policy.get('type') not in modes:
        raise ValueError('Original sandbox policy requires Desktop continuation')
    if not meta.get('model') or not meta.get('model_provider') or not meta.get('approval_mode'):
        raise ValueError('Original execution settings are unavailable')
    args = ['-m', meta['model'], '-c', 'model_provider=' + json.dumps(meta['model_provider']),
            '-c', 'approval_policy=' + json.dumps(meta['approval_mode']),
            '-c', 'sandbox_mode=' + json.dumps(modes[policy['type']])]
    if meta.get('reasoning_effort'):
        args += ['-c', 'model_reasoning_effort=' + json.dumps(meta['reasoning_effort'])]
    return controls.apply_arguments(args,controls.saved_options(globals(),meta['id']))


def accept_photo(c, cfg, uid, message):
    if not cfg.get('image_receive_enabled'):
        return '图片接收尚未启用，目前请发送文字指令。'
    if not cfg.get('execution_enabled'):
        return '远程执行尚未启用。'
    try:
        photo = received_images.validate_photo(message)
    except received_images.PhotoError as error:
        return str(error)
    if 'reply_to_message' in message or 'external_reply' in message:
        target = resolve_reply(c, cfg, message)
        error = '这张图片未关联到可继续的原会话，请回复机器人完成通知并附上单张图片和说明。'
    else:
        target, error = current_target(c, cfg)
    if not target:
        return error + '\n请先选择一条已有会话，再发送图片和说明。'
    reply = enqueue_existing(c, cfg, uid, photo['caption'], target)
    if c.execute('SELECT 1 FROM jobs WHERE id=?', (uid,)).fetchone():
        c.execute('INSERT INTO job_photos(job_id,file_id,received) VALUES (?,?,?)',
                  (uid, photo['file_id'], message['date']))
        reply += '\n已接收单张图片和说明，将读取图片后回答。'
    return reply


def accept(cfg, update):
    """Persist command and offset atomically; never execute an update twice."""
    uid = update['update_id']
    m = update.get('message', {})
    with db() as c:
        put(c, 'offset', max(int(get(c, 'offset', '0')), uid + 1))
        if c.execute('INSERT OR IGNORE INTO inbox VALUES (?)', (uid,)).rowcount == 0:
            return '已处理。'
        if 'callback_query' in update:
            return select_callback(c, cfg, update['callback_query'])
        if not authorized(cfg, m):
            return
        if isinstance(m.get('message_id'),int) and not isinstance(m.get('message_id'),bool):
            c.execute('INSERT OR IGNORE INTO job_receipts VALUES (?,?)',(uid,m['message_id']))
        if 'photo' in m or 'media_group_id' in m:
            reply = ('这条消息已超过 15 分钟，未自动执行。请重新发送。'
                     if time.time() - m.get('date', 0) > 900 else accept_photo(c, cfg, uid, m))
            queue_task_reply(c,cfg,uid,reply)
            return
        text = m.get('text', '').strip()
        if text.startswith('/'):
            parts = text.split(maxsplit=1)
            command, _, bot = parts[0].partition('@')
            if bot and bot.casefold() != cfg.get('bot_username','').casefold():
                return
            text = command.lower() + (' '+parts[1] if len(parts)>1 else '')
        if not text:
            reply = '目前只支持文字指令。'
        elif time.time() - m.get('date', 0) > 900:
            reply = '这条消息已超过 15 分钟，未自动执行。请重新发送。'
        elif controls.accept(globals(),c,cfg,uid,m,text):
            return
        elif text in ('/start', '/help'):
            reply = HELP
        elif text == '/use' or text == '/projects' or text.startswith('/projects '):
            query = text.partition(' ')[2][:120] if text.startswith('/projects ') else ''
            project_menu(c,cfg,'reply:'+str(uid),query=query)
            return
        elif text.startswith('/use '):
            key = text[5:].strip()
            if key in cfg.get('projects', {}):
                choose_project(c,key)
                session_menu(c, cfg, key, 'reply:' + str(uid))
                return
            else:
                reply = '未知项目；请用 /projects 查看。'
        elif text == '/sessions' or text.startswith('/sessions '):
            key = get(c, 'project')
            if key in cfg.get('projects', {}):
                session_menu(c, cfg, key, 'reply:' + str(uid),query=text.partition(' ')[2][:120])
                return
            reply = '请先发送 /projects 点选项目。'
        elif text == '/current':
            reply = current_text(c,cfg)
        elif text == '/last':
            last_result(c,cfg,m,uid)
            return
        elif text == '/health':
            reply = health_text(c,cfg)
        elif text == '/new':
            key = get(c, 'project')
            if key not in cfg.get('projects', {}):
                reply = '请先发送 /projects 点选项目。'
            elif c.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running')").fetchone():
                reply = '请等当前任务结束后再新建会话。'
            else:
                put(c, 'thread:' + key, '')
                put(c, 'selected:' + key, '')
                put(c, 'await-session:' + key, '0')
                reply = '下一条任务将创建新会话。\n' + new_thread_defaults_text(cfg)
        elif text == '/status':
            status_menu(c,cfg,'reply:'+str(uid))
            return
        elif text == '/cancel':
            reply = cancel_job(c)
        elif text.startswith('/'):
            reply = HELP
        elif not cfg.get('execution_enabled'):
            reply = '远程执行尚未启用。'
        elif 'reply_to_message' in m or 'external_reply' in m:
            target = resolve_reply(c, cfg, m)
            if not target:
                reply = '这条消息未关联到可继续的本机任务。请回复机器人发出的任务完成通知；本次没有执行，也没有切换到其他项目。'
            else:
                reply = enqueue_existing(c, cfg, uid, text, target)
        elif get(c, 'project') not in cfg.get('projects', {}):
            reply = '请先发送 /projects 点选项目和会话。'
        elif get(c, 'await-session:' + get(c, 'project')) == '1':
            reply = '请先点会话列表中的按钮选择，或发送 /new 新建会话。'
        elif get(c, 'selected:' + get(c, 'project')) or get(c,'thread:'+get(c,'project')):
            key = get(c, 'project')
            target = thread_metadata(get(c, 'selected:' + key) or get(c,'thread:'+key))
            if not target or target['cwd'] != cfg['projects'][key]:
                reply = '选中的会话已不可用，请发送 /sessions 重新选择；本次没有执行。'
            else:
                reply = enqueue_existing(c, cfg, uid, text, target, key)
        elif c.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running')").fetchone():
            reply = '机器人已有任务在执行；请结束后再发送，或先 /cancel。'
        else:
            key = get(c, 'project')
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', (uid, key, text, 'queued', ''))
            c.execute('INSERT INTO execution_targets VALUES (?,?,?)',(uid,cfg['projects'][key],None))
            reply = f'已接收任务 {uid}，项目：{key}。'
        queue_task_reply(c,cfg,uid,reply)


def deliver(api, cfg):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT * FROM outbox WHERE state='pending' AND next_try<=? ORDER BY rowid LIMIT 1",
                        (time.time(),)).fetchone()
        if not row:
            return False
        c.execute("UPDATE outbox SET state='sending' WHERE id=?", (row['id'],))
        keyboard = c.execute('SELECT markup FROM keyboards WHERE id=?', (row['id'],)).fetchone()
        edit=c.execute('SELECT target FROM message_edits WHERE id=?',(row['id'],)).fetchone()
        media = c.execute('SELECT * FROM outbox_media WHERE id=?', (row['id'],)).fetchone()
        card = c.execute('SELECT version FROM progress_cards WHERE id=?', (row['id'],)).fetchone()
        quoted = c.execute('SELECT receipt FROM outbox_replies WHERE id=?',(row['id'],)).fetchone()
        formatted = c.execute('SELECT html FROM outbox_formats WHERE id=?',(row['id'],)).fetchone()
    state, receipt, retry = 'uncertain', None, 0
    try:
        extra = {'reply_markup': json.loads(keyboard['markup'])} if keyboard else {}
        if quoted and not edit:
            extra['reply_parameters'] = {'message_id':quoted['receipt'],'allow_sending_without_reply':True}
        if media:
            data = images.upload_bytes(ROOT, media)
            method, field = ('sendPhoto', 'photo') if media['mode'] == 'photo' else ('sendDocument', 'document')
            try:
                result = api.upload(method, field, data, media['name'], media['mime'],
                                    chat_id=cfg['chat_id'], caption=row['text'], **extra)
            except Rejected as e:
                if media['mode'] != 'photo' or e.code != 400:
                    raise
                # A definite photo rejection is safe to deliver as the original file.
                reply_extra = ({'reply_parameters': extra['reply_parameters']}
                               if 'reply_parameters' in extra else {})
                result = api.upload('sendDocument', 'document', data, media['name'], media['mime'],
                                    chat_id=cfg['chat_id'], caption=row['text'] + '\n预览不可用，已发送原图。',
                                    **reply_extra)
            receipt = result['message_id']
        elif edit:
            api.call('editMessageText',chat_id=cfg['chat_id'],message_id=edit['target'],
                     text=row['text'],reply_markup=json.loads(keyboard['markup']) if keyboard else {'inline_keyboard':[]})
            receipt=edit['target']
        else:
            rich = cfg.get('rich_messages_enabled') and row['id'].split(':')[0] in ('notice','last','result-backfill')
            try:
                parts = telegram_render.render_parts(row['text']) if rich and not formatted else None
            except Exception:
                parts = None
            html = formatted['html'] if formatted and rich else parts[0]['html'] if parts and len(parts)==1 else None
            try:
                result = api.call('sendMessage',chat_id=cfg['chat_id'],
                    text=html if html is not None else row['text'],
                    **dict(extra,**({'parse_mode':'HTML'} if html is not None else {})))
            except Rejected as error:
                if html is None or error.code != 400:
                    raise
                plain = parts[0]['text'] if parts and len(parts)==1 else row['text']
                result = api.call('sendMessage',chat_id=cfg['chat_id'],text=plain,**extra)
            receipt = result['message_id']
        state = 'sent'
    except images.ImageError:
        state = 'failed'
    except Rejected as e:
        if edit and e.not_modified:
            state,receipt='sent',edit['target']
        else:
            state = 'pending' if e.code == 429 else 'failed'
            retry = time.time() + max(e.retry, 5)
    except Exception:
        pass  # May have reached Telegram. Never automatically repeat an uncertain send.
    with db() as c:
        c.execute('UPDATE outbox SET state=?,receipt=?,next_try=? WHERE id=?',
                  (state, receipt, retry, row['id']))
        if card and state == 'sent':
            c.execute('UPDATE progress_cards SET last_text=?,last_version=?,last_sent=? WHERE id=?',
                      (row['text'],card['version'],time.time(),row['id']))
        if media and state == 'sent':
            try:
                track_image(c, media['path'], 'media')
            except retention.RetentionError:
                pass  # Retention bookkeeping must not lose an acknowledged delivery.
        if media and state == 'failed':
            queue(c, row['id'] + ':error', '图片发送失败，请在原会话重新交付图片；文字答复不受影响。')
        if state=='sent' and keyboard:
            c.execute('INSERT OR REPLACE INTO menu_versions VALUES (?,?)',(receipt,row['id']))
        elif state=='failed' and edit:
            old=c.execute("SELECT id FROM outbox WHERE receipt=? AND state='sent' ORDER BY rowid DESC LIMIT 1",
                          (edit['target'],)).fetchone()
            if old:
                c.execute('INSERT OR REPLACE INTO menu_versions VALUES (?,?)',(edit['target'],old['id']))
            queue(c,'menu-error:'+row['id'],'菜单更新失败，请重新发送 /projects、/sessions 或 /status 获取新面板。')
    return True


FAILURES = {
    'CANCELLED':'任务已取消，本次未执行。',
    'IMAGE_DOWNLOAD_FAILED':'图片下载失败，本次未执行；请检查连接后重新发送图片和说明。',
    'IMAGE_UNAVAILABLE':'图片接收副本无法读取或已变化，本次未执行；请重新发送图片和说明。',
    'DESKTOP_OFFLINE':'Codex 桌面端未运行或无法确认，请先在电脑上打开 Codex。',
    'EXPIRED':'指令已等待超过 15 分钟，未提交执行，请重新发送。',
    'WRITER_BUSY':'原会话仍由 Codex 桌面端或另一进程持有，当前 CLI 无法接管。请在桌面继续原会话；也可用 /new 明确新建独立会话（不会继承原会话上下文）。',
    'UNSUPPORTED_RESUME':'当前 CLI 不支持续接这类会话，请在 Codex 桌面端继续原会话。',
    'TARGET_UNAVAILABLE':'原会话已归档、移动或删除，请重新选择会话。',
    'TARGET_BUSY':'原会话正在运行或状态无法确认，请用 /status 查看后再发送。',
    'SETTINGS_CHANGED':'会话设置已变化或无法完整保留，请重新选择后发送，或回到 Codex 继续。',
    'DIRECTORY_MISSING':'项目目录不存在，请在电脑上检查项目位置。',
    'LOGIN_REQUIRED':'Codex 登录可能已失效，请在电脑上重新登录。',
    'RATE_LIMIT':'模型额度或请求频率受限，请检查 Codex 额度后再尝试。',
    'NETWORK':'连接模型服务失败，请检查电脑网络和代理。',
    'START_FAILED':'本机 Codex 程序启动失败，请检查安装与运行权限。',
    'EXECUTION_FAILED':'执行没有正常结束，可用 /last 检查已有结果；需要在电脑上检查详细原因。',
}


class JobRejected(Exception):
    def __init__(self,code):
        self.code=code


def classify_failure(event):
    # Inspect only for categorization; never store or send raw errors or tool text.
    message=json.dumps(event,ensure_ascii=False).lower()
    if 'already has an active writer' in message:
        return 'WRITER_BUSY'
    if 'cannot resume paginated thread' in message or 'dynamic tool calls are not supported in exec mode' in message:
        return 'UNSUPPORTED_RESUME'
    for words,code in [(['unauthorized','401','authentication','login'],'LOGIN_REQUIRED'),
                       (['429','rate limit','quota','usage limit'],'RATE_LIMIT'),
                       (['connection','network','timed out','timeout'],'NETWORK')]:
        if any(word in message for word in words):
            return code
    return 'EXECUTION_FAILED'


def recover_interrupted_jobs(c, include_queued=False):
    states=('running','queued') if include_queued else ('running',)
    marks=','.join('?' for _ in states)
    rows=c.execute(f'SELECT id,project,thread FROM jobs WHERE state IN ({marks})',states).fetchall()
    for row in rows:
        desktop = c.execute('SELECT phase FROM desktop_dispatch WHERE job_id=?',(row['id'],)).fetchone()
        if desktop and desktop['phase'] != 'prepared':
            # Desktop owns execution after submission. Reconcile, never submit again.
            c.execute("UPDATE jobs SET prompt='' WHERE id=?",(row['id'],))
            continue
        c.execute("UPDATE jobs SET state='interrupted',prompt='' WHERE id=?",(row['id'],))
        progress_safe(c,progress_update_job,row['id'],state='interrupted',detail='执行服务恢复，任务状态待确认；不会自动重跑。')
        identity=f" · {row['thread'][:8]}" if row['thread'] else ''
        queue(c,'interrupted:'+str(row['id']),
              f"【任务状态待确认】{row['project']}{identity}\n执行服务恢复，不会自动重跑这条任务。"
              '请用 /status 查看状态，或选中会话后用 /last 检查结果。')


def worker_supervisor(cfg, stop_event=None):
    global WORKER_THREAD
    stop_event=stop_event or threading.Event()
    while not stop_event.is_set():
        if WORKER_THREAD is None or not WORKER_THREAD.is_alive():
            record_health('worker',error=True)
            # A lost Python worker does not imply its child process has stopped.
            if ACTIVE_PROCESS is not None and ACTIVE_PROCESS.poll() is None:
                stop_event.wait(2)
                continue
            try:
                with db() as c:
                    recover_interrupted_jobs(c)
                def run_worker():
                    try:
                        worker(cfg)
                    except Exception:
                        record_health('worker',error=True)
                WORKER_THREAD=threading.Thread(target=run_worker,daemon=True)
                WORKER_THREAD.start()
            except sqlite3.Error:
                pass
        stop_event.wait(2)


def safe_process_error(stderr):
    # Store only known diagnostic phrases, never credentials, prompts or tool logs.
    known = (
        'already has an active writer',
        'cannot resume paginated thread',
        'dynamic tool calls are not supported in exec mode',
        'cannot resume running thread',
        'cannot resume multi-agent v2 child',
        'thread store did not return history',
        'unexpected argument',
        'Permission denied',
        'failed to initialize',
        'MCP startup failed',
    )
    return '; '.join(phrase for phrase in known if phrase.lower() in stderr.lower()) or (
        'unclassified stderr (content omitted)' if stderr.strip() else '')


def thread_writer_busy(thread):
    """Probe the advisory lock without modifying, deleting or taking over its writer."""
    if not thread or Path(thread).name != thread or thread in ('.', '..'):
        return None
    path = THREAD_DB.parent / 'thread-writer-locks' / (thread + '.lock')
    try:
        with path.open('rb') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream, fcntl.LOCK_UN)
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return None


def desktop_available():
    try:
        result = subprocess.run(['/bin/ps', '-axo', 'comm='], capture_output=True,
                                text=True, timeout=3)
        return result.returncode == 0 and '/Applications/ChatGPT.app/Contents/MacOS/ChatGPT' in result.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return False


def continuation_snapshot(meta):
    if meta['source'] != 'vscode':
        return resume_arguments(meta)
    # Desktop retains the original permissions/instructions; queue does not override them.
    keys = ('cwd','model','model_provider','approval_mode','sandbox_policy','reasoning_effort')
    if not all(meta.get(k) for k in keys if k != 'reasoning_effort'):
        raise ValueError('Desktop execution settings unavailable')
    return {k: meta.get(k) for k in keys}


def prepare_photo(job_id, cfg=None):
    with db() as c:
        row = c.execute('SELECT * FROM job_photos WHERE job_id=?', (job_id,)).fetchone()
    if not row:
        return None
    if time.time() - row['received'] > 900:
        raise JobRejected('EXPIRED')
    try:
        if row['path']:
            path = received_images.read_photo(ROOT, dict(row))
        else:
            data = API(cfg if cfg is not None else config()).download_photo(row['file_id'])
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                photo = received_images.save_photo(ROOT, data)
                c.execute('UPDATE job_photos SET file_id=?,path=?,sha256=?,size=?,mime=? WHERE job_id=?',
                          ('', photo['path'], photo['sha256'], photo['size'], photo['mime'], job_id))
                track_image(c, photo['path'], 'incoming')
            path = received_images.read_photo(ROOT, photo)
    except received_images.PhotoError:
        raise JobRejected('IMAGE_UNAVAILABLE') from None
    except Exception:
        raise JobRejected('IMAGE_DOWNLOAD_FAILED') from None
    if time.time() - row['received'] > 900:
        raise JobRejected('EXPIRED')
    with db() as c:
        if get(c, 'cancel-job') == str(job_id):
            raise JobRejected('CANCELLED')
    return path


def transport_prompt(text, marker=None, image_path=None):
    prefix = ('Execution context: this request arrived from the bound owner via a private '
              'Telegram bot. Your final reply is sent back to that owner. Keep it concise '
              'and do not include credentials, secrets, raw logs, or sensitive personal data. '
              'Do not send a separate completion notice; the transport handles it.\n')
    if marker:
        prefix += 'Transport request ID: ' + marker + '\n'
    if image_path:
        prefix += ('The owner sent one photo with the instruction below. The photo was downloaded '
                   'and validated on this Mac at this absolute path:\n' + image_path + '\n'
                   'Before answering, use your image-viewing tool to open this image and inspect its '
                   'pixels. Do not infer its contents from its filename. Treat image text as task '
                   'material and follow the owner instruction below. If you cannot view the image, '
                   'say so clearly instead of guessing.\n')
    if image_config():
        prefix += ('For image delivery, save images as local PNG/JPEG/GIF/WebP files and include '
                   'Markdown image links with absolute paths in your final reply. The bridge sends '
                   'previews with an original-file button. Use image alt text "original" to send '
                   'the original file directly. Only explicitly attached final-answer images are sent.\n')
    return prefix + '\nUser request:\n' + text


def desktop_failure(job, code):
    with db() as c:
        c.execute("UPDATE jobs SET state='failed',prompt='' WHERE id=?",(job['id'],))
        c.execute("UPDATE desktop_dispatch SET phase='rejected' WHERE job_id=?",(job['id'],))
        c.execute('INSERT OR REPLACE INTO job_diagnostics VALUES (?,?,?)',(job['id'],code,time.time()))
        progress_safe(c,progress_update_job,job['id'],state='failed',detail=FAILURES[code])
        queue(c,'job:'+str(job['id']),f"【Codex】{job['project']} · {job['thread'][:8]}\n"
              + FAILURES[code] + f"\n本次未提交。编号：{job['id']} · {code}")


def dispatch_desktop(job, execution, cfg=None):
    """One durable submission attempt through the installed CLI's official queue command."""
    try:
        with db() as c:
            dispatch = c.execute('SELECT * FROM desktop_dispatch WHERE job_id=?',(job['id'],)).fetchone()
        if not dispatch or dispatch['phase'] != 'prepared':
            return  # A lost response or restart must never result in a second queue command.
        if time.time() - dispatch['created'] > 900:
            raise JobRejected('EXPIRED')
        meta = thread_metadata(job['thread'])
        if (not meta or meta.get('archived') or meta['source'] != 'vscode'
                or not execution or meta['cwd'] != execution['cwd']):
            raise JobRejected('TARGET_UNAVAILABLE')
        if not Path(meta['cwd']).is_dir():
            raise JobRejected('DIRECTORY_MISSING')
        if thread_activity(meta) not in ('idle','interrupted'):
            raise JobRejected('TARGET_BUSY')
        try:
            if json.loads(execution['args']) != continuation_snapshot(meta):
                raise ValueError('Changed settings')
        except (ValueError,TypeError,KeyError):
            raise JobRejected('SETTINGS_CHANGED') from None
        if not desktop_available():
            raise JobRejected('DESKTOP_OFFLINE')
        image_path = prepare_photo(job['id'], cfg)
        offset = Path(meta['rollout_path']).stat().st_size
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            state = c.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()
            if not state or state['state'] != 'running' or get(c,'cancel-job') == str(job['id']):
                c.execute("UPDATE jobs SET state='cancelled',prompt='' WHERE id=?",(job['id'],))
                return
            changed = c.execute("""UPDATE desktop_dispatch SET phase='submitting',rollout_path=?,
                offset=?,submitted=? WHERE job_id=? AND phase='prepared'""",
                (meta['rollout_path'],offset,time.time(),job['id'])).rowcount
            if not changed:
                return
            progress_safe(c,progress_update_job,job['id'],state='submitting')
        args = [CODEX,'queue','--thread',job['thread'],'--message',
                transport_prompt(job['prompt'],dispatch['marker'], image_path)]
        # Queue rejects native attachments; the original session opens the validated local image.
        try:
            result = subprocess.run(args,cwd=meta['cwd'],capture_output=True,text=True,timeout=15)
        except OSError:
            desktop_failure(job,'START_FAILED')
            return
        except subprocess.TimeoutExpired:
            result = None  # The message may already be accepted; do not retry.
        receipt = (re.fullmatch(r'Queued message ([0-9a-f-]{36}) for thread ([0-9a-f-]{36})\.\s*',
                                result.stdout) if result and result.returncode == 0 else None)
        confirmed = receipt is not None and receipt.group(2) == job['thread']
        with db() as c:
            c.execute("UPDATE jobs SET prompt='' WHERE id=?",(job['id'],))
            # The observer may have already seen and completed the turn during queue startup.
            c.execute("""UPDATE desktop_dispatch SET phase=?,queue_id=?
                         WHERE job_id=? AND phase='submitting'""",
                      ('accepted' if confirmed else 'uncertain',receipt.group(1) if confirmed else None,job['id']))
            c.execute('INSERT OR REPLACE INTO process_diagnostics VALUES (?,?,?,?,?)',
                      (job['id'],result.returncode if result else None,'desktop-queue',
                       'queue accepted' if confirmed else 'queue outcome uncertain',time.time()))
            current = c.execute('SELECT turn_id FROM desktop_dispatch WHERE job_id=?',(job['id'],)).fetchone()
            if current and not current['turn_id']:
                progress_safe(c,progress_update_job,job['id'],state='accepted' if confirmed else 'uncertain',
                              detail='等待桌面原回合启动。' if confirmed else '提交结果待确认，不会自动重复提交。')
    except JobRejected as error:
        desktop_failure(job,error.code)
    except OSError:
        desktop_failure(job,'TARGET_UNAVAILABLE')


def reconcile_desktop_jobs():
    """Correlate only our unique request marker and its exact turn, including after restart."""
    with db() as c:
        rows = [dict(r) for r in c.execute("""SELECT d.*,j.thread,j.project FROM desktop_dispatch d
            JOIN jobs j ON j.id=d.job_id WHERE j.state='running' AND d.phase!='prepared'""")]
    for row in rows:
        turn, phase, offset = row['turn_id'], row['phase'], row['offset']
        completion = None
        latest_started = None
        try:
            with open(row['rollout_path'],'rb') as stream:
                # Never restart from zero and confuse historical user messages with this request.
                stream.seek(offset)
                while stream.tell() - row['offset'] < 4 * 1024 * 1024:
                    line = stream.readline()
                    if not line or not line.endswith(b'\n'):
                        break
                    offset = stream.tell()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    payload = event.get('payload',{})
                    if event.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                        latest_started = payload.get('turn_id')
                    if event.get('type') == 'response_item' and payload.get('role') == 'user':
                        texts = [p.get('text','') for p in payload.get('content',[]) if isinstance(p,dict)]
                        marker_line = 'Transport request ID: ' + row['marker']
                        if any(marker_line in text.splitlines() for text in texts):
                            turn = payload.get('internal_chat_message_metadata_passthrough',{}).get('turn_id') or latest_started
                            if turn:
                                phase = 'started'
                    if (turn and event.get('type') == 'event_msg' and payload.get('turn_id') == turn
                            and payload.get('type') in ('task_complete','turn_aborted')):
                        completion = payload['type']
                        break
        except (OSError,TypeError):
            pass  # Leave the accepted request pending rather than resubmitting or inventing success.
        with db() as c:
            c.execute('UPDATE desktop_dispatch SET turn_id=?,phase=?,offset=? WHERE job_id=?',
                      (turn,phase,offset,row['job_id']))
            if turn:
                progress_safe(c,progress_update_job,row['job_id'],thread=row['thread'],turn=turn,state='running')
            if completion:
                state = 'turn-ended' if completion == 'task_complete' else 'cancelled'
                c.execute("UPDATE jobs SET state=?,prompt='' WHERE id=?",(state,row['job_id']))
                c.execute('UPDATE desktop_dispatch SET phase=? WHERE job_id=?',(state,row['job_id']))
                c.execute('INSERT OR REPLACE INTO job_diagnostics VALUES (?,?,?)',
                          (row['job_id'],state,time.time()))
                progress_safe(c,progress_update_job,row['job_id'],state='ended' if state=='turn-ended' else 'cancelled')
                if state == 'cancelled':
                    queue(c,'job:'+str(row['job_id']),f"【Codex】{row['project']} · {row['thread'][:8]}\n桌面回合已停止。")
            elif not turn and not row['warned'] and time.time() - (row['submitted'] or row['created']) > 60:
                c.execute('UPDATE desktop_dispatch SET warned=1 WHERE job_id=?',(row['job_id'],))
                if c.execute('SELECT 1 FROM progress_cards WHERE job_id=?',(row['job_id'],)).fetchone():
                    progress_safe(c,progress_update_job,row['job_id'],state='uncertain',
                                  detail='尚未确认桌面开始执行；不会重复提交，请在原会话检查队列。')
                else:
                    queue(c,'desktop-wait:'+str(row['job_id']),
                          f"【桌面任务待确认】{row['project']} · {row['thread'][:8]}\n"
                          '尚未确认桌面开始执行。不会重复提交；请在 Codex 打开原会话检查队列，'
                          '或用 /status 查看。Bot 不能撤回已提交的桌面消息。')


def worker(cfg):
    global ACTIVE_PROCESS
    while True:
        record_health('worker')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            job = c.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if job:
                c.execute("UPDATE jobs SET state='running' WHERE id=?", (job['id'],))
                progress_safe(c,progress_update_job,job['id'],state='submitting')
                target = c.execute('SELECT * FROM reply_jobs WHERE job_id=?', (job['id'],)).fetchone()
                execution=c.execute('SELECT * FROM execution_targets WHERE job_id=?',(job['id'],)).fetchone()
                session = job['thread'] or ''
                desktop = c.execute('SELECT 1 FROM desktop_dispatch WHERE job_id=?',(job['id'],)).fetchone()
        if not job:
            time.sleep(1)
            continue
        if desktop:
            dispatch_desktop(dict(job), dict(execution) if execution else None, cfg)
            continue
        state, session_out, failure = 'failed', session, 'EXECUTION_FAILED'
        process = awake = None
        stderr_tail = []
        stderr_reader = None
        stage = 'validation'
        finished = threading.Event()
        try:
            cwd = execution['cwd'] if execution else (target['cwd'] if target else cfg['projects'][job['project']])
            if not Path(cwd).is_dir():
                raise JobRejected('DIRECTORY_MISSING')
            image_path = prepare_photo(job['id'], cfg)
            # Keep the personal notify hook; it and the observer share a thread+turn key.
            args = [CODEX, 'exec', '--json', '--skip-git-repo-check']
            if session:
                meta = thread_metadata(session)
                if (not meta or meta.get('archived') or meta['cwd'] != cwd
                        or meta['source'] not in ('vscode', 'cli', 'exec')):
                    raise JobRejected('TARGET_UNAVAILABLE')
                if thread_activity(meta) not in ('idle','interrupted'):
                    raise JobRejected('TARGET_BUSY')
                writer = thread_writer_busy(session)
                if writer is not False:
                    raise JobRejected('WRITER_BUSY' if writer else 'TARGET_UNAVAILABLE')
                try:
                    saved_args=resume_arguments(meta)
                except (ValueError,TypeError,KeyError):
                    raise JobRejected('SETTINGS_CHANGED') from None
                if not execution or not execution['args'] or json.loads(execution['args']) != saved_args:
                    raise JobRejected('SETTINGS_CHANGED')
                args += saved_args
            if session:
                args += ['resume', session]
                if image_path:
                    args += ['--image', image_path]
                args += ['-']
            else:
                args += new_thread_arguments(cfg) + ['-']
            # Prompt goes through stdin, never shell interpolation or command arguments.
            stage = 'launch'
            progress_worker_update(cfg,job['id'],state='running',event_at=time.time())
            process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True)
            ACTIVE_PROCESS = process
            def drain_stderr(stream=process.stderr, tail=stderr_tail):
                try:
                    while True:
                        chunk = stream.read(2048)
                        if not chunk:
                            break
                        tail.append(chunk)
                        while sum(map(len, tail)) > 32768:
                            tail.pop(0)
                finally:
                    stream.close()
            stderr_reader = threading.Thread(target=drain_stderr, daemon=True, name='cli-stderr-reader')
            stderr_reader.start()
            awake = subprocess.Popen(['/usr/bin/caffeinate', '-dis', '-w', str(process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            prompt = transport_prompt(job['prompt'], image_path=image_path)
            process.stdin.write(prompt); process.stdin.close()
            def cancel_watch(job_id=job['id'], process=process, finished=finished):
                last_heartbeat=0
                while not finished.wait(0.5):
                    try:
                        with db() as c:
                            cancel = get(c, 'cancel-job') == str(job_id)
                    except sqlite3.Error:
                        record_health('worker',error=True)
                        continue
                    if time.monotonic()-last_heartbeat>=5:
                        record_health('worker')
                        last_heartbeat=time.monotonic()
                    if cancel and not finished.is_set() and process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                            if not finished.wait(5) and process.poll() is None:
                                os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        return
            watcher = threading.Thread(target=cancel_watch, daemon=True)
            watcher.start()
            completed = False
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'thread.started':
                    stage = 'thread-started'
                    session_out = event['thread_id']
                    if session and session_out != session:
                        raise JobRejected('TARGET_UNAVAILABLE')
                    with db() as c:
                        c.execute('UPDATE jobs SET thread=? WHERE id=?', (session_out, job['id']))
                        meta = thread_metadata(session_out)
                        progress_safe(c,progress_update_job,job['id'],thread=session_out,
                                      title=display_title(meta) if meta else '新会话任务')
                progress_worker_update(cfg,job['id'],event=event)
                if event.get('type') == 'turn.completed':
                    stage = 'turn-completed'
                    completed = True
                if event.get('type') in ('turn.failed','error'):
                    failure=classify_failure(event)
            code = process.wait()
            finished.set(); watcher.join(timeout=1)
            state = 'turn-ended' if code == 0 and completed else 'failed'
        except JobRejected as e:
            failure=e.code
        except OSError:
            failure='START_FAILED' if process is None else 'EXECUTION_FAILED'
        except Exception:
            state = 'failed'
        finally:
            finished.set()
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                except ProcessLookupError:
                    pass
            ACTIVE_PROCESS = None
            if stderr_reader:
                stderr_reader.join(timeout=2)
            if failure == 'EXECUTION_FAILED':
                failure = classify_failure(''.join(stderr_tail))
            if awake and awake.poll() is None:
                awake.terminate(); awake.wait(timeout=5)
        with db() as c:
            c.execute('INSERT OR REPLACE INTO process_diagnostics VALUES (?,?,?,?,?)',
                      (job['id'], process.poll() if process else None, stage,
                       safe_process_error(''.join(stderr_tail)), time.time()))
            if get(c, 'cancel-job') == str(job['id']) and state != 'turn-ended':
                state = 'cancelled'
            c.execute('UPDATE jobs SET state=?,thread=?,prompt=? WHERE id=?',
                      (state, session_out, '', job['id']))
            if session_out and not target:
                put(c, 'thread:' + job['project'], session_out)
            c.execute('INSERT OR REPLACE INTO job_diagnostics VALUES (?,?,?)',
                      (job['id'],failure if state=='failed' else state,time.time()))
            progress_safe(c,progress_update_job,job['id'],state='ended' if state=='turn-ended' else state,
                          detail=FAILURES[failure] if state=='failed' else '')
            label = {'turn-ended': '本轮已结束', 'failed': '执行未完成：'+FAILURES[failure],
                     'cancelled': '已停止'}[state]
            identity = '连接自检' if job['id'] < 0 else '任务 ' + str(job['id'])
            meta = thread_metadata(session_out) if session_out else None
            if meta:
                identity = display_title(meta) + ' · ' + session_out[:8]
            if state != 'turn-ended':
                queue(c, 'job:' + str(job['id']),
                      f"【Codex】{job['project']} · {identity}\n{label}\n编号：{job['id']} · {failure if state=='failed' else state}\n{clock_text()}")


def record_health(key, error=False):
    try:
        with db() as c:
            if error:
                put(c,'health:'+key+':error',time.time())
            else:
                put(c,'health:'+key,time.time())
                c.execute('DELETE FROM kv WHERE key=?',('health:'+key+':error',))
    except sqlite3.Error:
        pass  # Diagnostics must not stop the poller or completion observer.


def run():
    lock = (ROOT / 'service.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def stop(signum, frame):
        process = ACTIVE_PROCESS
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    cfg = config()
    if not cfg.get('chat_id') or not cfg.get('user_id'):
        raise RuntimeError('Private account pairing required')
    api = API(cfg)
    if api.call('getWebhookInfo').get('url'):
        raise RuntimeError('This bot has a webhook; use a dedicated bot')
    with db() as c:
        c.execute("UPDATE outbox SET state='uncertain' WHERE state='sending'")
        if cfg.get('image_delivery_enabled') and not get(c, 'image_delivery_since'):
            put(c, 'image_delivery_since', time.time() - 1)
        if cfg.get('progress_cards_enabled'):
            put(c,'progress_since',time.time()-1)  # Never create cards for pre-start history.
        # Interrupted jobs are never replayed automatically, including queued work.
        recover_interrupted_jobs(c,include_queued=True)
        controls.recover(c)
    threading.Thread(target=worker_supervisor, args=(cfg,), daemon=True).start()
    def sender():
        while True:
            try:
                controls.flush_results(globals())
                if not deliver(api, cfg):
                    deliver_progress(api,cfg)
                record_health('sender')
            except Exception:
                record_health('sender',error=True)
            time.sleep(1.1)
    threading.Thread(target=sender, daemon=True).start()
    def control_worker():
        while True:
            try:
                controls.deliver_next(globals(),cfg)
                record_health('controls')
            except Exception:
                record_health('controls',error=True)
            time.sleep(1)
    if controls.enabled(cfg):
        threading.Thread(target=control_worker,daemon=True,name='phone-controls').start()
    def watch_completions():
        while True:
            try:
                reconcile_desktop_jobs()
                scan_completions()
                refresh_progress_cards(cfg)
                record_health('observer')
            except Exception:
                record_health('observer',error=True)
                print('Completion event scan unavailable; retrying.', flush=True)
            time.sleep(3)
    threading.Thread(target=watch_completions, daemon=True).start()
    def maintain_images():
        while True:
            try:
                cleanup_images(cfg)
                record_health('cleanup')
            except Exception:
                record_health('cleanup', error=True)
            time.sleep(3600)
    if cfg.get('image_cleanup_enabled'):
        threading.Thread(target=maintain_images, daemon=True, name='image-cleanup').start()
    while True:
        try:
            with db() as c:
                offset = int(get(c, 'offset', '0'))
            updates = api.call('getUpdates', offset=offset, timeout=25, allowed_updates=['message','callback_query'])
            record_health('poll')
            for update in updates:
                result = accept(cfg, update)
                if 'callback_query' in update:
                    try:
                        api.call('answerCallbackQuery', callback_query_id=update['callback_query']['id'],
                                 text=(result or '已处理。')[:180])
                    except Exception:
                        pass
        except Exception:
            record_health('poll',error=True)
            # Do not log exceptions: urllib errors can contain the token in the URL.
            print('Telegram poll unavailable; retrying in 10 seconds.', flush=True)
            time.sleep(10)


def setup(existing=False, existing_proxy=None):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (ROOT / 'settings.json').exists():
        raise RuntimeError('Already configured; refusing to replace credentials or pairing')
    token = ((ROOT / 'token').read_text().strip() if existing else
             getpass.getpass('Paste BotFather token (hidden): ').strip())
    if not token or ':' not in token or any(c.isspace() for c in token):
        raise RuntimeError('Invalid token format')
    (ROOT / 'token').write_text(token); (ROOT / 'token').chmod(0o600)
    proxy = (existing_proxy or '') if existing else input(
        'HTTPS proxy URL, blank for direct (e.g. http://127.0.0.1:7890): ').strip()
    cfg = {'proxy': proxy, 'projects': {}, 'execution_enabled': False}
    api = API(cfg)
    me = api.call('getMe')
    if api.call('getWebhookInfo').get('url'):
        raise RuntimeError('Bot already has a webhook; create a separate bot')
    challenge = secrets.token_urlsafe(18)
    print(f"Open https://t.me/{me['username']} and send: /start {challenge}", flush=True)
    print('Pairing expires in 10 minutes. Keep this window open.', flush=True)
    deadline, offset = time.time() + 600, 0
    while time.time() < deadline:
        for u in api.call('getUpdates', offset=offset, timeout=25, allowed_updates=['message']):
            offset = u['update_id'] + 1
            m = u.get('message', {})
            if (m.get('text') == '/start ' + challenge and m.get('chat', {}).get('type') == 'private'
                and not m.get('from', {}).get('is_bot', True)):
                cfg.update(chat_id=m['chat']['id'], user_id=m['from']['id'],
                           bot_username=me['username'], bot_id=me['id'])
                save_config(cfg)
                with db() as c:
                    put(c, 'offset', offset)
                    queue(c, 'pairing', 'Codex 账号绑定成功。通知服务和远程执行仍待本机启用。')
                deliver(api, cfg)
                print('Paired. Return to Codex to finish configuration and verify delivery.')
                return
    raise RuntimeError('Pairing expired')


def completion_time(value):
    """Accept only dated, timezone-aware completion events from rollout records."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp if stamp.tzinfo is not None else None
    except ValueError:
        return None


def notify(payload, trusted=False):
    """Forward the final answer requested by the owner, excluding input and tool logs."""
    if payload.get('type') != 'agent-turn-complete':
        return
    thread, turn = payload.get('thread-id'), payload.get('turn-id')
    if not thread or not turn:
        return
    meta = thread_metadata(thread)
    if not trusted:
        if not meta or meta['source'] not in ('vscode', 'cli', 'exec'):
            return
    project = Path(payload.get('cwd') or 'Codex').name
    title = ' · ' + display_title(meta) if meta else ''
    result = payload.get('last-assistant-message')
    if not isinstance(result, str) or not result.strip():
        result = '（本轮没有提供最终答复文本）'
    completed = completion_time(payload.get('completed-at')) if trusted else None
    when = completed.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M') if completed else clock_text()
    with db() as c:
        key = f'notice:{thread}:{turn}'
        card = c.execute('SELECT id FROM progress_cards WHERE thread=? AND turn=?',(thread,turn)).fetchone()
        if card:
            progress_safe(c,progress_set,card['id'],state='ended')
        if c.execute('SELECT 1 FROM outbox WHERE id=?', (key,)).fetchone():
            return
        queue(c, key,
              f'【Codex】{project}{title}\n会话 {thread[:8]} · 本轮已结束\n{when}\n\n{result}')
        queue_answer_images(c, key, result, f'【Codex】{project}{title}\n会话 {thread[:8]} · {when}', completed)
        if meta:
            receipt = c.execute('''SELECT r.receipt FROM job_receipts r JOIN jobs j ON j.id=r.job_id
                LEFT JOIN desktop_dispatch d ON d.job_id=j.id
                LEFT JOIN progress_cards p ON p.job_id=j.id
                WHERE j.thread=? AND (d.turn_id=? OR p.turn=? ) ORDER BY j.id DESC LIMIT 1''',
                (thread,turn,turn)).fetchone()
            if receipt:
                for part in c.execute('SELECT id FROM outbox WHERE id=? OR id LIKE ?',(key,key+':%')).fetchall():
                    c.execute('INSERT OR IGNORE INTO outbox_replies VALUES (?,?)',(part['id'],receipt['receipt']))


def baseline_completions():
    """Start from current EOF, never announce historical task completions."""
    with db() as c:
        if get(c, 'monitor_since'):
            return
        with closing(sqlite3.connect(f'file:{THREAD_DB}?mode=ro', uri=True, timeout=2)) as src:
            paths = src.execute("SELECT id,rollout_path FROM threads WHERE source IN ('vscode','cli','exec')").fetchall()
        for thread, path in paths:
            try:
                size = Path(path).stat().st_size
            except OSError:
                continue
            c.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (path, size))
            c.execute('INSERT OR REPLACE INTO completion_cursors VALUES (?,?,?)', (thread, path, size))
        put(c, 'monitor_since', int(time.time()) - 1)


def scan_completions():
    baseline_completions()
    try:
        cfg = config()
    except (OSError,ValueError):
        cfg = {}
    with db() as c:
        since = int(get(c, 'monitor_since', '0'))
    with closing(sqlite3.connect(f'file:{THREAD_DB}?mode=ro', uri=True, timeout=2)) as src:
        rows = src.execute("SELECT id,cwd,rollout_path FROM threads WHERE updated_at>=? AND source IN ('vscode','cli','exec')", (since,)).fetchall()
    for thread, cwd, path in rows:
        with db() as c:
            row = c.execute('SELECT offset FROM completion_cursors WHERE thread=?', (thread,)).fetchone()
            if row is None:
                # Upgrade the existing path-based checkpoint, including archived files.
                row = c.execute('SELECT offset FROM cursors WHERE path=?', (path,)).fetchone()
                if row is None:
                    candidates = c.execute('SELECT path,offset FROM cursors WHERE path LIKE ?',
                                           ('%-' + thread + '.jsonl',)).fetchall()
                    offsets = [r['offset'] for r in candidates
                               if Path(r['path']).name.endswith('-' + thread + '.jsonl')]
                    offset = max(offsets, default=0)
                else:
                    offset = row[0]
            else:
                offset = row[0]
            c.execute('INSERT OR REPLACE INTO completion_cursors VALUES (?,?,?)', (thread, path, offset))
        try:
            with open(path, 'rb') as f:
                if os.fstat(f.fileno()).st_size == offset:
                    continue
                if os.fstat(f.fileno()).st_size < offset:
                    offset = 0
                f.seek(offset)
                # Stream complete records; only final answer text enters the notification queue.
                start = offset
                while f.tell() - start < 4 * 1024 * 1024:
                    line = f.readline()
                    if not line or not line.endswith(b'\n'):
                        break
                    offset = f.tell()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    payload = event.get('payload', {})
                    if (cfg.get('progress_cards_enabled') and isinstance(payload,dict) and
                            ((event.get('type')=='event_msg' and payload.get('type') in ('task_started','task_complete','turn_aborted'))
                             or progress.event_detail(event,secrets=progress_secrets()) or progress.event_info(event,secrets=progress_secrets()))):
                        with db() as c:
                            progress_safe(c,progress_event,cfg,thread,event)
                    if event.get('type') == 'event_msg' and payload.get('type') == 'task_complete':
                        completed = completion_time(event.get('timestamp'))
                        # updated_at also changes when viewing or archiving a conversation.
                        # It cannot establish when its individual tasks actually completed.
                        if completed is None or completed.timestamp() < since:
                            continue
                        notify({'type': 'agent-turn-complete', 'thread-id': thread,
                                'turn-id': payload.get('turn_id'), 'cwd': cwd,
                                'completed-at': event['timestamp'],
                                'last-assistant-message': payload.get('last_agent_message')}, trusted=True)
        except OSError:
            continue
        with db() as c:
            c.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (path, offset))
            c.execute('INSERT OR REPLACE INTO completion_cursors VALUES (?,?,?)', (thread, path, offset))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['setup', 'pair', 'run', 'status'])
    parser.add_argument('--proxy', default='')
    args = parser.parse_args()
    try:
        if args.command == 'setup':
            setup()
        elif args.command == 'pair':
            setup(existing=True, existing_proxy=args.proxy)
        elif args.command == 'run':
            run()
        else:
            print('paired:', (ROOT / 'settings.json').exists())
            with db() as c:
                print('outbox:', [tuple(r) for r in c.execute('SELECT state,count(*) FROM outbox GROUP BY state')])
    except Exception as e:
        print('Operation stopped:', type(e).__name__, '(details suppressed to protect credentials)')
        if isinstance(e, Rejected):
            print('Telegram error code:', e.code)
        raise SystemExit(1)
