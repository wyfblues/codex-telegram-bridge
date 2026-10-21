"""Owner-only phone controls. Durable intent, no model polling or mutation retry."""
import json
import time

import codex_control
import mobile_runtime as runtime


class Context:
    def __init__(self, values):
        self.values = values

    def __getattr__(self, name):
        return self.values[name]


def enabled(cfg):
    return cfg.get('remote_controls_enabled') is True


def target(b, c, cfg, message):
    if 'reply_to_message' in message or 'external_reply' in message:
        meta = b.resolve_reply(c, cfg, message)
        return meta, '这条消息没有关联到原会话，请回复任务结果或先选择会话。'
    return b.current_target(c, cfg)


def active_turn(b, meta):
    event = b.find_task_event(meta, ('task_started', 'task_complete', 'turn_aborted'))
    if not event or event['payload'].get('type') != 'task_started':
        return None
    turn = event['payload'].get('turn_id')
    return turn if isinstance(turn, str) and turn else None


def enqueue(b, c, cfg, request_id, kind, meta, args=None):
    if kind in ('steer', 'stop', 'set') and not cfg.get('execution_enabled'):
        return '远程执行尚未启用。'
    if kind != 'usage' and not meta:
        return '请先选择一条已有会话。'
    turn = active_turn(b, meta) if meta else None
    if kind in ('steer', 'stop') and not turn:
        return '原会话目前没有可追加或停止的执行回合。'
    if kind == 'set' and (turn or c.execute(
            "SELECT 1 FROM jobs WHERE thread=? AND state IN ('queued','running')", (meta['id'],)).fetchone()):
        return '请等这条会话的任务结束后再调整设置。'
    thread = meta['id'] if meta else ''
    key = 'control:' + thread + ':' + str(request_id)
    c.execute('''INSERT OR IGNORE INTO control_requests
        (id,kind,thread,cwd,turn,args,created,state,reply_key) VALUES (?,?,?,?,?,?,?,?,?)''',
              (str(request_id),kind,thread,meta['cwd'] if meta else '',turn,
               json.dumps(args or {},ensure_ascii=False),time.time(),'queued',key))
    b.queue(c,key,'正在读取额度…' if kind == 'usage' else '正在处理原会话的请求…')
    return None


def accept(values, c, cfg, uid, message, text):
    command, _, argument = text.partition(' ')
    if command not in ('/settings','/model','/reasoning','/think','/fast','/usage','/steer','/stop'):
        return False
    b = Context(values)
    if not enabled(cfg):
        b.queue(c,'reply:'+str(uid),'手机控制尚未启用，请在本机完成配置和测试。')
        return True
    meta, error = target(b,c,cfg,message)
    if not meta and command != '/usage':
        b.queue(c,'reply:'+str(uid),error)
        return True
    kind, args = 'settings', {}
    if command == '/usage':
        kind = 'usage'
    elif command == '/steer':
        if not argument.strip():
            b.queue(c,'reply:'+str(uid),'用法：/steer 补充要求。只追加到选中或被回复的原会话。')
            return True
        kind, args = 'steer', {'text':argument.strip()}
    elif command == '/stop':
        kind = 'stop'
    elif argument.strip():
        field = {'/model':'model','/reasoning':'effort','/think':'effort','/fast':'serviceTier'}.get(command)
        if field:
            value = argument.strip()
            if field == 'serviceTier':
                if value not in ('on','off'):
                    b.queue(c,'reply:'+str(uid),'用法：/fast on 或 /fast off。')
                    return True
                value = 'priority' if value == 'on' else 'default'
            kind, args = 'set', {field:value}
    error = enqueue(b,c,cfg,uid,kind,meta,args)
    if error:
        b.queue(c,'reply:'+str(uid),error)
    return True


