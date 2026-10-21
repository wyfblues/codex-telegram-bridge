"""Offline structured-progress, formatted-answer, and exact-reply integration."""
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import test_progress_integration as fixture
from test_telegram_render import StrictHTML

b = fixture.b


class RichDeliveryTests(unittest.TestCase):
    setUp = fixture.ProgressIntegrationTests.setUp
    tearDown = fixture.ProgressIntegrationTests.tearDown
    rows = fixture.ProgressIntegrationTests.rows
    update = fixture.ProgressIntegrationTests.update
    make_thread = fixture.ProgressIntegrationTests.make_thread
    append_event = fixture.ProgressIntegrationTests.append_event
    select_default = fixture.ProgressIntegrationTests.select_default
    final_payload = fixture.ProgressIntegrationTests.final_payload
    save_settings = fixture.ProgressIntegrationTests.save_settings
    stamp = fixture.ProgressIntegrationTests.stamp
    lifecycle = fixture.ProgressIntegrationTests.lifecycle
    commentary = fixture.ProgressIntegrationTests.commentary
    emit = fixture.ProgressIntegrationTests.emit
    generic_card = fixture.ProgressIntegrationTests.generic_card
    card = fixture.ProgressIntegrationTests.card
    send_initial = fixture.ProgressIntegrationTests.send_initial
    quoted_message = staticmethod(fixture.ProgressIntegrationTests.quoted_message)

    def structured(self, kind, payload, offset=1, turn='current'):
        payload = dict(payload)
        if turn is not None:
            payload['turn_id'] = turn
        return {'type': kind, 'timestamp': self.stamp(offset), 'payload': payload}

    def plan(self, offset=1, turn='current'):
        return self.structured('response_item', {'type': 'function_call',
            'name': 'update_plan', 'arguments': json.dumps({'explanation': 'PRIVATE EXPLANATION',
            'plan': [{'step': '检查布局', 'status': 'completed'},
                     {'step': '运行验证', 'status': 'in_progress'}]})}, offset, turn)

    def details(self, key='progress:original:current'):
        with b.db() as c:
            row = c.execute('SELECT data FROM progress_details WHERE id=?', (key,)).fetchone()
            return json.loads(row['data']) if row else {}

    @staticmethod
    def visible(call):
        if call.kwargs.get('parse_mode') != 'HTML':
            return call.kwargs['text']
        parser = StrictHTML()
        parser.feed(call.kwargs['text'])
        return parser.finish()

    def drain(self):
        for _ in range(100):
            if not b.deliver(self.api, self.cfg):
                return
        self.fail('Outbox did not drain within 100 messages')

    def bind_incoming_job(self, job=100, receipt=801, turn='current'):
        update = self.update(job, '检查当前布局')
        update['message']['message_id'] = receipt
        b.accept(self.cfg, update)
        self.send_initial('reply:' + str(job))
        with b.db() as c:
            c.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
            b.progress_update_job(c, job, turn=turn, state='running')
        return 'reply:' + str(job)

    def test_exact_rollout_turn_updates_structured_fields_on_one_existing_card(self):
        b.baseline_completions()
        events = [self.lifecycle(offset=1), self.plan(offset=2),
                  self.structured('event_msg', {'type': 'patch_apply_end', 'success': True,
                    'changes': {'src/layout.py': {'unified_diff': 'PRIVATE FULL CODE DIFF'}},
                    'stdout': 'PRIVATE TOOL OUTPUT'}, offset=3),
                  self.structured('turn_context', {'model': 'gpt-example', 'effort': 'high',
                    'instructions': 'PRIVATE INSTRUCTIONS'}, offset=4),
                  self.structured('event_msg', {'type': 'token_count', 'info': {
                    'last_token_usage': {'total_tokens': 1234}, 'model_context_window': 200000}},
                    offset=5, turn=None), self.commentary('正在核对验证结果', offset=6)]
        path = Path(self.meta['rollout_path'])
        with path.open('a') as stream:
            stream.writelines(json.dumps(event) + '\n' for event in events)
        b.scan_completions()
        info = self.details()
        self.assertEqual(info['plan'][1], {'step': '运行验证', 'status': 'in_progress'})
        self.assertEqual(info['files'], [{'path': 'src/layout.py'}])
        self.assertEqual(info['model'], 'gpt-example')
        self.assertEqual(info['effort'], 'high')
        self.assertEqual(info['token_usage'], {'used': 1234, 'window': 200000})
        self.assertEqual(len(self.rows('progress_cards')), 1)
        self.assertEqual([row['id'] for row in self.rows('outbox')], ['progress:original:current'])
        self.send_initial()
        text = self.api.call.call_args.kwargs['text']
        for public in ('✓ 检查布局', '→ 运行验证', 'src/layout.py', 'gpt-example', '1,234 / 200,000',
                       '正在核对验证结果'):
            self.assertIn(public, text)
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('PRIVATE', json.dumps(self.rows('progress_details')))

    def test_wrong_turn_historical_and_late_terminal_events_do_not_add_info(self):
        self.generic_card()
        self.emit(self.plan(offset=1))
        expected = self.details()
        version = self.card()['version']
        self.emit(self.structured('turn_context', {'model': 'WRONG MODEL'}, offset=2, turn='wrong'))
        self.emit(self.structured('turn_context', {'model': 'HISTORICAL MODEL'}, offset=-60))
        self.emit(self.structured('event_msg', {'type': 'token_count', 'info': {
            'last_token_usage': {'total_tokens': 99999}}}, offset=-60, turn=None))
        self.assertEqual(self.details(), expected)
        self.assertEqual(self.card()['version'], version)
        self.emit(self.lifecycle(kind='task_complete', offset=3))
        terminal = self.card()
        self.emit(self.structured('turn_context', {'model': 'LATE MODEL'}, offset=4))
        self.emit(self.plan(offset=5))
        self.assertEqual(self.card()['version'], terminal['version'])
        self.assertEqual(self.details(), expected)
        self.assertEqual(len(self.rows('progress_cards')), 1)

    def test_structured_events_without_new_turn_do_not_create_historical_cards(self):
        for event in (self.plan(offset=-60),
                      self.structured('turn_context', {'model': 'old-model'}, offset=-59),
                      self.structured('event_msg', {'type': 'token_count', 'info': {
                        'last_token_usage': {'total_tokens': 123}}}, offset=-58, turn=None)):
            self.emit(event)
        self.assertEqual(self.rows('progress_cards'), [])
        self.assertEqual(self.rows('progress_details'), [])
        self.assertEqual(self.rows('outbox'), [])

    def test_private_reasoning_command_arguments_and_failed_patch_do_not_change_card(self):
        self.generic_card()
        before = self.card()
        for event in (
            self.structured('response_item', {'type': 'reasoning', 'text': 'PRIVATE REASONING'}),
            self.structured('response_item', {'type': 'function_call', 'name': 'exec_command',
                                            'arguments': '{"cmd":"PRIVATE COMMAND"}'}),
            self.structured('event_msg', {'type': 'patch_apply_end', 'success': False,
                                          'changes': {'PRIVATE-FILE': {}}, 'stdout': 'PRIVATE LOG'})):
            self.emit(event)
        self.assertEqual(self.card()['version'], before['version'])
        self.assertEqual(self.details(), {})
        self.send_initial()
        self.assertNotIn('PRIVATE', self.api.call.call_args.kwargs['text'])

    def test_incremental_successful_patches_retain_all_changed_paths_on_the_same_card(self):
        self.generic_card()
        for index, path in enumerate(('src/a.py', 'src/b.py', 'src/a.py'), start=1):
            self.emit(self.structured('event_msg', {'type': 'patch_apply_end', 'success': True,
                'changes': {path: {'unified_diff': 'PRIVATE CODE'}}}, offset=index))
        self.assertEqual(self.details()['files'], [{'path': 'src/a.py'}, {'path': 'src/b.py'}])
        self.assertEqual(len(self.rows('progress_cards')), 1)
        self.assertNotIn('PRIVATE', self.rows('outbox')[0]['text'])

    def test_rich_final_sends_balanced_html_with_original_visible_answer(self):
        self.cfg['rich_messages_enabled'] = True
        self.save_settings()
        b.notify(self.final_payload('original', 'formatted', '**重点** 与 `<unsafe>&`。'), trusted=True)
        self.assertTrue(b.deliver(self.api, self.cfg))
        call = self.api.call.call_args
        self.assertEqual(call.kwargs.get('parse_mode'), 'HTML')
        self.assertIn('<b>重点</b>', call.kwargs['text'])
        self.assertIn('<code>&lt;unsafe&gt;&amp;</code>', call.kwargs['text'])
        self.assertIn('重点 与 <unsafe>&。', self.visible(call))
        self.assertNotIn('PRIVATE INPUT', self.visible(call))

    def test_explicit_html_rejection_falls_back_once_to_the_matching_visible_plain_text(self):
        self.cfg['rich_messages_enabled'] = True
        self.save_settings()
        b.notify(self.final_payload('original', 'fallback', '**重点** 和 `a < b`'), trusted=True)
        self.api.call.side_effect = [b.Rejected(400), {'message_id': 702}]
        self.assertTrue(b.deliver(self.api, self.cfg))
        self.assertEqual(self.api.call.call_count, 2)
        first, fallback = self.api.call.call_args_list
        self.assertEqual(first.kwargs.get('parse_mode'), 'HTML')
        self.assertNotIn('parse_mode', fallback.kwargs)
        self.assertEqual(fallback.kwargs['text'], self.visible(first))
        self.assertEqual(self.rows('outbox')[0]['state'], 'sent')
        self.assertFalse(b.deliver(self.api, self.cfg))

    def test_unknown_html_send_timeout_never_retries_plain_or_repeats_delivery(self):
        self.cfg['rich_messages_enabled'] = True
        self.save_settings()
        b.notify(self.final_payload('original', 'timeout', '**重点**'), trusted=True)
        self.api.call.side_effect = TimeoutError('uncertain outcome')
        self.assertTrue(b.deliver(self.api, self.cfg))
        self.api.call.assert_called_once()
        self.assertEqual(self.rows('outbox')[0]['state'], 'uncertain')
        self.assertFalse(b.deliver(self.api, self.cfg))
        self.api.call.assert_called_once()

    def test_non_parse_rejection_does_not_trigger_plain_fallback(self):
        self.cfg['rich_messages_enabled'] = True
        self.save_settings()
        b.notify(self.final_payload('original', 'rejected', '**重点**'), trusted=True)
        self.api.call.side_effect = b.Rejected(403)
        self.assertTrue(b.deliver(self.api, self.cfg))
        self.api.call.assert_called_once()
        self.assertEqual(self.rows('outbox')[0]['state'], 'failed')

    def test_optional_rich_renderer_failure_falls_back_plain_without_losing_final_notice(self):
        self.cfg['rich_messages_enabled'] = True
        self.save_settings()
        payload = self.final_payload('original', 'renderer-error', '**原有完整答复**\n第二行')
        with patch.object(b.telegram_render, 'render_parts', side_effect=ValueError('test formatter failure')):
            b.notify(payload, trusted=True)
            b.notify(payload, trusted=True)
            self.assertEqual(len(self.rows('outbox')), 1)
            self.assertTrue(b.deliver(self.api, self.cfg))
        self.api.call.assert_called_once()
        self.assertNotIn('parse_mode', self.api.call.call_args.kwargs)
        self.assertIn('原有完整答复', self.api.call.call_args.kwargs['text'])
        self.assertIn('第二行', self.api.call.call_args.kwargs['text'])
        self.assertEqual(self.rows('outbox')[0]['state'], 'sent')

    def test_long_code_preserves_every_line_and_html_formatting_across_notice_parts(self):
        self.cfg['rich_messages_enabled'] = True
        self.save_settings()
        code = 'print("safe😀")\n' * 900
        b.notify(self.final_payload('original', 'longcode', '```python\n' + code + '```'), trusted=True)
        self.drain()
        calls = self.api.call.call_args_list
        self.assertGreater(len(calls), 2)
        self.assertTrue(all(call.kwargs.get('parse_mode') == 'HTML' for call in calls))
        visible = ''.join(self.visible(call) for call in calls)
        visible = re.sub(r'【\d+/\d+】\n', '', visible)
        self.assertNotIn('```', visible)
        self.assertEqual(visible.count('print("safe😀")'), 900)
        self.assertEqual('print' + visible.split('print', 1)[1], code)
        self.assertGreater(sum('<pre>' in call.kwargs['text'] for call in calls), 1)

    def test_each_multipart_notice_quotes_the_exact_incoming_job_receipt(self):
        self.cfg['rich_messages_enabled'] = True
        self.save_settings()
        self.bind_incoming_job(receipt=801)
        self.make_thread('other', source='exec')
        self.select_default('other')
        b.notify(self.final_payload('original', 'current', '**完成检查**\n' + '结果😀\n' * 1400), trusted=True)
        notice_rows = [row for row in self.rows('outbox') if row['id'].startswith('notice:original:current')]
        self.assertGreater(len(notice_rows), 1)
        mappings = {row['id']: row['receipt'] for row in self.rows('outbox_replies')}
        self.assertEqual({row['id']: mappings.get(row['id']) for row in notice_rows},
                         {row['id']: 801 for row in notice_rows})
        self.api.call.reset_mock()
        self.drain()
        for call in self.api.call.call_args_list:
            self.assertEqual(call.kwargs['reply_parameters'],
                             {'message_id': 801, 'allow_sending_without_reply': True})
            self.visible(call)
        b.notify(self.final_payload('original', 'unrelated', '另一回合答复'), trusted=True)
        self.assertNotIn('notice:original:unrelated',
                         {row['id'] for row in self.rows('outbox_replies')})

    def test_image_bytes_and_visible_final_answer_survive_rich_delivery_with_exact_reply(self):
        self.cfg.update(rich_messages_enabled=True, image_delivery_enabled=True)
        self.save_settings()
        self.bind_incoming_job(receipt=802)
        source = b.ROOT / 'safe-preview.png'
        source.write_bytes(fixture.PNG)
        result = '**完整最终答复**\n![截图](<' + str(source) + '>)'
        b.notify(self.final_payload('original', 'current', result), trusted=True)
        media = self.rows('outbox_media')[0]
        self.api.call.reset_mock()
        self.api.upload.return_value = {'message_id': 703}
        self.drain()
        self.assertIn('完整最终答复', ''.join(self.visible(call) for call in self.api.call.call_args_list))
        self.assertEqual(self.api.upload.call_args.args[:3], ('sendPhoto', 'photo', fixture.PNG))
        self.assertEqual(Path(media['path']).read_bytes(), fixture.PNG)
        self.assertEqual(self.api.upload.call_args.kwargs['reply_parameters'],
                         {'message_id': 802, 'allow_sending_without_reply': True})
        with b.db() as c:
            self.assertEqual(b.resolve_reply(c, self.cfg,
                {'reply_to_message': self.quoted_message(703)})['id'], 'original')

    def test_rejected_photo_fallback_document_keeps_exact_reply_without_photo_button(self):
        self.cfg.update(rich_messages_enabled=True, image_delivery_enabled=True)
        self.save_settings()
        self.bind_incoming_job(receipt=803)
        source = b.ROOT / 'photo-fallback.png'
        source.write_bytes(fixture.PNG)
        b.notify(self.final_payload('original', 'current', '![截图](<' + str(source) + '>)'), trusted=True)
        self.api.upload.side_effect = [b.Rejected(400), {'message_id': 704}]
        self.drain()
        photo, document = self.api.upload.call_args_list
        self.assertEqual(photo.args[:3], ('sendPhoto', 'photo', fixture.PNG))
        self.assertEqual(document.args[:3], ('sendDocument', 'document', fixture.PNG))
        expected = {'message_id': 803, 'allow_sending_without_reply': True}
        self.assertEqual(photo.kwargs['reply_parameters'], expected)
        self.assertEqual(document.kwargs['reply_parameters'], expected)
        self.assertIn('reply_markup', photo.kwargs)
        self.assertNotIn('reply_markup', document.kwargs)
        self.assertIn('预览不可用，已发送原图', document.kwargs['caption'])
        self.assertTrue(all(row['state'] == 'sent' for row in self.rows('outbox')))


if __name__ == '__main__':
    unittest.main()
