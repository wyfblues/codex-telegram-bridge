"""Offline one-message progress, routing, delivery races, and image regressions."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch

import test_reliability as support

b = support.b
PNG = b'\x89PNG\r\n\x1a\n' + b'progress-image-regression'


class ProgressIntegrationTests(unittest.TestCase):
    tearDown = support.ReliabilityTests.tearDown
    rows = support.ReliabilityTests.rows
    update = support.ReliabilityTests.update
    make_thread = support.ReliabilityTests.make_thread
    append_event = support.ReliabilityTests.append_event
    select_default = support.ReliabilityTests.select_default
    final_payload = support.ReliabilityTests.final_payload

    def setUp(self):
        support.ReliabilityTests.setUp(self)
        self.cfg['progress_cards_enabled'] = True
        self.now = time.time()
        self.meta = self.make_thread('original', source='exec')
        self.select_default('original')
        self.save_settings()
        with b.db() as c:
            b.put(c, 'progress_since', self.now - 1)
        self.api = Mock()
        self.api.call.return_value = {'message_id': 701}

    def save_settings(self):
        (b.ROOT / 'settings.json').write_text(json.dumps(self.cfg))

    def stamp(self, offset=0):
        return b.datetime.fromtimestamp(self.now + offset, b.ZoneInfo('UTC')).isoformat()

    def lifecycle(self, kind='task_started', turn='current', offset=0, **extra):
        return {'type': 'event_msg', 'timestamp': self.stamp(offset),
                'payload': dict({'type': kind, 'turn_id': turn}, **extra)}

    def commentary(self, text='正在检查页面', turn='current', offset=1, **extra):
        payload = {'type': 'message', 'role': 'assistant', 'phase': 'commentary',
                   'content': [{'type': 'output_text', 'text': text}],
                   'internal_chat_message_metadata_passthrough': {'turn_id': turn}}
        payload.update(extra)
        return {'type': 'response_item', 'timestamp': self.stamp(offset), 'payload': payload}

    def emit(self, event, thread='original'):
        with b.db() as c:
            b.progress_event(c, self.cfg, thread, event)

    def generic_card(self, turn='current'):
        self.emit(self.lifecycle(turn=turn))
        return 'progress:original:' + turn

    def card(self, key='progress:original:current'):
        with b.db() as c:
            return dict(c.execute('SELECT * FROM progress_cards WHERE id=?', (key,)).fetchone())

    def send_initial(self, key='progress:original:current'):
        self.assertTrue(b.deliver(self.api, self.cfg))
        self.assertEqual(self.rows('outbox')[0]['receipt'], 701)
        return self.card(key)['last_sent']

    @staticmethod
    def quoted_message(receipt=701):
        return {'message_id': receipt, 'chat': {'id': 123},
                'from': {'id': 456, 'is_bot': True}}

    def test_accepted_task_reuses_one_acknowledgement_and_one_edit_target(self):
        update = self.update(100, '检查当前页面')
        b.accept(self.cfg, update)
        b.accept(self.cfg, update)
        self.assertEqual([row['id'] for row in self.rows('outbox')], ['reply:100'])
        self.assertEqual(len(self.rows('jobs')), 1)
        self.assertEqual(self.rows('progress_cards')[0]['job_id'], 100)
        sent = self.send_initial('reply:100')
        with b.db() as c:
            c.execute("UPDATE jobs SET state='running' WHERE id=100")
            b.progress_update_job(c, 100, state='running', turn='exact-turn', detail='已定位页面组件')
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 11))
        calls = self.api.call.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['sendMessage', 'editMessageText'])
        self.assertEqual(calls[1].kwargs['message_id'], 701)
        self.assertIn('已定位页面组件', calls[1].kwargs['text'])
        self.assertEqual(len(self.rows('outbox')), 1)
        self.assertEqual(self.rows('message_edits'), [])

    def test_burst_updates_coalesce_latest_detail_and_respect_ten_second_interval(self):
        self.generic_card()
        sent = self.send_initial()
        for index in range(1, 21):
            self.emit(self.commentary(f'进度步骤 {index}', offset=index))
        self.assertFalse(b.deliver_progress(self.api, self.cfg, now=sent + 9))
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 10))
        self.assertIn('进度步骤 20', self.api.call.call_args.kwargs['text'])
        self.emit(self.commentary('最新验证结果', offset=21))
        self.assertFalse(b.deliver_progress(self.api, self.cfg, now=sent + 19))
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 20))
        self.assertIn('最新验证结果', self.api.call.call_args.kwargs['text'])
        self.assertEqual([call.args[0] for call in self.api.call.call_args_list],
                         ['sendMessage', 'editMessageText', 'editMessageText'])
        self.assertEqual(len(self.rows('outbox')), 1)

    def test_only_exact_turn_public_commentary_enters_existing_card(self):
        self.generic_card()
        self.emit(self.commentary('允许展示的进度', offset=1))
        before = self.card()
        ignored = [self.commentary('WRONG TURN', turn='other-turn', offset=2),
                   self.commentary('PRIVATE USER', offset=3, role='user'),
                   self.commentary('FINAL BODY', offset=4, phase='final_answer'),
                   self.commentary('PRIVATE REASONING', offset=5, type='reasoning',
                                   summary=[{'text': 'PRIVATE REASONING'}]),
                   self.commentary('PRIVATE TOOL CALL', offset=6, type='function_call'),
                   self.commentary('PRIVATE TOOL OUTPUT', offset=7, type='function_call_output'),
                   self.commentary('NO EXPLICIT PHASE', offset=8, phase=None)]
        for event in ignored:
            self.emit(event)
        self.emit(self.commentary('OTHER THREAD', offset=9), thread='different')
        after = self.card()
        self.assertEqual(after['detail'], '允许展示的进度')
        self.assertEqual(after['version'], before['version'])
        self.assertEqual(len(self.rows('progress_cards')), 1)
        self.assertNotIn('PRIVATE', json.dumps(self.rows('progress_cards')))

    def test_old_undated_naive_and_commentary_without_start_never_create_cards(self):
        self.emit(self.lifecycle(turn='historical', offset=-60))
        for stamp in (None, 'invalid', '2026-10-15T12:00:00'):
            event = self.lifecycle(turn='invalid-' + str(stamp))
            event['timestamp'] = stamp
            self.emit(event)
        self.emit(self.commentary('An orphan commentary', turn='orphan'))
        self.emit(self.lifecycle(kind='task_complete', turn='orphan', offset=2))
        self.assertEqual(self.rows('progress_cards'), [])
        self.assertEqual(self.rows('outbox'), [])

    def test_desktop_ack_requires_matched_request_turn_before_association(self):
        desktop = self.make_thread('desktop', source='vscode')
        with patch.object(b, 'desktop_available', return_value=True), b.db() as c:
            b.enqueue_existing(c, self.cfg, 100, '检查桌面页面', desktop)
            b.queue_task_reply(c, self.cfg, 100, 'Fallback acknowledgement')
            c.execute("UPDATE jobs SET state='running' WHERE id=100")
        self.emit(self.lifecycle(turn='unrelated'), thread='desktop')
        self.assertIsNone(self.card('reply:100')['turn'])
        self.assertEqual(len(self.rows('progress_cards')), 1)
        with b.db() as c:
            c.execute("UPDATE desktop_dispatch SET turn_id='matched' WHERE job_id=100")
        self.emit(self.lifecycle(turn='matched', offset=1), thread='desktop')
        self.emit(self.commentary('原桌面任务的进展', turn='matched', offset=2), thread='desktop')
        self.assertEqual(self.card('reply:100')['turn'], 'matched')
        self.assertEqual(self.card('reply:100')['detail'], '原桌面任务的进展')
        self.assertEqual(len(self.rows('progress_cards')), 1)

    def test_cli_ack_ignores_previous_completion_and_binds_only_after_launch(self):
        b.accept(self.cfg, self.update(100, '检查原任务'))
        initial = self.card('reply:100')
        previous = self.lifecycle(kind='task_complete', turn='previous')
        previous['timestamp'] = b.datetime.fromtimestamp(initial['started'] - 0.5,
                                                        b.ZoneInfo('UTC')).isoformat()
        self.emit(previous)
        self.assertIsNone(self.card('reply:100')['turn'])
        self.assertIsNone(self.card('reply:100')['finished'])
        self.assertEqual(self.card('reply:100')['state'], 'queued')
        self.emit(self.commentary('Previous public commentary', turn='previous'))
        self.assertIsNone(self.card('reply:100')['turn'])
        launch = initial['started'] + 1
        with b.db() as c:
            c.execute("UPDATE jobs SET state='running' WHERE id=100")
            b.progress_update_job(c, 100, state='submitting', event_at=launch)
        started = self.lifecycle(turn='launched')
        started['timestamp'] = b.datetime.fromtimestamp(launch + 0.1,
                                                       b.ZoneInfo('UTC')).isoformat()
        self.emit(started)
        self.assertEqual(self.card('reply:100')['turn'], 'launched')
        self.assertEqual(self.card('reply:100')['state'], 'running')
        self.assertEqual(len(self.rows('progress_cards')), 1)

    def test_scanner_partial_record_and_rollout_move_do_not_duplicate_cards(self):
        b.baseline_completions()
        path = Path(self.meta['rollout_path'])
        with path.open('a') as stream:
            stream.write(json.dumps(self.lifecycle()) + '\n')
            partial = json.dumps(self.commentary('完整记录之后才展示'))
            stream.write(partial)
        b.scan_completions()
        self.assertEqual(self.card()['detail'], '')
        with path.open('a') as stream:
            stream.write('\n')
        b.scan_completions()
        self.assertEqual(self.card()['detail'], '完整记录之后才展示')
        destination = b.ROOT / 'archived_sessions' / path.name
        destination.parent.mkdir()
        path.rename(destination)
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(destination), 'original'))
        b.scan_completions()
        with destination.open('a') as stream:
            stream.write(json.dumps(self.commentary('移动记录后的新进展', offset=2)) + '\n')
        b.scan_completions()
        b.scan_completions()
        self.assertEqual(self.card()['detail'], '移动记录后的新进展')
        self.assertEqual(len(self.rows('progress_cards')), 1)
        self.assertEqual([row['id'] for row in self.rows('outbox')], ['progress:original:current'])

    def test_reply_to_edited_progress_receipt_preserves_original_thread(self):
        self.generic_card()
        sent = self.send_initial()
        self.emit(self.lifecycle(kind='task_complete', offset=2))
        b.deliver_progress(self.api, self.cfg, now=sent + 11)
        self.make_thread('other', source='exec')
        self.select_default('other')
        update = self.update(100, '继续原来的任务')
        update['message']['reply_to_message'] = self.quoted_message()
        b.accept(self.cfg, update)
        self.assertEqual(self.rows('jobs')[0]['thread'], 'original')
        with b.db() as c:
            self.assertEqual(b.get(c, 'thread:demo'), 'other')
        self.assertEqual(self.api.call.call_args.kwargs['message_id'], 701)

    def test_uncertain_initial_send_never_creates_or_edits_a_replacement_card(self):
        self.generic_card()
        self.api.call.side_effect = TimeoutError('simulated uncertain initial delivery')
        self.assertTrue(b.deliver(self.api, self.cfg))
        self.emit(self.commentary('后续进度'))
        self.assertFalse(b.deliver_progress(self.api, self.cfg, now=self.now + 100))
        self.assertFalse(b.deliver(self.api, self.cfg))
        self.api.call.assert_called_once()
        self.assertEqual(self.rows('outbox')[0]['state'], 'uncertain')
        self.assertEqual(len(self.rows('outbox')), 1)

    def test_edit_rate_limit_retries_the_same_message_after_retry_after(self):
        self.generic_card()
        sent = self.send_initial()
        self.emit(self.commentary('正在验证'))
        self.api.call.side_effect = [b.Rejected(429, 30), {'message_id': 701}]
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 11))
        self.assertFalse(b.deliver_progress(self.api, self.cfg, now=sent + 40))
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 41))
        edits = self.api.call.call_args_list[1:]
        self.assertEqual([call.args[0] for call in edits], ['editMessageText', 'editMessageText'])
        self.assertEqual([call.kwargs['message_id'] for call in edits], [701, 701])
        self.assertEqual(self.card()['disabled'], 0)
        self.assertEqual(len(self.rows('outbox')), 1)

    def test_not_modified_acknowledges_terminal_card_without_new_message(self):
        self.generic_card()
        sent = self.send_initial()
        self.emit(self.lifecycle(kind='task_complete', offset=2))
        self.api.call.side_effect = b.Rejected(400, not_modified=True)
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 11))
        card = self.card()
        self.assertEqual(card['last_version'], card['version'])
        self.assertEqual(card['disabled'], 0)
        self.assertFalse(b.deliver_progress(self.api, self.cfg, now=sent + 100))
        self.assertEqual(self.api.call.call_count, 2)
        self.assertEqual(len(self.rows('outbox')), 1)

    def test_deleted_progress_message_disables_edits_without_fallback_spam(self):
        self.generic_card()
        sent = self.send_initial()
        self.emit(self.commentary('正在执行'))
        self.api.call.side_effect = b.Rejected(400)
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 11))
        self.assertEqual(self.card()['disabled'], 1)
        self.emit(self.commentary('更多进度', offset=2))
        self.assertFalse(b.deliver_progress(self.api, self.cfg, now=sent + 100))
        self.assertEqual([call.args[0] for call in self.api.call.call_args_list],
                         ['sendMessage', 'editMessageText'])
        self.assertEqual(len(self.rows('outbox')), 1)

    def test_edit_timeout_retries_only_the_existing_receipt(self):
        self.generic_card()
        sent = self.send_initial()
        self.emit(self.commentary('正在检查结果'))
        self.api.call.side_effect = [TimeoutError('edit timeout'), {'message_id': 701}]
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 11))
        self.assertFalse(b.deliver_progress(self.api, self.cfg, now=sent + 20))
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 21))
        self.assertEqual([call.kwargs['message_id'] for call in self.api.call.call_args_list[1:]],
                         [701, 701])
        self.assertEqual(len(self.rows('outbox')), 1)

    def test_completion_during_inflight_edit_remains_unsent_until_next_edit(self):
        self.generic_card()
        sent = self.send_initial()
        self.emit(self.commentary('即将完成'))

        def complete_while_editing(method, **payload):
            self.assertEqual(method, 'editMessageText')
            with b.db() as c:
                b.progress_set(c, 'progress:original:current', state='ended', now=sent + 12)
            return {'message_id': payload['message_id']}

        self.api.call.side_effect = complete_while_editing
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 11))
        raced = self.card()
        self.assertEqual(raced['state'], 'ended')
        self.assertGreater(raced['version'], raced['last_version'])
        self.api.call.side_effect = None
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 21))
        self.assertIn('本轮已结束', self.api.call.call_args.kwargs['text'])
        finished = self.card()
        self.assertEqual(finished['version'], finished['last_version'])
        self.emit(self.commentary('LATE PROGRESS MUST NOT REOPEN', offset=200))
        self.assertEqual(self.card()['version'], finished['version'])
        self.assertFalse(b.deliver_progress(self.api, self.cfg, now=sent + 500))
        self.assertEqual(len(self.rows('outbox')), 1)

    def test_final_answer_images_original_bytes_and_receipt_routing_are_unchanged(self):
        self.cfg['image_delivery_enabled'] = True
        self.save_settings()
        self.generic_card()
        sent = self.send_initial()
        self.api.call.return_value = {'message_id': 702}
        self.api.upload.side_effect = [{'message_id': 703}, {'message_id': 704}]
        source = b.ROOT / 'final-screenshot.png'
        source.write_bytes(PNG)
        result = '完整最终答复\n![截图](<' + str(source) + '>)'
        payload = self.final_payload('original', 'current', result)
        b.notify(payload, trusted=True)
        b.notify(payload, trusted=True)
        self.assertEqual(self.card()['state'], 'ended')
        self.assertEqual(len([row for row in self.rows('outbox')
                              if row['id'] == 'notice:original:current']), 1)
        self.assertEqual(len(self.rows('outbox_media')), 1)
        self.assertTrue(b.deliver(self.api, self.cfg))
        self.assertTrue(b.deliver(self.api, self.cfg))
        self.assertIn('完整最终答复', self.api.call.call_args.kwargs['text'])
        self.assertEqual(self.api.upload.call_args.args[:3], ('sendPhoto', 'photo', PNG))
        image = self.rows('outbox_media')[0]
        token = self.rows('media_tokens')[0]['token']
        reply = self.update(200, '继续检查截图')['message']
        reply['reply_to_message'] = self.quoted_message(703)
        source.write_bytes(PNG + b'changed project original')
        callback = {'id': 'original-progress-test', 'from': {'id': 123, 'is_bot': False},
                    'data': 'original:' + token,
                    'message': {'message_id': 703, 'chat': {'id': 123, 'type': 'private'},
                                'from': {'id': 456, 'is_bot': True}}}
        with b.db() as c:
            self.assertEqual(b.resolve_reply(c, self.cfg, reply)['id'], 'original')
            self.assertIn('已入队', b.select_callback(c, self.cfg, callback))
        self.assertTrue(b.deliver(self.api, self.cfg))
        self.assertEqual(self.api.upload.call_args.args[:3], ('sendDocument', 'document', PNG))
        self.assertEqual(Path(image['path']).read_bytes(), PNG)
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 11))
        self.assertEqual(self.api.call.call_args.kwargs['message_id'], 701)
        self.assertEqual(len(self.rows('outbox')), 4)

    def test_feature_disabled_preserves_existing_text_ack_and_final_notice(self):
        self.cfg['progress_cards_enabled'] = False
        self.save_settings()
        b.accept(self.cfg, self.update(100, '检查页面'))
        self.emit(self.lifecycle())
        self.assertEqual(self.rows('progress_cards'), [])
        self.assertIn('已接收', self.rows('outbox')[0]['text'])
        self.assertTrue(b.deliver(self.api, self.cfg))
        self.assertFalse(b.deliver_progress(self.api, self.cfg, now=self.now + 100))
        b.notify(self.final_payload('original', 'current', '原有最终通知'), trusted=True)
        self.assertEqual(len(self.rows('outbox')), 2)
        self.assertIn('原有最终通知', self.rows('outbox')[-1]['text'])

    def test_renderer_failure_cannot_rollback_an_accepted_task(self):
        with patch.object(b.progress, 'render', side_effect=ValueError('renderer test failure')):
            b.accept(self.cfg, self.update(100, '执行原任务'))
        self.assertEqual(len(self.rows('jobs')), 1)
        self.assertEqual(self.rows('jobs')[0]['thread'], 'original')
        self.assertEqual(self.rows('progress_cards'), [])
        self.assertEqual(len(self.rows('outbox')), 1)
        self.assertIn('已接收', self.rows('outbox')[0]['text'])

    def test_renderer_failure_cannot_rollback_a_final_notice(self):
        self.generic_card()
        payload = self.final_payload('original', 'current', '完整最终结果仍须发送')
        with patch.object(b.progress, 'render', side_effect=ValueError('renderer test failure')):
            b.notify(payload, trusted=True)
        notices = [row for row in self.rows('outbox') if row['id'] == 'notice:original:current']
        self.assertEqual(len(notices), 1)
        self.assertIn('完整最终结果仍须发送', notices[0]['text'])

    def test_optional_worker_update_database_connection_failure_never_escapes(self):
        with patch.object(b, 'db', side_effect=sqlite3.OperationalError('test database unavailable')) as database:
            b.progress_worker_update(self.cfg, 100, state='running')
        database.assert_called_once()
        self.assertEqual(self.rows('jobs'), [])
        self.api.call.assert_not_called()

    def test_optional_worker_update_parse_failure_never_opens_database_or_escapes(self):
        with patch.object(b.progress, 'event_detail', side_effect=ValueError('test parse failure')) as parse, \
                patch.object(b, 'db', side_effect=AssertionError('Unexpected optional database access')) as database:
            b.progress_worker_update(self.cfg, 100, event=self.commentary())
        parse.assert_called_once()
        database.assert_not_called()
        self.api.call.assert_not_called()

    def test_disabled_optional_worker_update_never_parses_or_opens_database(self):
        self.cfg['progress_cards_enabled'] = False
        with patch.object(b.progress, 'event_detail', side_effect=AssertionError('Disabled parse')) as parse, \
                patch.object(b, 'db', side_effect=AssertionError('Disabled database')) as database:
            b.progress_worker_update(self.cfg, 100, event=self.commentary(), state='running')
        parse.assert_not_called()
        database.assert_not_called()
        self.api.call.assert_not_called()

    def test_generic_card_freezes_when_exact_turn_process_and_writer_have_exited(self):
        self.generic_card()
        with Path(self.meta['rollout_path']).open('a') as stream:
            stream.write(json.dumps(self.lifecycle()) + '\n')
        with patch.object(b.time, 'time', return_value=self.now + 31), \
                patch.object(b, 'open_rollout_paths', return_value=set()), \
                patch.object(b, 'thread_writer_busy', return_value=False):
            b.refresh_progress_cards(self.cfg)
        ended = self.card()
        self.assertEqual(ended['state'], 'interrupted')
        self.assertIsNotNone(ended['finished'])
        self.assertIn('不会自动重跑', ended['detail'])
        self.emit(self.commentary('LATE COMMENTARY', offset=100))
        self.assertEqual(self.card()['version'], ended['version'])
        self.assertEqual(self.rows('jobs'), [])
        self.api.call.assert_not_called()

    def test_generic_process_probe_failure_or_active_writer_does_not_claim_interruption(self):
        self.generic_card()
        with Path(self.meta['rollout_path']).open('a') as stream:
            stream.write(json.dumps(self.lifecycle()) + '\n')
        original = self.card()
        with patch.object(b.time, 'time', return_value=self.now + 31), \
                patch.object(b, 'open_rollout_paths', side_effect=OSError('test process probe unavailable')):
            b.refresh_progress_cards(self.cfg)
        self.assertEqual(self.card()['state'], 'running')
        self.assertIsNone(self.card()['finished'])
        for offset, writer, paths in ((62, None, set()), (93, True, set()),
                                      (124, False, {self.meta['rollout_path']})):
            with self.subTest(writer=writer, live_path=bool(paths)), \
                    patch.object(b.time, 'time', return_value=self.now + offset), \
                    patch.object(b, 'open_rollout_paths', return_value=paths), \
                    patch.object(b, 'thread_writer_busy', return_value=writer):
                b.refresh_progress_cards(self.cfg)
            self.assertEqual(self.card()['state'], 'running')
            self.assertIsNone(self.card()['finished'])
            self.assertEqual(self.card()['version'], original['version'])
        self.api.call.assert_not_called()

    def test_newer_turn_freezes_unfinished_generic_card_without_claiming_success(self):
        self.generic_card()
        self.emit(self.commentary('本回合最后的公开进展', offset=5))
        sent = self.send_initial()
        path = Path(self.meta['rollout_path'])
        for index, stamp in enumerate((None, 'invalid', self.stamp(5)), start=1):
            later = self.lifecycle(turn='later-turn', offset=6)
            later['timestamp'] = stamp
            with path.open('a') as stream:
                stream.write(json.dumps(later) + '\n')
            with self.subTest(stamp=stamp), \
                    patch.object(b.time, 'time', return_value=self.now + 31 * index), \
                    patch.object(b, 'open_rollout_paths', return_value={str(path)}), \
                    patch.object(b, 'thread_writer_busy', return_value=True):
                b.refresh_progress_cards(self.cfg)
            self.assertEqual(self.card()['state'], 'running')
            self.assertIsNone(self.card()['finished'])
        with path.open('a') as stream:
            stream.write(json.dumps(self.lifecycle(turn='later-turn', offset=6)) + '\n')
        with patch.object(b.time, 'time', return_value=self.now + 124), \
                patch.object(b, 'open_rollout_paths', return_value={str(path)}), \
                patch.object(b, 'thread_writer_busy', return_value=True):
            b.refresh_progress_cards(self.cfg)
        frozen = self.card()
        self.assertEqual(frozen['turn'], 'current')
        self.assertEqual(frozen['state'], 'interrupted')
        self.assertIsNotNone(frozen['finished'])
        self.assertIn('未确认本回合最终结果', frozen['detail'])
        self.assertIn('停止刷新且不自动重跑', frozen['detail'])
        self.assertTrue(b.deliver_progress(self.api, self.cfg, now=sent + 130))
        self.assertNotIn('本轮已结束', self.api.call.call_args.kwargs['text'])
        self.assertEqual(self.api.call.call_args.kwargs['message_id'], 701)
        self.emit(self.commentary('旧回合迟到的消息', offset=150))
        self.assertEqual(self.card()['version'], frozen['version'])
        self.assertFalse(b.deliver_progress(self.api, self.cfg, now=sent + 200))
        self.assertEqual([row['id'] for row in self.rows('outbox')], ['progress:original:current'])
        self.assertEqual(self.rows('jobs'), [])


if __name__ == '__main__':
    unittest.main()