def callback(values,c,cfg,callback,action):
    b = Context(values)
    if not enabled(cfg):
        return '手机控制已停用。'
    data = json.loads(action['data'])
    meta = b.thread_metadata(data.get('thread')) if data.get('thread') else None
    if not meta or meta.get('archived') or meta['cwd'] != data.get('cwd'):
        return '原会话已不可用，请重新打开设置。'
    # navigation_callback already checked the private sender and exact menu receipt.
    request = 'button-' + callback['id']
    if c.execute('SELECT 1 FROM control_requests WHERE id=?',(request,)).fetchone():
        return '已接收，请等待原请求结果。'
    kind = data.get('operation','settings')
    args = data.get('options',{})
    error = enqueue(b,c,cfg,request,kind,meta,args)
    return error or '已接收原会话请求。'


def saved_options(values, thread):
    b = Context(values)
    try:
        if not enabled(b.config()):
            return {}
        with b.db() as c:
            row = c.execute('SELECT options FROM runtime_options WHERE thread=?',(thread,)).fetchone()
        value = json.loads(row['options']) if row else {}
        return value if isinstance(value,dict) else {}
    except (OSError, ValueError):
        return {}


def apply_arguments(args, options):
    result, index = [], 0
    keys = {'effort':'model_reasoning_effort=','serviceTier':'service_tier='}
    while index < len(args):
        flag = args[index]
        value = args[index+1] if index+1 < len(args) else None
        if flag == '-m' and value is not None and options.get('model'):
            result += ['-m',options['model']]; index += 2; continue
        if flag == '-c' and isinstance(value,str) and any(
                name in options and value.startswith(prefix) for name,prefix in keys.items()):
            index += 2; continue
        result.append(flag); index += 1
    for name,prefix in keys.items():
        if name in options:
            result += ['-c',prefix+json.dumps(options[name])]
    return result


def finish(b, request, text, keyboard=None, state='done'):
    with b.db() as c:
        c.execute('UPDATE control_requests SET state=?,args=? WHERE id=?',
                  (state,'{}',request['id']))
        c.execute('INSERT OR REPLACE INTO control_results VALUES (?,?,?,0)',
                  (request['id'],text,json.dumps(keyboard) if keyboard else None))
    flush_results(b.values)


def flush_results(values):
    b = Context(values)
    with b.db() as c:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute('''SELECT r.*,q.reply_key FROM control_results r
            JOIN control_requests q ON q.id=r.request_id WHERE r.delivered=0''').fetchall()
        for result in rows:
            _flush_result(b,c,result)


def _flush_result(b,c,result):
    key, text = result['reply_key'],result['text']
    keyboard = json.loads(result['keyboard']) if result['keyboard'] else None
    row = c.execute('SELECT state,receipt FROM outbox WHERE id=?',(key,)).fetchone()
    if not row or row['state'] == 'sending':
        return
    if row['state'] == 'pending':
        c.execute('UPDATE outbox SET text=? WHERE id=?',(text,key))
        if keyboard:
            c.execute('INSERT OR REPLACE INTO keyboards VALUES (?,?)',(key,json.dumps(keyboard)))
    elif row['state'] == 'sent' and row['receipt'] is not None:
        result_key = key + ':result'
        c.execute('UPDATE actions SET menu_id=? WHERE menu_id=?',(result_key,key))
        b.queue(c,result_key,text,keyboard)
        b.edit_menu(c,result_key,row['receipt'])
    c.execute('UPDATE control_results SET delivered=1 WHERE request_id=?',(result['request_id'],))
    # An uncertain initial send never produces a duplicate replacement.


def current_options(b, meta, client):
    snap = runtime.safe_read_snapshot(meta['rollout_path'],meta['id'])
    current = dict(snap.get('current') or {})
    current.setdefault('model',meta.get('model'))
    current.setdefault('effort',meta.get('reasoning_effort'))
    with b.db() as c:
        saved = c.execute('SELECT options FROM runtime_options WHERE thread=?',(meta['id'],)).fetchone()
    if saved and meta['source'] != 'vscode':
        current.update(json.loads(saved['options']))
    if meta['source'] == 'vscode':
        current.pop('serviceTier',None)
        current.pop('service_tier',None)
        settings = client.read_settings(meta['id'])
        if settings.get('modelProvider') and settings['modelProvider'] != meta.get('model_provider'):
            raise codex_control.ControlError('provider_changed')
        if settings.get('model'):
            current['model'] = settings['model']
        if 'reasoningEffort' in settings:
            current['effort'] = settings['reasoningEffort']
        if 'serviceTier' in settings:
            current['serviceTier'] = settings['serviceTier']
    return current


