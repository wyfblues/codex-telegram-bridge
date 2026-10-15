"""Pure progress-card filtering, credential redaction, and elapsed regressions."""
import copy
import unittest

import progress_card as progress


def units(text):
    return len(text.encode('utf-16-le')) // 2


class ProgressCardTests(unittest.TestCase):
    def response(self, **changes):
        payload = {'type': 'message', 'role': 'assistant', 'phase': 'commentary',
                   'content': [{'type': 'output_text', 'text': '正在检查页面布局。'}]}
        payload.update(changes)
        return {'type': 'response_item', 'payload': payload}

    def card(self, **changes):
        card = {'project': 'demo', 'title': '检查页面布局', 'thread': '12345678-abcdef',
                'state': 'running', 'started': 100.0, 'finished': None,
                'detail': '正在核对截图。'}
        card.update(changes)
        return card

    def test_interval_and_terminal_states(self):
        self.assertEqual(progress.CARD_INTERVAL, 10)
        self.assertEqual(progress.TERMINAL_STATES,
                         {'ended', 'failed', 'cancelled', 'interrupted'})

    def test_only_explicit_public_response_commentary_is_extracted(self):
        self.assertEqual(progress.event_detail(self.response()), '正在检查页面布局。')
        content = [{'type': 'reasoning_text', 'text': 'private reasoning'},
                   {'type': 'output_text', 'text': '检查完布局，'},
                   {'type': 'tool_result', 'text': 'raw log'},
                   {'type': 'output_text', 'text': '正在核对按钮。'}]
        self.assertEqual(progress.event_detail(self.response(content=content)),
                         '检查完布局， 正在核对按钮。')
        for changes in ({'phase': 'final_answer'}, {'phase': None}, {'phase': 'analysis'},
                        {'role': 'user'}, {'role': 'tool'}, {'type': 'reasoning'},
                        {'type': 'function_call'}, {'content': 'raw log'}):
            with self.subTest(changes=changes):
                self.assertIsNone(progress.event_detail(self.response(**changes)))

    def test_event_message_commentary_requires_agent_message_and_phase(self):
        event = {'type': 'event_msg', 'payload': {'type': 'agent_message',
                 'phase': 'commentary', 'message': '正在读取当前页面。'}}
        self.assertEqual(progress.event_detail(event), '正在读取当前页面。')
        for changes in ({'phase': 'final_answer'}, {'phase': None}, {'type': 'task_complete'},
                        {'type': 'agent_reasoning'}, {'type': 'user_message'}, {'role': 'user'},
                        {'message': {'text': 'malformed'}}):
            with self.subTest(changes=changes):
                self.assertIsNone(progress.event_detail(
                    dict(event, payload=dict(event['payload'], **changes))))

    def test_cli_commentary_accepts_started_updated_and_completed_only(self):
        item = {'type': 'agent_message', 'phase': 'commentary', 'text': '正在运行验证。'}
        for kind in ('item.started', 'item.updated', 'item.completed'):
            with self.subTest(kind=kind):
                self.assertEqual(progress.event_detail({'type': kind, 'item': item}), '正在运行验证。')
                for changes in ({'phase': 'final_answer'}, {'phase': None}, {'role': 'user'},
                                {'type': 'command_execution'}, {'type': 'reasoning'}, {'text': None}):
                    self.assertIsNone(progress.event_detail(
                        {'type': kind, 'item': dict(item, **changes)}))
        self.assertIsNone(progress.event_detail({'type': 'thread.started', 'item': item}))

    def test_malformed_and_private_events_never_become_detail(self):
        events = [None, [], 'raw command log', {}, {'type': []},
                  {'type': 'response_item', 'payload': None},
                  {'type': 'response_item', 'payload': []},
                  {'type': 'event_msg', 'payload': 'raw log'},
                  {'type': 'item.completed', 'item': []},
                  {'type': 'exec_command', 'output': 'raw log'},
                  {'type': 'event_msg', 'payload': {'type': 'task_complete',
                   'last_agent_message': 'Final business answer'}},
                  self.response(content=[None, {}, {'type': 'output_text', 'text': []}])]
        for event in events:
            with self.subTest(event=event):
                self.assertIsNone(progress.event_detail(event))

    def test_clean_removes_controls_direction_overrides_and_ansi(self):
        self.assertEqual(progress.clean(' \x1b[31m检查\x1b[0m\n\t页面\x00\u202e\ud800 '), '检查 页面')
        self.assertEqual(progress.clean('\x1b]0;hidden window title\x07当前进展'), '当前进展')
        self.assertEqual(progress.clean(None), '')
        self.assertEqual(progress.clean({'raw': 'log'}), '')

    def test_secrets_are_redacted_before_truncation_and_longest_first(self):
        secret = 'known-credential-example'
        cleaned = progress.clean('开始 ' + secret + ' 完成', (secret, 'known'), limit=15)
        self.assertNotIn('known', cleaned)
        self.assertNotIn('credential', cleaned)
        self.assertIn('[凭据已隐藏]', cleaned)
        self.assertLessEqual(units(cleaned), 15)
        self.assertEqual(progress.clean(secret, secrets=secret), '[凭据已隐藏]')
        self.assertEqual(progress.clean('无敏感信息', secrets=None), '无敏感信息')

    def test_event_detail_hides_known_credentials_and_remains_bounded(self):
        text = '检查 known-credential-example\n' + '😀' * 500
        detail = progress.event_detail(self.response(
            content=[{'type': 'output_text', 'text': text}]), secrets=('known-credential-example',))
        self.assertIn('[凭据已隐藏]', detail)
        self.assertNotIn('known-credential-example', detail)
        self.assertLessEqual(units(detail), 320)
        self.assertNotIn('\n', detail)

    def test_clean_utf16_bound_never_splits_emoji(self):
        for limit in (0, 1, 2, 3, 9, 320):
            with self.subTest(limit=limit):
                value = progress.clean('😀中' * 500, limit=limit)
                self.assertLessEqual(units(value), limit)
                value.encode('utf-8')
        self.assertEqual(progress.clean('abc', limit=3), 'abc')
        self.assertEqual(progress.clean('abcd', limit=3), 'ab…')

    def test_render_has_plain_identity_state_elapsed_and_latest_detail(self):
        card = self.card()
        original = copy.deepcopy(card)
        text = progress.render(card, now=190)
        self.assertEqual(text, '【Codex 执行进度】\n项目：demo\n任务：检查页面布局\n'
                         '会话：12345678\n状态：正在执行\n耗时：1 分 30 秒\n进展：正在核对截图。')
        self.assertEqual(card, original)
        self.assertNotIn('%', text)

    def test_all_states_are_explicit_without_claiming_business_success(self):
        states = ('queued', 'submitting', 'accepted', 'running', 'waiting', 'uncertain',
                  'ended', 'failed', 'cancelled', 'interrupted')
        for state in states:
            with self.subTest(state=state):
                text = progress.render(self.card(state=state, finished=150), now=180)
                self.assertNotIn('\n状态：状态待确认\n', text)
                self.assertNotIn('成功', text)
                self.assertNotIn('100%', text)
        self.assertIn('提交状态待确认', progress.render(self.card(state='uncertain'), now=180))

    def test_terminal_elapsed_freezes_at_finished_and_missing_finish_does_not_tick(self):
        for state in progress.TERMINAL_STATES:
            with self.subTest(state=state):
                card = self.card(state=state, finished=185)
                self.assertEqual(progress.render(card, now=200), progress.render(card, now=5000))
                self.assertIn('耗时：1 分 25 秒', progress.render(card, now=5000))
                card['finished'] = None
                self.assertIn('耗时：待确认', progress.render(card, now=5000))
                self.assertEqual(progress.render(card, now=200), progress.render(card, now=5000))

    def test_elapsed_handles_days_future_clock_and_bad_timestamps(self):
        self.assertIn('耗时：1 天 2 小时 3 分 4 秒',
                      progress.render(self.card(started=0), now=86400 + 7200 + 184))
        self.assertIn('耗时：0 秒', progress.render(self.card(started=200), now=100))
        for stamp in (None, '', True, -1, float('nan'), float('inf'), [], {}):
            with self.subTest(stamp=stamp):
                self.assertIn('耗时：待确认', progress.render(self.card(started=stamp), now=180))
                self.assertIn('耗时：待确认', progress.render(self.card(), now=stamp)
                              if stamp is not None else progress.render(self.card(started=None), now=180))

    def test_render_sanitizes_every_field_and_keeps_total_bound(self):
        secret = 'known-credential-example'
        card = self.card(project=secret + '😀' * 500, title=secret + '\n' + '😀' * 500,
                         thread=secret, detail=secret + '\x00' + '😀' * 500, secrets=(secret,))
        text = progress.render(card, now=180)
        self.assertNotIn(secret, text)
        self.assertIn('会话：[凭据已隐藏]', text)
        self.assertLessEqual(units(text), 1000)
        self.assertEqual(len(text.splitlines()), 7)

    def test_render_malformed_card_fails_to_neutral_display(self):
        for card in (None, [], 'raw log', {'state': []},
                     self.card(project={}, title=[], thread=None, detail={'text': 'raw'})):
            with self.subTest(card=card):
                text = progress.render(card, now=180)
                self.assertTrue(text.startswith('【Codex 执行进度】'))
                self.assertLessEqual(units(text), 1000)
                self.assertNotIn('raw', text)


if __name__ == '__main__':
    unittest.main()
