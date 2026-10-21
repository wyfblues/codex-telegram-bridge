"""Offline phone-control integration; no network, subprocess, or live state."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest.mock import Mock, patch

import codex_control
import telegram_controls as controls
import test_reliability as support

b = support.b


class PhoneControlsTests(unittest.TestCase):
    update = support.ReliabilityTests.update
    rows = support.ReliabilityTests.rows
    make_thread = support.ReliabilityTests.make_thread
    append_event = support.ReliabilityTests.append_event
    select_default = support.ReliabilityTests.select_default
    final_payload = support.ReliabilityTests.final_payload

    def setUp(self):
        support.ReliabilityTests.setUp(self)
        self.cfg['remote_controls_enabled'] = True
        self.config_patch = patch.object(b, 'config', return_value=self.cfg)
        self.config_patch.start()
        self.client = Mock()
        self.client.read_settings.return_value = {'model': 'gpt-6-astra', 'reasoningEffort': 'high',
            'modelProvider': 'openai', 'mode': 'default', 'resumeState': 'loaded'}
        self.client.update_settings.return_value = {'applied': True}
        self.client.steer.return_value = {}
        self.client.interrupt.return_value = {}
        self.catalog = {'data': [
            {'id': 'gpt-6-astra', 'model': 'gpt-6-astra', 'displayName': 'GPT Astra',
             'defaultReasoningEffort': 'high', 'supportedReasoningEfforts': [
                 {'reasoningEffort': effort} for effort in ('low','high','max','ultra')],
             'serviceTiers': [{'id': 'priority', 'name': 'Fast', 'description': 'Faster'}]},
            {'id': 'gpt-6-sol', 'model': 'gpt-6-sol', 'displayName': 'GPT Sol',
             'defaultReasoningEffort': 'low', 'supportedReasoningEfforts': [
                 {'reasoningEffort': effort} for effort in ('low','high')]},
        ], 'nextCursor': None}
        self.query = Mock()
        self.query.rpc.side_effect = self.query_result

    def tearDown(self):
        self.config_patch.stop()
        support.ReliabilityTests.tearDown(self)

    def query_result(self, method, params):
        if method == 'model/list':
            return self.catalog
        if method == 'account/rateLimits/read':
            return {'rateLimits': None, 'rateLimitsByLimitId': {}}
        raise AssertionError(method)

    def process(self):
        return controls.deliver_next(b.__dict__, self.cfg, self.client, self.query)

    def accept(self, update_id, text, **kwargs):
        update = self.update(update_id, text, **kwargs)
        b.accept(self.cfg, update)
        return update

    def quoted(self, receipt):
        return {'message_id': receipt, 'chat': {'id': 123, 'type': 'private'},
                'from': {'id': 456, 'is_bot': True}}

    def callback(self, action, callback_id='button-1', receipt=70, user=123):
        return {'id': callback_id, 'data': action,
                'from': {'id': user, 'is_bot': False},
                'message': self.quoted(receipt)}

    def sent_panel(self, update_id=100, receipt=70, source='vscode'):
        self.make_thread(source=source)
        self.select_default()
        self.accept(update_id, '/settings')
        self.assertTrue(self.process())
        request = self.rows('control_requests')[0]
        with b.db() as c:
            c.execute("UPDATE outbox SET state='sent',receipt=? WHERE id=?", (receipt, request['reply_key']))
        return request

    def active(self, source='vscode'):
        meta = self.make_thread(kind='task_started', source=source)
        self.select_default()
        return meta

    def test_unauthorized_stale_and_duplicate_commands_never_duplicate_controls(self):
        self.make_thread()
        self.select_default()
        for command in ('/settings', '/model gpt-6-sol', '/steer update', '/stop', '/usage'):
            for kwargs in ({'user': 99}, {'chat': -1, 'kind': 'group'}, {'age': 1000}):
                self.accept(100 + len(self.rows('inbox')), command, **kwargs)
        self.assertEqual(self.rows('control_requests'), [])
        self.accept(200, '/settings')
        self.accept(200, '/settings')
        self.assertEqual(len(self.rows('control_requests')), 1)
        self.assertEqual(self.rows('jobs'), [])
        self.client.update_settings.assert_not_called()

    def test_feature_disabled_and_execution_disabled_refuse_writes(self):
        self.make_thread()
        self.select_default()
        self.cfg['remote_controls_enabled'] = False
        self.accept(100, '/settings')
        self.assertEqual(self.rows('control_requests'), [])
        self.cfg.update(remote_controls_enabled=True, execution_enabled=False)
        self.accept(101, '/model gpt-6-sol')
        self.accept(102, '/stop')
        self.assertEqual(self.rows('control_requests'), [])
        self.client.update_settings.assert_not_called()

    def test_reply_binds_exact_thread_and_selection_does_not_change_it(self):
        self.make_thread('original', source='vscode')
        self.make_thread('other', source='vscode')
        self.select_default('other')
        with b.db() as c:
            b.queue(c, 'notice:original:old-turn', 'Old reply')
            c.execute("UPDATE outbox SET state='sent',receipt=81 WHERE id='notice:original:old-turn'")
        update = self.update(100, '/settings')
        update['message']['reply_to_message'] = self.quoted(81)
        b.accept(self.cfg, update)
        self.select_default('other')
        self.assertTrue(self.process())
        self.client.read_settings.assert_called_once_with('original')
        request = self.rows('control_requests')[0]
        self.assertEqual(request['thread'], 'original')
        with b.db() as c:
            self.assertEqual(b.get(c, 'thread:demo'), 'other')

    def test_unverified_reply_does_not_fall_back_to_selected_thread(self):
        self.make_thread(source='vscode')
        self.select_default()
        update = self.update(100, '/model gpt-6-sol')
        update['message']['reply_to_message'] = self.quoted(9999)
        b.accept(self.cfg, update)
        self.assertEqual(self.rows('control_requests'), [])
        self.client.read_settings.assert_not_called()

    def test_settings_menu_requires_exact_bot_receipt_owner_and_fresh_action(self):
        self.sent_panel()
        action = self.rows('actions')[0]
        token = 'action:' + action['token']
        with b.db() as c:
            self.assertIn('无权', b.select_callback(c, self.cfg, self.callback(token, user=999)))
            self.assertIn('最新按钮', b.select_callback(c, self.cfg, self.callback(token, receipt=999)))
            self.assertEqual(len(c.execute('SELECT * FROM control_requests').fetchall()), 1)
            c.execute('UPDATE actions SET expires=? WHERE token=?', (time.time()-1, action['token']))
            self.assertIn('过期', b.select_callback(c, self.cfg, self.callback(token)))
        self.assertEqual(len(self.rows('control_requests')), 1)

    def test_settings_button_targets_original_after_selection_changes_and_deduplicates(self):
        self.sent_panel()
        self.make_thread('other', source='vscode')
        self.select_default('other')
        action = next(row for row in self.rows('actions')
                      if json.loads(row['data']).get('options') == {'effort': 'ultra'})
        callback = self.callback('action:' + action['token'])
        with b.db() as c:
            self.assertIn('已接收', b.select_callback(c, self.cfg, callback))
            self.assertIn('已接收', b.select_callback(c, self.cfg, callback))
        self.assertEqual(len(self.rows('control_requests')), 2)
        self.process()
        self.client.update_settings.assert_called_once()
        self.assertEqual(self.client.update_settings.call_args.args, ('original',))
        self.assertEqual(self.client.update_settings.call_args.kwargs['effort'], 'ultra')
        self.assertEqual(self.rows('runtime_options')[0]['thread'], 'original')
        with b.db() as c:
            self.assertEqual(b.get(c,'thread:demo'), 'other')

    def test_cli_model_effort_fast_are_saved_per_thread_without_global_or_desktop_writes(self):
        original = self.make_thread(source='exec')
        other = self.make_thread('other')
        self.select_default()
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute("UPDATE threads SET approval_mode='on-request',sandbox_policy=? WHERE id='original'",
                      (json.dumps({'type':'read-only'}),))
        original = b.thread_metadata('original')
        original_args, other_args = b.resume_arguments(original), b.resume_arguments(other)
        for uid, command, expected in (
                (100,'/reasoning ultra',{'model':'gpt-6-astra','effort':'ultra'}),
                (101,'/fast on',{'model':'gpt-6-astra','effort':'ultra','serviceTier':'priority'}),
                (102,'/model gpt-6-sol',{'model':'gpt-6-sol','effort':'low'})):
            self.accept(uid, command)
            self.assertTrue(self.process())
            self.assertEqual(json.loads(self.rows('runtime_options')[0]['options']),expected)
        self.client.update_settings.assert_not_called()
        self.assertEqual(len(self.rows('runtime_options')), 1)
        saved = json.loads(self.rows('runtime_options')[0]['options'])
        self.assertEqual(saved, {'model': 'gpt-6-sol', 'effort': 'low'})
        arguments = b.resume_arguments(original)
        self.assertEqual(arguments[arguments.index('-m')+1], 'gpt-6-sol')
        self.assertIn('model_reasoning_effort="low"', arguments)
        self.assertIn('approval_policy="on-request"', arguments)
        self.assertIn('sandbox_mode="read-only"', arguments)
        self.assertIn('model_provider="openai"', arguments)
        self.assertEqual(b.resume_arguments(other), other_args)
        self.assertNotEqual(arguments, original_args)
        self.assertFalse((b.ROOT/'config.toml').exists())
        self.assertFalse((b.ROOT/'settings.json').exists())

    def test_unsupported_model_effort_and_speed_fail_without_saving(self):
        self.make_thread(source='vscode')
        self.select_default()
        for uid, command in ((100,'/model no-model'), (101,'/reasoning enormous'), (102,'/fast invalid')):
            self.accept(uid, command)
            self.process()
        self.assertEqual(self.rows('runtime_options'), [])
        self.client.update_settings.assert_not_called()
        self.assertTrue(all(row['state'] == 'failed' for row in self.rows('control_requests')))

    def test_busy_settings_are_refused_before_enqueue_and_again_before_write(self):
        meta = self.active()
        self.accept(100, '/reasoning ultra')
        self.assertEqual(self.rows('control_requests'), [])
        self.append_event(meta['rollout_path'], 'baseline', kind='task_complete')
        self.accept(101, '/reasoning ultra')
        self.append_event(meta['rollout_path'], 'new-turn', kind='task_started')
        self.assertTrue(self.process())
        self.assertEqual(self.rows('control_requests')[0]['state'], 'failed')
        self.client.update_settings.assert_not_called()
        self.assertEqual(self.rows('runtime_options'), [])

    def test_desktop_settings_compare_guard_failure_does_not_persist(self):
        self.make_thread(source='vscode')
        self.select_default()
        self.client.update_settings.return_value = {'applied': False}
        self.accept(100, '/reasoning ultra')
        self.process()
        self.assertEqual(self.rows('runtime_options'), [])
        self.assertEqual(self.rows('control_requests')[0]['state'], 'failed')
        self.assertEqual(self.client.update_settings.call_args.kwargs['expected'],
                         {'model': 'gpt-6-astra', 'reasoningEffort': 'high'})

    def test_steer_uses_captured_turn_and_never_creates_a_job(self):
        self.active()
        self.accept(100, '/steer Add tests before continuing')
        request = self.rows('control_requests')[0]
        self.assertEqual(request['turn'], 'baseline')
        with patch.object(b, 'thread_activity', return_value='active'):
            self.process()
        self.client.steer.assert_called_once_with('original', 'baseline', 'Add tests before continuing', request_id='100')
        self.assertEqual(self.rows('jobs'), [])
        self.assertEqual(self.rows('control_requests')[0]['args'], '{}')
        self.assertNotIn('Add tests', json.dumps(self.rows('outbox')))

    def test_stale_turn_and_request_age_refuse_network_mutation(self):
        meta = self.active()
        self.accept(100, '/stop')
        self.append_event(meta['rollout_path'], 'next-turn', kind='task_started')
        with patch.object(b, 'thread_activity', return_value='active'):
            self.process()
        self.client.interrupt.assert_not_called()
        self.accept(101, '/stop')
        with b.db() as c:
            c.execute('UPDATE control_requests SET created=? WHERE id=?', (time.time()-901,'101'))
        self.process()
        self.client.interrupt.assert_not_called()
        self.assertTrue(all(row['state'] == 'failed' for row in self.rows('control_requests')))

    def test_desktop_stop_is_exact_turn_and_does_not_stop_cli_worker(self):
        self.active()
        self.accept(100, '/stop')
        with patch.object(b, 'thread_activity', return_value='active'), patch.object(b, 'cancel_job') as cancel:
            self.process()
        self.client.interrupt.assert_called_once_with('original', 'baseline')
        cancel.assert_not_called()
        self.assertIn('最终停止状态以进度卡片为准', self.rows('outbox')[0]['text'])

    def test_cli_worker_stop_sets_only_matching_job_cancel_marker(self):
        self.active(source='exec')
        with b.db() as c:
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', (90,'demo','Private task','running','original'))
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', (91,'demo','Other task','running','other'))
        self.accept(100, '/stop')
        with patch.object(b, 'thread_activity', return_value='active'):
            self.process()
        self.client.interrupt.assert_not_called()
        with b.db() as c:
            self.assertEqual(b.get(c,'cancel-job'), '90')
        self.assertEqual([row['state'] for row in self.rows('jobs')], ['running','running'])

    def test_uncertain_mutation_is_never_retried_and_prompt_is_cleared(self):
        self.active()
        self.accept(100, '/steer Sensitive extra instruction')
        self.client.steer.side_effect = codex_control.ControlError('timeout', uncertain=True)
        with patch.object(b, 'thread_activity', return_value='active'):
            self.assertTrue(self.process())
            self.assertFalse(self.process())
            with b.db() as c:
                controls.recover(c)
            self.assertFalse(self.process())
        self.client.steer.assert_called_once()
        self.assertEqual(self.rows('control_requests')[0]['state'], 'uncertain')
        self.assertEqual(self.rows('control_requests')[0]['args'], '{}')
        self.assertIn('不会自动重发', self.rows('outbox')[0]['text'])

    def test_restart_recovers_sending_and_queued_controls_without_resubmission(self):
        self.active()
        self.accept(100, '/steer Private instruction 1')
        self.accept(101, '/stop')
        with b.db() as c:
            c.execute("UPDATE control_requests SET state='sending' WHERE id='100'")
            controls.recover(c)
        self.assertEqual([row['state'] for row in self.rows('control_requests')], ['uncertain','interrupted'])
        self.assertTrue(all(row['args'] == '{}' for row in self.rows('control_requests')))
        self.assertFalse(self.process())
        self.client.steer.assert_not_called()
        self.client.interrupt.assert_not_called()

    def test_usage_unknown_is_truthful_and_query_failure_uses_stale_snapshot(self):
        self.make_thread()
        self.select_default()
        self.accept(100, '/usage')
        self.process()
        self.assertIn('未按 0% 处理', self.rows('outbox')[0]['text'])
        self.client.read_settings.assert_not_called()
        self.query.rpc.side_effect = codex_control.ControlError('unavailable')
        snapshot = {'rate_limits': {'primary': {'used_percent': 25, 'resets_at': time.time()-30}},
                    'sampled_at': time.time()-100, 'context': {'used_tokens': 100,'window_tokens':1000}}
        self.accept(101, '/usage')
        with patch.object(controls.runtime, 'safe_read_snapshot', return_value=snapshot):
            self.process()
        text = self.rows('outbox')[1]['text']
        self.assertIn('上次剩余 75%', text)
        self.assertIn('已过重置时间', text)
        self.assertIn('100 / 1,000 tokens', text)
        self.assertNotIn('SECRET', text)
        self.client.steer.assert_not_called()
        self.client.update_settings.assert_not_called()
        self.assertEqual(self.rows('jobs'), [])

    def test_deleted_target_and_changed_directory_refuse_without_reroute(self):
        self.make_thread(source='vscode')
        self.select_default()
        self.accept(100, '/model gpt-6-sol')
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute("UPDATE threads SET cwd='/different' WHERE id='original'")
        self.process()
        self.assertEqual(self.rows('control_requests')[0]['state'], 'failed')
        self.client.read_settings.assert_not_called()
        self.client.update_settings.assert_not_called()

    def test_control_response_reply_continues_original_conversation(self):
        self.make_thread(source='exec')
        self.make_thread('other')
        self.select_default()
        self.accept(100, '/usage')
        self.process()
        key = self.rows('control_requests')[0]['reply_key']
        with b.db() as c:
            c.execute("UPDATE outbox SET state='sent',receipt=91 WHERE id=?", (key,))
        self.select_default('other')
        update = self.update(101, 'Continue this exact original conversation')
        update['message']['reply_to_message'] = self.quoted(91)
        b.accept(self.cfg, update)
        self.assertEqual(self.rows('jobs')[0]['thread'], 'original')
        self.assertEqual(self.rows('reply_jobs')[0]['thread'], 'original')
        self.assertEqual(len(self.rows('control_requests')), 1)

    def test_finished_control_edits_same_known_message_without_replacement_on_uncertain_send(self):
        self.make_thread()
        self.select_default()
        self.accept(100, '/usage')
        key = self.rows('control_requests')[0]['reply_key']
        with b.db() as c:
            c.execute("UPDATE outbox SET state='sent',receipt=71 WHERE id=?", (key,))
        self.process()
        self.assertEqual(self.rows('message_edits'), [{'id': key+':result','target':71}])
        self.accept(101, '/usage')
        uncertain_key = self.rows('control_requests')[1]['reply_key']
        with b.db() as c:
            c.execute("UPDATE outbox SET state='uncertain' WHERE id=?", (uncertain_key,))
        before = len(self.rows('outbox'))
        self.process()
        self.assertEqual(len(self.rows('outbox')), before)
        self.assertEqual(len(self.rows('message_edits')), 1)

    def test_settings_result_during_initial_send_waits_then_edits_exact_receipt(self):
        self.make_thread(source='vscode')
        self.select_default()
        self.accept(100,'/settings')
        key = self.rows('control_requests')[0]['reply_key']
        with b.db() as c:
            c.execute("UPDATE outbox SET state='sending' WHERE id=?",(key,))
        self.process()
        self.assertEqual(self.rows('control_requests')[0]['state'],'done')
        self.assertEqual(self.rows('control_results')[0]['delivered'],0)
        self.assertEqual(self.rows('message_edits'),[])
        with b.db() as c:
            c.execute("UPDATE outbox SET state='sent',receipt=71 WHERE id=?",(key,))
        controls.flush_results(b.__dict__)
        self.assertEqual(self.rows('message_edits'),[{'id':key+':result','target':71}])
        self.assertTrue(all(row['menu_id']==key+':result' for row in self.rows('actions')))
        api = Mock()
        api.call.return_value = {'message_id':71}
        self.assertTrue(b.deliver(api,self.cfg))
        self.assertEqual(api.call.call_args.args,('editMessageText',))
        self.assertEqual(api.call.call_args.kwargs['message_id'],71)
        with b.db() as c:
            action = c.execute('SELECT * FROM actions LIMIT 1').fetchone()
            answer = b.select_callback(c,self.cfg,self.callback('action:'+action['token'],receipt=71))
        self.assertIn('已接收',answer)
        self.assertEqual(len(self.rows('control_requests')),2)

    def test_recovery_status_replaces_pending_ack_without_replaying_commands(self):
        self.active()
        self.accept(100,'/steer Private instruction')
        with b.db() as c:
            c.execute("UPDATE control_requests SET state='sending' WHERE id='100'")
            controls.recover(c)
        controls.flush_results(b.__dict__)
        text = self.rows('outbox')[0]['text']
        self.assertIn('不会自动',text)
        self.assertNotIn('正在处理',text)
        self.assertFalse(self.process())
        self.client.steer.assert_not_called()

    def test_concurrent_result_flushers_cannot_republish_a_delivered_menu(self):
        self.make_thread(source='vscode')
        self.select_default()
        self.accept(100, '/settings')
        request = self.rows('control_requests')[0]
        key, result_key = request['reply_key'], request['reply_key'] + ':result'
        with b.db() as c:
            button = b.action_button(c, key, 'Refresh', 'runtime-control',
                                     thread='original', cwd=str(b.ROOT), operation='settings')
            c.execute("UPDATE control_requests SET state='done',args='{}' WHERE id='100'")
            c.execute("UPDATE outbox SET state='sent',receipt=71 WHERE id=?", (key,))
            c.execute('INSERT INTO control_results VALUES (?,?,?,0)',
                      ('100', 'Completed settings panel', json.dumps({'inline_keyboard': [[button]]})))

        first_snapshot = threading.Event()
        release_first = threading.Event()
        second_attempt = threading.Event()
        stale_snapshot = threading.Event()
        errors, flushed_by = [], []
        original_db, original_flush = b.db, controls._flush_result

        @contextmanager
        def observed_db():
            with original_db() as c:
                def trace(sql):
                    if threading.current_thread().name != 'second-result-flusher':
                        return
                    statement = ' '.join(sql.upper().split())
                    if statement.startswith('BEGIN IMMEDIATE'):
                        # SQLite calls its trace hook before acquiring the lock.
                        second_attempt.set()
                    elif statement.startswith('SELECT R.*') and 'CONTROL_RESULTS' in statement:
                        if not release_first.is_set():
                            stale_snapshot.set()
                        second_attempt.set()
                c.set_trace_callback(trace)
                yield c

        def hold_first_snapshot(context, c, result):
            name = threading.current_thread().name
            flushed_by.append(name)
            if name == 'first-result-flusher':
                first_snapshot.set()
                if not release_first.wait(3):
                    raise AssertionError('The competing flusher did not reach the database')
            original_flush(context, c, result)

        def flush():
            try:
                controls.flush_results(b.__dict__)
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=flush, name='first-result-flusher')
        second = threading.Thread(target=flush, name='second-result-flusher')
        with patch.object(b, 'db', observed_db), \
                patch.object(controls, '_flush_result', hold_first_snapshot):
            try:
                first.start()
                self.assertTrue(first_snapshot.wait(3))
                second.start()
                self.assertTrue(second_attempt.wait(3))
                self.assertFalse(stale_snapshot.is_set(),
                                 'A second flusher read a result before the first transaction completed')
            finally:
                release_first.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(flushed_by, ['first-result-flusher'])
        self.assertEqual(self.rows('control_results')[0]['delivered'], 1)
        self.assertEqual(self.rows('message_edits'), [{'id': result_key, 'target': 71}])

        api = Mock()
        api.call.return_value = {'message_id': 71}
        self.assertTrue(b.deliver(api, self.cfg))
        self.assertFalse(b.deliver(api, self.cfg))
        self.assertEqual(api.call.call_count, 1)
        with b.db() as c:
            self.assertTrue(b.valid_menu(c, result_key, 71))

    def test_idle_settings_change_refuses_existing_queued_job(self):
        self.make_thread()
        self.select_default()
        with b.db() as c:
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(99,'demo','Task','queued','original'))
        self.accept(100,'/reasoning ultra')
        self.assertEqual(self.rows('control_requests'),[])
        self.client.update_settings.assert_not_called()

    def test_unknown_desktop_speed_is_displayed_as_unknown(self):
        self.sent_panel()
        self.assertIn('速度档位：未知（沿用原会话）',self.rows('outbox')[0]['text'])
        self.assertNotIn('速度档位：标准',self.rows('outbox')[0]['text'])

    def test_final_result_quotes_exact_incoming_task_message(self):
        self.make_thread(source='exec')
        self.select_default()
        self.cfg['progress_cards_enabled'] = True
        update = self.update(100,'Inspect this exact task')
        update['message']['message_id'] = 501
        b.accept(self.cfg,update)
        with b.db() as c:
            b.progress_update_job(c,100,turn='finished-turn',state='running')
        b.notify(self.final_payload('original','finished-turn','Final answer for this task'))
        quotes = self.rows('outbox_replies')
        self.assertEqual(quotes,[{'id':'notice:original:finished-turn','receipt':501}])
        api = Mock()
        api.call.return_value = {'message_id':71}
        while b.deliver(api,self.cfg):
            pass
        final_call = next(call for call in api.call.call_args_list
                          if 'Final answer for this task' in call.kwargs.get('text',''))
        self.assertEqual(final_call.kwargs['reply_parameters'],
                         {'message_id':501,'allow_sending_without_reply':True})


if __name__ == '__main__':
    unittest.main()