def model_catalog(query):
    rows, cursor = [], None
    for _ in range(5):
        result = query.rpc('model/list',dict({'limit':100,'includeHidden':False}, **({'cursor':cursor} if cursor else {})))
        rows.extend(runtime.catalog_models(result))
        cursor = result.get('nextCursor')
        if not cursor:
            break
    return rows


def settings_panel(b, request, meta, current, catalog):
    buttons = []
    model = next((r for r in catalog if r['model'] == current.get('model')),None)
    with b.db() as c:
        for row in catalog[:12]:
            buttons.append([b.action_button(c,request['reply_key'],
                ('✓ ' if row['model'] == current.get('model') else '')+row['displayName'],
                'runtime-control',thread=meta['id'],cwd=meta['cwd'],operation='set',options={'model':row['model']})])
        if model:
            row = []
            for effort in model['efforts']:
                row.append(b.action_button(c,request['reply_key'],effort,'runtime-control',
                    thread=meta['id'],cwd=meta['cwd'],operation='set',options={'effort':effort}))
                if len(row) == 3:
                    buttons.append(row); row=[]
            if row:
                buttons.append(row)
            if 'priority' in model['serviceTiers']:
                buttons.append([b.action_button(c,request['reply_key'],label,'runtime-control',
                    thread=meta['id'],cwd=meta['cwd'],operation='set',options={'serviceTier':value})
                    for label,value in (('Fast 开启','priority'),('标准速度','default'))])
        buttons.append([b.action_button(c,request['reply_key'],'刷新设置','runtime-control',
            thread=meta['id'],cwd=meta['cwd'],operation='settings')])
    return runtime.options_text(current), {'inline_keyboard':buttons}


