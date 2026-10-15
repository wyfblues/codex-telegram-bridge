import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import sqlite3
from contextlib import closing

spec = importlib.util.spec_from_file_location('bridge', Path(__file__).with_name('bridge.py'))
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = b.ROOT; b.ROOT = Path(self.tmp.name)
        self.old_thread_db = b.THREAD_DB; b.THREAD_DB = b.ROOT / 'threads.sqlite'
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute('''CREATE TABLE threads(id,source,cwd,rollout_path,updated_at, archived INTEGER DEFAULT 0,
                model TEXT DEFAULT 'gpt-6-astra', reasoning_effort TEXT DEFAULT 'high',
                sandbox_policy TEXT DEFAULT '{"type":"disabled"}', approval_mode TEXT DEFAULT 'never',
                model_provider TEXT DEFAULT 'openai', title TEXT DEFAULT '测试会话', name TEXT,
                recency_at_ms INTEGER, updated_at_ms INTEGER)''')
        self.cfg = {'chat_id': 123, 'user_id': 123, 'bot_id': 456, 'execution_enabled': True,
                    'projects': {'demo': '/tmp/demo'}}
        self.live_patch=patch.object(b,'open_rollout_paths',side_effect=lambda:{str(p) for p in b.ROOT.glob('*.jsonl')})
        self.live_patch.start()
        # Desktop availability is a fixture, independent of the host's apps.
        desktop_patch = patch.object(b, 'desktop_available', return_value=True)
        desktop_patch.start()
        self.addCleanup(desktop_patch.stop)
    def tearDown(self):
        self.live_patch.stop()
        b.ROOT = self.old; b.THREAD_DB = self.old_thread_db; self.tmp.cleanup()
    def update(self, id, text, user=123, chat=123, kind='private', age=0):
        return {'update_id': id, 'message': {'text': text, 'date': time.time()-age,
            'chat': {'id': chat, 'type': kind}, 'from': {'id': user, 'is_bot': False}}}
    def rows(self, table):
        with b.db() as c: return [dict(r) for r in c.execute('SELECT * FROM ' + table)]
    def test_strangers_groups_and_stale_commands_never_execute(self):
        for i, u in enumerate([self.update(1, '/use demo', user=5),
            self.update(2, '/use demo', chat=-10, kind='group'), self.update(3, '/use demo', age=1000)]):
            b.accept(self.cfg, u)
        b.accept(self.cfg, self.update(4, 'run tests'))
        self.assertEqual(self.rows('jobs'), [])
    def test_requires_project_and_deduplicates_updates(self):
        b.accept(self.cfg, self.update(1, '/use demo'))
        with b.db() as c: b.put(c, 'await-session:demo', '0')
        b.accept(self.cfg, self.update(2, 'run tests'))
        b.accept(self.cfg, self.update(2, 'run tests'))
        b.accept(self.cfg, self.update(3, 'second task'))
        self.assertEqual(len(self.rows('jobs')), 1)
        with b.db() as c: self.assertEqual(b.get(c, 'offset'), '4')
    def test_cancel_does_not_replay_queued_prompt(self):
        b.accept(self.cfg, self.update(1, '/use demo'))
        with b.db() as c: b.put(c, 'await-session:demo', '0')
        b.accept(self.cfg, self.update(2, 'run tests'))
        b.accept(self.cfg, self.update(3, '/cancel'))
        self.assertEqual(self.rows('jobs')[0]['state'], 'cancelled')
        self.assertEqual(self.rows('jobs')[0]['prompt'], '')
    def test_success_receipt_and_no_duplicate_send(self):
        class API:
            calls = 0
            def call(self, *args, **kwargs):
                self.calls += 1
                return {'message_id': 42}
        api = API()
        with b.db() as c: b.queue(c, 'one', 'test')
        b.deliver(api, self.cfg); b.deliver(api, self.cfg)
        self.assertEqual(api.calls, 1)
        self.assertEqual(self.rows('outbox')[0]['receipt'], 42)
    def test_uncertain_send_is_not_automatically_repeated(self):
        class API:
            def call(self, *a, **k): raise TimeoutError()
        with b.db() as c: b.queue(c, 'one', 'test')
        b.deliver(API(), self.cfg)
        self.assertFalse(b.deliver(API(), self.cfg))
        self.assertEqual(self.rows('outbox')[0]['state'], 'uncertain')
    def test_rate_limit_uses_retry_after(self):
        class API:
            def call(self, *a, **k): raise b.Rejected(429, 60)
        with b.db() as c: b.queue(c, 'one', 'test')
        b.deliver(API(), self.cfg)
        r = self.rows('outbox')[0]
        self.assertEqual(r['state'], 'pending')
        self.assertGreater(r['next_try'], time.time()+55)
    def test_notification_includes_final_answer_but_omits_input(self):
        p = {'type': 'agent-turn-complete', 'thread-id': 'abc', 'turn-id': 'def',
            'cwd': '/tmp/demo', 'last-assistant-message': '处理完成：已修复并验证。', 'input-messages': ['SECRET']}
        b.notify(p, trusted=True); b.notify(p, trusted=True)
        rows = self.rows('outbox')
        self.assertEqual(len(rows), 1)
        self.assertNotIn('SECRET', json.dumps(rows))
        self.assertIn('处理完成：已修复并验证。', rows[0]['text'])

    def test_observer_baseline_partial_records_dedupe_and_subagent_filter(self):
        log = b.ROOT / 'events.jsonl'
        def event(turn):
            return json.dumps({'type': 'event_msg', 'timestamp': b.datetime.now(b.ZoneInfo('UTC')).isoformat(),
                               'payload': {'type': 'task_complete', 'turn_id': turn, 'last_agent_message': '监听结果正文'}}) + '\n'
        log.write_text(event('historical'))
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute('INSERT INTO threads(id,source,cwd,rollout_path,updated_at) VALUES (?,?,?,?,?)', ('primary', 'vscode', '/tmp/demo', str(log), int(time.time())))
        b.baseline_completions(); b.scan_completions()
        self.assertEqual(self.rows('outbox'), [])
        line = event('new')
        with log.open('a') as f: f.write(line[:-1])
        b.scan_completions()
        self.assertEqual(self.rows('outbox'), [])
        with log.open('a') as f: f.write('\n')
        b.scan_completions(); b.scan_completions()
        self.assertEqual(len(self.rows('outbox')), 1)
        self.assertIn('监听结果正文', self.rows('outbox')[0]['text'])
        b.notify({'type':'agent-turn-complete','thread-id':'primary','turn-id':'new','cwd':'/tmp/demo'})
        self.assertEqual(len(self.rows('outbox')), 1)
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute('INSERT INTO threads(id,source,cwd,rollout_path,updated_at) VALUES (?,?,?,?,?)', ('child', '{"subagent":{}}', '/tmp/demo', str(log), int(time.time())))
        b.notify({'type':'agent-turn-complete','thread-id':'child','turn-id':'new','cwd':'/tmp/demo'})
        self.assertEqual(len(self.rows('outbox')), 1)
    def test_old_bridge_marker_does_not_suppress_desktop_notice(self):
        with b.db() as c: b.put(c, 'bridge-thread:bot', '1')
        b.notify({'type':'agent-turn-complete','thread-id':'bot','turn-id':'turn','cwd':'/tmp/demo'}, trusted=True)
        self.assertEqual(len(self.rows('outbox')), 1)

    def test_long_result_is_complete_utf16_safe_and_deduplicated(self):
        body = '中文😀\n' * 2100
        with b.db() as c:
            b.queue(c, 'long', body)
            b.queue(c, 'long', body + 'changed')
        rows = self.rows('outbox')
        self.assertGreater(len(rows), 1)
        self.assertEqual(''.join(r['text'].split('\n', 1)[1] for r in rows), body)
        self.assertTrue(all(len(r['text'].encode('utf-16-le')) // 2 <= 4096 for r in rows))
        self.assertEqual(rows[0]['id'], 'long')
        self.assertEqual(rows[-1]['id'], 'long:part:' + str(len(rows)))
    def test_bridge_token_is_hidden_in_final_answer(self):
        (b.ROOT / 'token').write_text('fake-test-token:12345')
        with b.db() as c: b.queue(c, 'token', 'result fake-test-token:12345 done')
        self.assertNotIn('fake-test-token:12345', self.rows('outbox')[0]['text'])
        self.assertIn('Bot Token 已隐藏', self.rows('outbox')[0]['text'])

    def original_task(self, thread='original', active=False):
        log = b.ROOT / (thread + '.jsonl')
        log.write_text(json.dumps({'type':'event_msg', 'payload':{
            'type':'task_started' if active else 'task_complete','turn_id':'turn'}}) + '\n')
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute('INSERT INTO threads(id,source,cwd,rollout_path,updated_at) VALUES (?,?,?,?,?)',
                      (thread, 'vscode', str(b.ROOT), str(log), int(time.time())))
        with b.db() as c:
            b.queue(c, 'notice:' + thread + ':turn', '已完成')
            c.execute('UPDATE outbox SET state=?,receipt=? WHERE id=?',
                      ('sent', 900, 'notice:' + thread + ':turn'))
        return b.thread_metadata(thread)
    def reply_update(self, id=10, receipt=900):
        u = self.update(id, '继续修复')
        u['message']['reply_to_message'] = {'message_id': receipt,
            'chat':{'id':123}, 'from':{'id':456,'is_bot':True}}
        return u
    def test_reply_targets_original_not_selected_project_and_deduplicates(self):
        self.original_task()
        b.accept(self.cfg, self.update(1, '/use demo'))
        with b.db() as c: b.put(c, 'await-session:demo', '0')
        b.accept(self.cfg, self.reply_update())
        b.accept(self.cfg, self.reply_update())
        jobs = self.rows('jobs')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['thread'], 'original')
        self.assertEqual(self.rows('reply_jobs')[0]['cwd'], str(b.ROOT))
        with b.db() as c: self.assertEqual(b.get(c, 'project'), 'demo')
    def test_unknown_foreign_and_external_replies_never_fall_back_to_use(self):
        self.original_task()
        b.accept(self.cfg, self.update(1, '/use demo'))
        with b.db() as c: b.put(c, 'await-session:demo', '0')
        u=self.reply_update(2, 9999);b.accept(self.cfg,u)
        u=self.reply_update(3);u['message']['reply_to_message']['chat']['id']=999;b.accept(self.cfg,u)
        u=self.reply_update(4);u['message']['reply_to_message']['from']['id']=999;b.accept(self.cfg,u)
        u=self.reply_update(5);u['message'].pop('reply_to_message');u['message']['external_reply']={};b.accept(self.cfg,u)
        self.assertEqual(self.rows('jobs'), [])
    def test_active_or_deleted_original_rejected(self):
        meta=self.original_task(active=True)
        b.accept(self.cfg, self.reply_update())
        self.assertEqual(self.rows('jobs'), [])
        Path(meta['rollout_path']).unlink()
        b.accept(self.cfg, self.reply_update(11))
        self.assertEqual(self.rows('jobs'), [])
    def test_reply_to_split_notification_and_bot_result(self):
        self.original_task()
        with b.db() as c:
            c.execute('INSERT INTO outbox(id,text,state,receipt) VALUES (?,?,?,?)',
                      ('notice:original:turn:part:2','第二段','sent',901))
            self.assertEqual(b.resolve_reply(c,self.cfg,self.reply_update(receipt=901)['message'])['id'],'original')
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(123,'demo','','turn-ended','original'))
            c.execute('INSERT INTO outbox(id,text,state,receipt) VALUES (?,?,?,?)',('job:123','结果','sent',902))
            self.assertEqual(b.resolve_reply(c,self.cfg,self.reply_update(receipt=902)['message'])['id'],'original')
    def test_original_settings_preserved_and_unknown_permissions_refused(self):
        meta=self.original_task()
        args=b.resume_arguments(meta)
        self.assertIn('gpt-6-astra',args)
        self.assertIn('model_reasoning_effort="high"',args)
        self.assertIn('approval_policy="never"',args)
        meta['sandbox_policy']='{"type":"unknown"}'
        with self.assertRaises(ValueError): b.resume_arguments(meta)

    def menu_callback(self, token, uid=20, receipt=1000, user=123):
        return {'update_id':uid,'callback_query':{'id':'cb'+str(uid),
            'from':{'id':user,'is_bot':False},'data':'session:'+token,
            'message':{'message_id':receipt,'chat':{'id':123,'type':'private'},
                       'from':{'id':456,'is_bot':True}}}}
    def fixture_menu(self):
        meta=self.original_task()
        self.cfg['projects']['demo']=meta['cwd']
        b.accept(self.cfg,self.update(1,'/use demo'))
        with b.db() as c:
            c.execute("UPDATE outbox SET state='sent',receipt=1000 WHERE id='reply:1'")
        return next(r for r in self.rows('choices') if r['thread']=='original')['token']
    def test_recent_five_exclude_other_projects_archives_and_subagents(self):
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            for i in range(8):
                c.execute('INSERT INTO threads(id,source,cwd,rollout_path,updated_at) VALUES (?,?,?,?,?)',
                          (str(i),'vscode','/tmp/demo','/tmp/log',i))
            for id,source,cwd,archived in [('child','{"subagent":{}}','/tmp/demo',0),
                ('archived','vscode','/tmp/demo',1),('foreign','vscode','/elsewhere',0)]:
                c.execute('INSERT INTO threads(id,source,cwd,rollout_path,updated_at,archived) VALUES (?,?,?,?,?,?)',
                          (id,source,cwd,'/tmp/log',99,archived))
        self.assertEqual([r['id'] for r in b.recent_threads('/tmp/demo')],['7','6','5','4','3'])
    def test_use_sends_buttons_and_selection_routes_plain_message(self):
        token=self.fixture_menu()
        self.assertEqual(len(self.rows('keyboards')),1)
        b.accept(self.cfg,self.update(2,'do not execute before selection'))
        self.assertEqual(self.rows('jobs'),[])
        self.assertEqual(b.accept(self.cfg,self.menu_callback(token)),'已选择。')
        b.accept(self.cfg,self.update(21,'继续处理'))
        self.assertEqual(self.rows('jobs')[0]['thread'],'original')
        b.accept(self.cfg,self.menu_callback(token))
        self.assertEqual(len(self.rows('jobs')),1)
    def test_stale_foreign_wrong_receipt_buttons_do_not_change_selection(self):
        token=self.fixture_menu()
        b.accept(self.cfg,self.menu_callback(token,user=999))
        b.accept(self.cfg,self.menu_callback(token,uid=21,receipt=9999))
        with b.db() as c:
            self.assertEqual(b.get(c,'selected:demo'),'')
            b.put(c,'project','other')
        b.accept(self.cfg,self.menu_callback(token,uid=22))
        with b.db() as c:
            self.assertEqual(b.get(c,'selected:demo'),'')
            b.put(c,'project','demo')
            c.execute('UPDATE choices SET expires=0')
        b.accept(self.cfg,self.menu_callback(token,uid=23))
        with b.db() as c: self.assertEqual(b.get(c,'selected:demo'),'')
    def test_new_button_and_command_clear_selected_target(self):
        token=self.fixture_menu()
        b.accept(self.cfg,self.menu_callback(token))
        newtoken=next(r for r in self.rows('choices') if not r['thread'])['token']
        b.accept(self.cfg,self.menu_callback(newtoken,uid=21))
        with b.db() as c:
            self.assertEqual(b.get(c,'selected:demo'),'')
            self.assertEqual(b.get(c,'await-session:demo'),'0')
        b.accept(self.cfg,self.menu_callback(token,uid=22))
        b.accept(self.cfg,self.update(23,'/new'))
        with b.db() as c: self.assertEqual(b.get(c,'selected:demo'),'')
    def test_reply_still_overrides_selected_session(self):
        token=self.fixture_menu()
        b.accept(self.cfg,self.menu_callback(token))
        self.original_task('second')
        # Two fixtures use receipt 900; use a distinct receipt for the second task.
        with b.db() as c:
            c.execute("UPDATE outbox SET receipt=901 WHERE id='notice:second:turn'")
        b.accept(self.cfg,self.reply_update(id=21,receipt=901))
        self.assertEqual(self.rows('jobs')[0]['thread'],'second')
        with b.db() as c: self.assertEqual(b.get(c,'selected:demo'),'original')
    def test_keyboard_reaches_send_message(self):
        class API:
            payload=None
            def call(self, method, **payload):
                self.payload=payload
                return {'message_id':1234}
        api=API()
        with b.db() as c:
            b.queue(c,'menu','选择',{'inline_keyboard':[[{'text':'选择','callback_data':'session:test'}]]})
        b.deliver(api,self.cfg)
        self.assertIn('reply_markup',api.payload)

    def status_task(self, thread, kinds=('task_started',), source='vscode', archived=0):
        log = b.ROOT / (thread + '.jsonl')
        stamp = b.datetime.fromtimestamp(time.time()-125, b.ZoneInfo('UTC')).isoformat()
        log.write_text(''.join(json.dumps({'type':'event_msg', 'timestamp':stamp,
            'payload':{'type':kind,'turn_id':thread}}) + '\n' for kind in kinds))
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute('''INSERT INTO threads(id,source,cwd,rollout_path,updated_at,name,archived)
                VALUES (?,?,?,?,?,?,?)''', (thread,source,'/tmp/'+thread,str(log),
                int(time.time()),'会话-'+thread,archived))
        return str(log)

    def test_status_lists_all_projects_and_filters_finished_stale_and_children(self):
        paths = {self.status_task('running'+str(i)) for i in range(7)}
        paths.add(self.status_task('completed', ('task_started','task_complete')))
        paths.add(self.status_task('aborted', ('task_started','turn_aborted')))
        paths.add(self.status_task('child', source='{"subagent":{}}'))
        paths.add(self.status_task('archived', archived=1))
        self.status_task('crashed')  # No live process owns this unfinished record.
        self.cfg['projects']['项目别名'] = '/tmp/running0'
        with patch.object(b, 'open_rollout_paths', return_value=paths):
            b.accept(self.cfg, self.update(90, '/status'))
        answer = self.rows('outbox')[0]['text']
        self.assertIn('7 个未结束任务',answer)
        self.assertIn('项目别名｜会话-running0',answer)
        self.assertIn('已运行 2 分钟',answer)
        for thread in ('completed','aborted','child','archived'):
            self.assertNotIn('会话-'+thread, answer)
        self.assertIn('异常中断',answer)
        self.assertIn('会话-crashed',answer)
        self.assertEqual(self.rows('jobs'), [])

    def test_status_merges_telegram_job_and_includes_starting_job_without_prompt(self):
        path = self.status_task('bot',source='exec')
        with b.db() as c:
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(1,'demo','SECRET','running','bot'))
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(2,'other','SECRET','queued',None))
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(3,'past','SECRET','turn-ended','past'))
            b.queue(c,'old','result')
            c.execute("UPDATE outbox SET state='sent'")
            with patch.object(b,'open_rollout_paths',return_value={path}):
                answer = b.status_text(c,self.cfg)
        self.assertIn('2 个未结束任务',answer)
        self.assertEqual(answer.count('会话-bot'),1)
        self.assertIn('运行中 · Telegram',answer)
        self.assertIn('等待启动 · Telegram',answer)
        self.assertIn('已发送 1 条',answer)
        self.assertNotIn('past',answer)
        self.assertNotIn('SECRET',answer)

    def test_status_empty_and_failure_are_distinct(self):
        with b.db() as c:
            with patch.object(b,'open_rollout_paths',return_value=set()):
                answer = b.status_text(c,self.cfg)
            self.assertIn('当前没有正在运行的任务',answer)
            for error in (OSError(), b.subprocess.TimeoutExpired('lsof',5), sqlite3.OperationalError()):
                with patch.object(b,'running_threads',side_effect=error):
                    answer = b.status_text(c,self.cfg)
                self.assertIn('暂时无法读取',answer)
                self.assertNotIn('没有正在运行',answer)

    def test_status_unknown_record_is_not_reported_as_idle(self):
        path = self.status_task('unknown',kinds=())
        with b.db() as c, patch.object(b,'open_rollout_paths',return_value={path}):
            answer = b.status_text(c,self.cfg)
        self.assertIn('1 个会话暂时无法确认状态',answer)
        self.assertNotIn('没有正在运行',answer)

    def test_task_lifecycle_handles_long_turn_and_incomplete_tail(self):
        path = self.status_task('long')
        with open(path,'a') as f:
            f.write(json.dumps({'type':'response_item','payload':{'text':'x'*100000}})+'\n')
            f.write('{"type":"event_msg","payload":{"type":"task_complete"}}')
        self.assertEqual(b.thread_activity({'rollout_path':path}),'active')
        with open(path,'a') as f: f.write('\n')
        self.assertEqual(b.thread_activity({'rollout_path':path}),'idle')

    def test_process_inspection_parses_only_rollouts_and_reports_errors(self):
        self.live_patch.stop()
        result = b.subprocess.CompletedProcess([],0,'p123\nn/tmp/a.jsonl\nn/tmp/db.sqlite\nn/tmp/b.jsonl\n','')
        with patch.object(b.subprocess,'run',return_value=result) as call:
            self.assertEqual(b.open_rollout_paths(),{'/tmp/a.jsonl','/tmp/b.jsonl'})
            self.assertEqual(call.call_args.kwargs['timeout'],5)
        with patch.object(b.subprocess,'run',return_value=b.subprocess.CompletedProcess([],1,'','')):
            self.assertEqual(b.open_rollout_paths(),set())
        with patch.object(b.subprocess,'run',return_value=b.subprocess.CompletedProcess([],1,'','failure')):
            with self.assertRaises(OSError): b.open_rollout_paths()

    def test_large_status_is_split_without_losing_tasks(self):
        rows = [{'id':'thread-'+str(i),'cwd':'/tmp/demo','name':'会话'+str(i)+'很长的名称'*20,
                 'source':'vscode','started_at':None} for i in range(30)]
        with patch.object(b,'running_threads',return_value=(rows,0)):
            b.accept(self.cfg,self.update(90,'/status'))
        messages = self.rows('outbox')
        self.assertGreater(len(messages),1)
        self.assertIn('30 个未结束任务',''.join(r['text'] for r in messages))
        for i in range(30):
            self.assertIn('会话'+str(i)+'很长',''.join(r['text'] for r in messages))
        self.assertTrue(all(len(r['text'].encode('utf-16-le'))//2<=4096 for r in messages))

    def action_callback(self, token, uid=100, receipt=1000, user=123):
        update = self.menu_callback(token,uid,receipt,user)
        update['callback_query']['data'] = 'action:'+token
        return update

    def mark_sent(self, key, receipt=1000):
        with b.db() as c:
            c.execute("UPDATE outbox SET state='sent',receipt=? WHERE id=?",(receipt,key))

    def test_project_buttons_paginate_search_and_select_without_executing(self):
        self.cfg['projects'] = {f'project-{i}':'/tmp/'+str(i) for i in range(12)}
        b.accept(self.cfg,self.update(1,'/use'))
        self.mark_sent('reply:1')
        actions = self.rows('actions')
        self.assertEqual(sum(r['kind']=='project' for r in actions),8)
        next_page = next(r for r in actions if r['kind']=='projects')
        b.accept(self.cfg,self.action_callback(next_page['token']))
        self.mark_sent('navigation:cb100',1001)
        projects = [r for r in self.rows('actions') if r['menu_id']=='navigation:cb100' and r['kind']=='project']
        self.assertEqual(len(projects),4)
        choice = next(r for r in projects if json.loads(r['data'])['project']=='project-10')
        b.accept(self.cfg,self.action_callback(choice['token'],101,1001))
        with b.db() as c:
            self.assertEqual(b.get(c,'project'),'project-10')
            self.assertEqual(b.get(c,'await-session:project-10'),'1')
        self.assertEqual(self.rows('jobs'),[])
        b.accept(self.cfg,self.update(102,'/projects PROJECT-10'))
        matches=[r for r in self.rows('actions') if r['menu_id']=='reply:102' and r['kind']=='project']
        self.assertEqual(len(matches),1)

    def test_navigation_rejects_foreign_forged_and_expired_callbacks(self):
        b.accept(self.cfg,self.update(1,'/projects'))
        self.mark_sent('reply:1')
        token=self.rows('actions')[0]['token']
        b.accept(self.cfg,self.action_callback(token,user=999))
        b.accept(self.cfg,self.action_callback(token,101,receipt=777))
        with b.db() as c:
            self.assertEqual(b.get(c,'project'),'')
            c.execute('UPDATE actions SET expires=0')
        b.accept(self.cfg,self.action_callback(token,102))
        with b.db() as c: self.assertEqual(b.get(c,'project'),'')
        self.assertEqual(self.rows('jobs'),[])

    def test_session_pages_search_and_old_project_buttons(self):
        for i in range(8):
            self.status_task(str(i),kinds=('task_complete',))
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute("UPDATE threads SET cwd='/tmp/demo',updated_at=CAST(id AS INTEGER)")
        b.accept(self.cfg,self.update(1,'/use demo'))
        self.mark_sent('reply:1')
        next_page=next(r for r in self.rows('actions') if r['kind']=='sessions')
        b.accept(self.cfg,self.action_callback(next_page['token']))
        choices={r['thread'] for r in self.rows('choices') if r['menu_id']=='navigation:cb100'}
        self.assertEqual(choices,{'0','1','2',''})
        b.accept(self.cfg,self.update(101,'/sessions 会话-1'))
        choices={r['thread'] for r in self.rows('choices') if r['menu_id']=='reply:101'}
        self.assertEqual(choices,{'1',''})
        with b.db() as c: b.put(c,'project','other')
        self.assertIn('菜单已更新',b.accept(self.cfg,self.action_callback(next_page['token'],102)))

    def test_current_reports_selection_and_new_mode_without_mutation(self):
        token=self.fixture_menu()
        b.accept(self.cfg,self.update(2,'/current'))
        self.assertIn('等待选择',self.rows('outbox')[-1]['text'])
        b.accept(self.cfg,self.menu_callback(token))
        b.accept(self.cfg,self.update(21,'/current'))
        text=self.rows('outbox')[-1]['text']
        self.assertIn('会话 ID：original',text)
        self.assertIn('gpt-6-astra',text)
        self.assertIn('high',text)
        b.accept(self.cfg,self.update(22,'/new'))
        b.accept(self.cfg,self.update(23,'/current'))
        self.assertIn('新建会话模式',self.rows('outbox')[-1]['text'])
        self.assertEqual(self.rows('jobs'),[])

    def test_current_distinguishes_starting_bot_job_from_new_mode(self):
        self.original_task('bot-task')
        self.cfg['projects']['demo']=str(b.ROOT)
        with b.db() as c:
            b.put(c,'project','demo')
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(10,'demo','SECRET','queued',''))
            self.assertIn('等待启动',b.current_text(c,self.cfg))
            c.execute("UPDATE jobs SET thread='bot-task',state='running' WHERE id=10")
            self.assertIn('会话 ID：bot-task',b.current_text(c,self.cfg))
            c.execute('INSERT INTO reply_jobs VALUES (?,?,?)',(10,'bot-task',str(b.ROOT)))
            self.assertIn('新建会话模式',b.current_text(c,self.cfg))

    def test_last_reads_only_complete_final_result_and_reply_routes_exact_thread(self):
        token=self.fixture_menu()
        b.accept(self.cfg,self.menu_callback(token))
        meta=b.thread_metadata('original')
        result='完整结果😀'*1000
        events=[{'type':'event_msg','timestamp':'2026-09-30T12:00:00Z','payload':{
            'type':'task_complete','turn_id':'final-turn','last_agent_message':result}},
            {'type':'event_msg','payload':{'type':'user_message','message':'SECRET-PROMPT'}},
            {'type':'response_item','payload':{'type':'reasoning','text':'SECRET-THOUGHT'}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':'next'}}]
        Path(meta['rollout_path']).write_text(''.join(json.dumps(e)+'\n' for e in events))
        b.accept(self.cfg,self.update(21,'/last'))
        rows=[r for r in self.rows('outbox') if r['id'].startswith('last:original:21')]
        self.assertGreater(len(rows),1)
        combined=''.join(r['text'].split('\n',1)[1] for r in rows)
        self.assertIn(result,combined)
        self.assertNotIn('SECRET',combined)
        self.mark_sent(rows[-1]['id'],1002)
        with b.db() as c:
            self.assertEqual(b.resolve_reply(c,self.cfg,self.reply_update(receipt=1002)['message'])['id'],'original')
        self.assertEqual(self.rows('jobs'),[])

    def test_last_reply_takes_priority_and_unknown_reply_never_falls_back(self):
        token=self.fixture_menu()
        b.accept(self.cfg,self.menu_callback(token))
        meta=self.original_task('second')
        Path(meta['rollout_path']).write_text(json.dumps({'type':'event_msg','payload':{
            'type':'task_complete','last_agent_message':'SECOND-RESULT'}})+'\n')
        self.mark_sent('notice:second:turn',9001)
        u=self.reply_update(21,9001);u['message']['text']='/last'
        b.accept(self.cfg,u)
        self.assertIn('SECOND-RESULT',self.rows('outbox')[-1]['text'])
        u=self.reply_update(22,9999);u['message']['text']='/last'
        b.accept(self.cfg,u)
        self.assertIn('没有关联',self.rows('outbox')[-1]['text'])
        with b.db() as c: self.assertEqual(b.get(c,'selected:demo'),'original')

    def test_last_missing_record_does_not_send_unrelated_history(self):
        token=self.fixture_menu(); b.accept(self.cfg,self.menu_callback(token))
        Path(b.thread_metadata('original')['rollout_path']).unlink()
        b.accept(self.cfg,self.update(21,'/last'))
        self.assertIn('暂未找到',self.rows('outbox')[-1]['text'])
        self.assertEqual(self.rows('jobs'),[])

    def test_health_records_recovery_and_excludes_credentials(self):
        b.record_health('poll',error=True)
        b.record_health('observer')
        self.cfg['proxy']='http://SECRET-PROXY'
        with b.db() as c: self.assertIn('最近遇到异常',b.health_text(c,self.cfg))
        b.record_health('poll')
        b.accept(self.cfg,self.update(1,'/health'))
        text=self.rows('outbox')[-1]['text']
        self.assertIn('服务在线',text)
        self.assertNotIn('最近遇到异常',text)
        self.assertNotIn('SECRET',text)
        with patch.object(b,'db',side_effect=sqlite3.OperationalError('locked')):
            b.record_health('poll',error=True)  # Diagnostics cannot terminate polling.

    def test_cancel_empty_and_scope_do_not_target_future_jobs(self):
        b.accept(self.cfg,self.update(1,'/cancel'))
        self.assertIn('没有可停止',self.rows('outbox')[-1]['text'])
        with b.db() as c:
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(10,'demo','SECRET','running','original'))
        b.accept(self.cfg,self.update(2,'/cancel'))
        with b.db() as c:
            self.assertEqual(b.get(c,'cancel-job'),'10')
            c.execute("UPDATE jobs SET state='cancelled',prompt='' WHERE id=10")
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(11,'demo','NEW','running','next'))
            self.assertNotEqual(b.get(c,'cancel-job'),'11')

    def test_worker_does_not_apply_previous_cancellation_to_next_job(self):
        self.cfg['projects']['demo']=str(b.ROOT)
        class StopWorker(Exception): pass
        class Process:
            pid=100000
            def __init__(self,thread,code=0):
                self.stdin=io.StringIO();self.code=code
                self.stderr=io.StringIO()
                events=[{'type':'thread.started','thread_id':thread},
                        {'type':'item.completed','item':{'type':'agent_message','text':'RESULT'}}]
                if not code: events.append({'type':'turn.completed'})
                self.stdout=io.StringIO(''.join(json.dumps(e)+'\n' for e in events))
            def poll(self): return self.code
            def wait(self,**kw): return self.code
        with b.db() as c:
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(10,'demo','one','queued',''))
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(11,'demo','two','queued',''))
            b.put(c,'cancel-job',10)
        processes=[Process('first',-15),Process('awake'),Process('second'),Process('awake')]
        with patch.object(b.subprocess,'Popen',side_effect=processes),patch.object(b.time,'sleep',side_effect=StopWorker):
            with self.assertRaises(StopWorker): b.worker(self.cfg)
        jobs={r['id']:r for r in self.rows('jobs')}
        self.assertEqual(jobs[10]['state'],'cancelled')
        self.assertEqual(jobs[11]['state'],'turn-ended')
        self.assertEqual(jobs[11]['thread'],'second')
        self.assertTrue(all(not r['prompt'] for r in jobs.values()))

    def test_bot_addressed_commands_and_foreign_bot_commands(self):
        self.cfg['bot_username']='example_bot'
        b.accept(self.cfg,self.update(1,'/CURRENT@example_bot'))
        self.assertIn('尚未选择项目',self.rows('outbox')[-1]['text'])
        count=len(self.rows('outbox'))
        b.accept(self.cfg,self.update(2,'/new@other_bot'))
        self.assertEqual(len(self.rows('outbox')),count)
        self.assertEqual(self.rows('jobs'),[])

    def test_notification_includes_conversation_name(self):
        meta=self.original_task()
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute("UPDATE threads SET name='准确会话名' WHERE id='original'")
        b.notify({'type':'agent-turn-complete','thread-id':'original','turn-id':'new',
                  'cwd':'/tmp/demo','last-assistant-message':'结果'})
        self.assertIn('demo · 准确会话名',self.rows('outbox')[-1]['text'])

if __name__ == '__main__': unittest.main()