def execute(b,cfg,request,client,query):
    if time.time()-request['created'] > 900:
        return '这条控制请求已超过 15 分钟，未执行。',None,'failed'
    kind, args = request['kind'], json.loads(request['args'])
    meta = b.thread_metadata(request['thread']) if request['thread'] else None
    if request['thread'] and (not meta or meta.get('archived') or meta['cwd'] != request['cwd']
                             or meta['source'] not in ('vscode','cli','exec')):
        return '原会话已不可用，没有切换到其他会话。',None,'failed'
    if kind == 'usage':
        snapshot = runtime.safe_read_snapshot(meta['rollout_path'],meta['id']) if meta else {}
        stamp = time.time()
        try:
            limits = query.rpc('account/rateLimits/read',{})
        except codex_control.ControlError:
            limits, stamp = snapshot.get('rate_limits',{}), snapshot.get('sampled_at')
        text = runtime.usage_text(limits,snapshot.get('context'),sampled_at=stamp)
        text += '\n额度为本机登录账号共用；上下文仅属于此会话。' if meta else '\n额度为本机登录账号共用。'
        if meta:
            text = '会话：'+b.display_title(meta,60)+'\n'+text
        return text,None,'done'
    if not meta:
        return '请先选择原会话。',None,'failed'
    if kind in ('steer','stop'):
        if active_turn(b,meta) != request['turn'] or b.thread_activity(meta) != 'active':
            return '原回合已结束或发生变化，本次没有发送控制请求。',None,'failed'
        if kind == 'steer':
            result = client.steer(meta['id'],request['turn'],args['text'],request_id=request['id'])
            guarded = result.get('atomicTurnGuard',True) if isinstance(result,dict) else True
            label = '已追加到原会话处理时的活跃回合。' if not guarded else '已追加到原执行回合。'
            return label,None,'done'
        if meta['source'] != 'vscode':
            with b.db() as c:
                job = c.execute("SELECT id FROM jobs WHERE thread=? AND state='running'",(meta['id'],)).fetchone()
                if job:
                    return b.cancel_job(c,job['id']),None,'done'
        client.interrupt(meta['id'],request['turn'])
        return '已请求停止原回合，最终停止状态以进度卡片为准。',None,'done'
    current = current_options(b,meta,client)
    catalog = model_catalog(query)
    if not catalog:
        return '暂时无法读取可用模型列表，请稍后重试。',None,'failed'
    if kind == 'set':
        if active_turn(b,meta):
            return '原会话正在执行，未修改运行设置。',None,'failed'
        with b.db() as c:
            if c.execute("SELECT 1 FROM jobs WHERE thread=? AND state IN ('queued','running')",(meta['id'],)).fetchone():
                return '原会话已有待执行任务，未修改设置。',None,'failed'
        selected = {'model':current.get('model'), 'effort':current.get('effort')}
        if 'serviceTier' in current:
            selected['serviceTier'] = current['serviceTier']
        if 'model' in args and args['model'] != current.get('model'):
            selected.pop('effort',None)
            selected.pop('serviceTier',None)
        selected.update(args)
        selected = {k:v for k,v in selected.items() if v is not None}
        options = runtime.validate_options(selected,catalog)
        if meta['source'] == 'vscode':
            kwargs = {'model':options['model'],'effort':options.get('effort'),
                      'expected':{'model':current.get('model'),'reasoningEffort':current.get('effort')}}
            if 'serviceTier' in args:
                kwargs['service_tier'] = options['serviceTier']
            applied = client.update_settings(meta['id'],**kwargs)
            if isinstance(applied,dict) and applied.get('applied') is False:
                return '桌面设置已变化，请刷新后再选择。',None,'failed'
        with b.db() as c:
            c.execute('INSERT OR REPLACE INTO runtime_options VALUES (?,?,?)',
                      (meta['id'],json.dumps(options),time.time()))
        current.update(options)
    text, keyboard = settings_panel(b,request,meta,current,catalog)
    if kind == 'set':
        text = '已保存此会话设置，下一轮生效。\n\n'+text
    return text,keyboard,'done'


def deliver_next(values,cfg,client=None,query=None):
    if not enabled(cfg):
        return False
    b = Context(values)
    with b.db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT * FROM control_requests WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:
            return False
        request = dict(row)
        c.execute("UPDATE control_requests SET state='sending' WHERE id=?",(row['id'],))
    try:
        client = client or codex_control.Client(codex=b.CODEX)
        query = query or codex_control.CatalogClient(codex=b.CODEX)
        text, keyboard, state = execute(b,cfg,request,client,query)
    except codex_control.ControlError as error:
        state = 'uncertain' if error.uncertain else 'failed'
        text = ('提交结果待确认，不会自动重发；请查看原会话。' if error.uncertain else
                '本机控制通道暂时不可用或原会话未加载，请在 Codex 打开原会话后重试。')
        keyboard = None
    except ValueError as error:
        text,keyboard,state = b.progress.clean(str(error),secrets=b.progress_secrets()),None,'failed'
    except Exception:
        text,keyboard,state = '控制请求未完成，请检查原会话；不会自动重试。',None,'uncertain'
    finish(b,request,text,keyboard,state)
    return True


def recover(c):
    for row in c.execute("SELECT id,state FROM control_requests WHERE state IN ('queued','sending')").fetchall():
        text = ('服务恢复，控制请求结果待确认；不会自动重发，请查看原会话。' if row['state']=='sending'
                else '服务恢复，尚未发送的控制请求已中止；请重新发送。')
        c.execute('INSERT OR IGNORE INTO control_results VALUES (?,?,NULL,0)',(row['id'],text))
    c.execute("UPDATE control_requests SET state='uncertain',args='{}' WHERE state='sending'")
    c.execute("UPDATE control_requests SET state='interrupted',args='{}' WHERE state='queued'")
