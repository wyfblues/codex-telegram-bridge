"""Bounded public plan/file summaries without exposing code or tool output."""
import copy
import json
import unittest

import progress_card as progress


class RichProgressTests(unittest.TestCase):
    def event(self, method, **params):
        return {'method': method, 'params': dict(threadId='thread', turnId='turn', **params)}

    def card(self, **changes):
        card = {'project': 'demo', 'title': '改善布局', 'thread': '12345678-abc',
                'started': 10, 'state': 'running', 'detail': '正在检查布局。'}
        card.update(changes)
        return card

    def test_official_plan_status_normalization_and_no_explanation_leak(self):
        info = progress.event_info(self.event('turn/plan/updated',
            explanation='private rationale', plan=[{'step': '检查页面', 'status': 'completed'},
            {'step': '修正间距', 'status': 'inProgress'}, {'step': '运行验证', 'status': 'pending'}]))
        self.assertEqual(info, {'plan': [{'step': '检查页面', 'status': 'completed'},
            {'step': '修正间距', 'status': 'in_progress'}, {'step': '运行验证', 'status': 'pending'}]})
        self.assertNotIn('private', str(info))

    def test_render_shows_four_plan_steps_and_three_file_paths(self):
        plan = [{'step': f'步骤 {i}', 'status': 'in_progress' if i == 1 else 'pending'} for i in range(6)]
        files = [{'path': f'src/file{i}.py', 'added': i, 'removed': 2} for i in range(5)]
        text = progress.render(self.card(plan=plan, files=files), now=20)
        for expected in ('→ 步骤 1', '○ 步骤 3', '另有 2 项',
                         'src/file2.py  +2 -2', '另有 2 个文件'):
            self.assertIn(expected, text)
        self.assertNotIn('步骤 4', text)
        self.assertNotIn('file3', text)

    def test_model_queue_and_context_are_distinct_public_fields(self):
        text = progress.render(self.card(model='gpt-example', effort='high', queue_count=2,
                               token_usage={'used': 15120, 'window': 200000}), now=20)
        self.assertIn('运行：gpt-example · high', text)
        self.assertIn('等待任务：2', text)
        self.assertIn('上下文：15,120 / 200,000 tokens', text)
        self.assertNotIn('额度', text)
        self.assertNotIn('等待任务', progress.render(self.card(queue_count=0), now=20))

    def test_omitted_context_window_does_not_invent_capacity(self):
        text = progress.render(self.card(token_usage={'used': 100}), now=20)
        self.assertIn('上下文：100 tokens', text)
        self.assertNotIn(' / ', text)

    def test_usage_protocol_and_rollout_use_last_not_cumulative_counts(self):
        official = self.event('thread/tokenUsage/updated', tokenUsage={
            'last': {'totalTokens': 250}, 'total': {'totalTokens': 9000}, 'modelContextWindow': 1000})
        rollout = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'last_token_usage': {'total_tokens': 250}, 'total_token_usage': {'total_tokens': 9000},
            'model_context_window': 1000}}}
        for event in (official, rollout):
            self.assertEqual(progress.event_info(event), {'token_usage': {'used': 250, 'window': 1000}})

    def test_rollout_context_accepts_only_model_and_effort(self):
        event = {'type': 'turn_context', 'payload': {'model': 'gpt-example', 'effort': 'high',
                 'instructions': 'private content', 'cwd': '/private/project', 'turn_id': 'exact'}}
        self.assertEqual(progress.event_info(event), {'model': 'gpt-example', 'effort': 'high'})

    def test_structured_plan_call_does_not_include_extra_tool_arguments(self):
        args = {'plan': [{'step': '检查布局', 'status': 'in_progress'}], 'explanation': 'private'}
        for name in ('update_plan', 'functions.update_plan'):
            event = {'type': 'response_item', 'payload': {'type': 'function_call', 'name': name,
                     'arguments': json.dumps(args)}}
            self.assertEqual(progress.event_info(event), {'plan': args['plan']})
        for name in ('exec_command', 'apply_patch', 'tools.update_plan'):
            event['payload']['name'] = name
            self.assertEqual(progress.event_info(event), {})

    def test_unified_diff_reports_only_paths_and_counts(self):
        diff = ('diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n'
                '@@ -1,2 +1,3 @@\n context\n-private_old()\n+secret_new()\n+more_code()\n'
                'diff --git a/deleted.py b/deleted.py\n--- a/deleted.py\n+++ /dev/null\n'
                '@@ -1 +0,0 @@\n-private_delete()\n')
        info = progress.event_info(self.event('turn/diff/updated', diff=diff))
        self.assertEqual(info, {'files': [{'path': 'src/a.py', 'added': 2, 'removed': 1},
                                         {'path': 'deleted.py', 'added': 0, 'removed': 1}]})
        for private in ('secret_new', 'private_old', 'more_code', 'private_delete'):
            self.assertNotIn(private, str(info))

    def test_new_files_quoted_paths_multiple_hunks_and_missing_newline(self):
        diff = ('--- /dev/null\n+++ "b/space file.py"\n@@ -0,0 +1,1 @@\n+line\n'
                '\\ No newline at end of file\n@@ -0,0 +2,1 @@\n+line2\n')
        self.assertEqual(progress.event_info(self.event('turn/diff/updated', diff=diff)),
                         {'files': [{'path': 'space file.py', 'added': 2, 'removed': 0}]})

    def test_code_that_looks_like_headers_never_becomes_a_path(self):
        diff = ('--- a/real.py\n+++ b/real.py\n@@ -1 +1 @@\n-private\n+++ fake_secret_path\n')
        info = progress.event_info(self.event('turn/diff/updated', diff=diff))
        self.assertEqual(info['files'], [{'path': 'real.py', 'added': 1, 'removed': 1}])
        self.assertNotIn('fake_secret_path', str(info))
        self.assertEqual(progress.event_info(self.event('turn/diff/updated', diff='+++ arbitrary')), {'files': []})

    def test_per_file_patch_notification_counts_without_output(self):
        info = progress.event_info(self.event('item/fileChange/patchUpdated', changes=[
            {'path': 'src/a.py', 'kind': {'type': 'update'},
             'diff': '@@ -1 +1,2 @@\n-oldcode\n+newcode\n+second\n'}]))
        self.assertEqual(info, {'files': [{'path': 'src/a.py', 'added': 2, 'removed': 1}]})
        self.assertNotIn('newcode', str(info))

    def test_confirmed_rollout_patch_and_cli_file_change_only(self):
        event = {'type': 'event_msg', 'payload': {'type': 'patch_apply_end', 'success': True,
                 'changes': {'src/a.py': {'unified_diff': 'private patch'}}, 'stdout': 'private stdout'}}
        self.assertEqual(progress.event_info(event), {'files': [{'path': 'src/a.py'}]})
        event['payload']['success'] = False
        self.assertEqual(progress.event_info(event), {})
        cli = {'type': 'item.completed', 'item': {'type': 'file_change',
                'changes': [{'path': 'src/a.py', 'kind': 'update'}], 'output': 'private stdout'}}
        self.assertEqual(progress.event_info(cli), {'files': [{'path': 'src/a.py'}]})
        cli['item']['status'] = 'failed'
        self.assertEqual(progress.event_info(cli), {})
        cli['type'] = 'item.started'
        self.assertEqual(progress.event_info(cli), {})

    def test_incomplete_or_failed_official_file_change_is_not_reported_as_changed(self):
        item = {'type': 'fileChange', 'status': 'inProgress', 'changes': [{'path': 'src/a.py'}]}
        self.assertEqual(progress.event_info(self.event('item/started', item=item)), {})
        item['status'] = 'failed'
        self.assertEqual(progress.event_info(self.event('item/completed', item=item)), {})
        item['status'] = 'completed'
        self.assertEqual(progress.event_info(self.event('item/completed', item=item)),
                         {'files': [{'path': 'src/a.py'}]})

    def test_cli_todo_list_has_no_unstructured_text_fallback(self):
        event = {'type': 'item.updated', 'item': {'type': 'todo_list', 'text': 'private',
                 'items': [{'text': '检查', 'completed': True}, {'text': '验证', 'completed': False}]}}
        self.assertEqual(progress.event_info(event), {'plan': [
            {'step': '检查', 'status': 'completed'}, {'step': '验证', 'status': 'pending'}]})

    def test_private_events_and_arbitrary_tool_outputs_are_ignored(self):
        events = [{'type': 'response_item', 'payload': {'type': 'reasoning', 'text': 'private'}},
                  {'method': 'item/commandExecution/outputDelta', 'params': {'delta': 'private'}},
                  {'method': 'item/reasoning/textDelta', 'params': {'delta': 'private'}},
                  {'type': 'item.completed', 'item': {'type': 'command_execution', 'output': 'private'}},
                  {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'apply_patch',
                    'input': '*** Add File: secret.py\n+private'}},
                  {'type': 'event_msg', 'payload': {'type': 'agent_reasoning', 'message': 'private'}}]
        for event in events:
            with self.subTest(event=event):
                self.assertEqual(progress.event_info(event), {})

    def test_bad_values_are_neutral_and_never_render_raw_objects(self):
        events = [None, [], {}, {'params': []}, {'type': []},
                  self.event('turn/plan/updated', plan=[None, {}, {'step': 'bad', 'status': []}]),
                  self.event('thread/tokenUsage/updated', tokenUsage={'last': {'totalTokens': True}}),
                  {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'update_plan',
                   'arguments': '{not json'}}, self.event('turn/diff/updated', diff=[]),
                  {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'update_plan',
                   'arguments': '[' * 2000 + ']' * 2000}}]
        for event in events:
            with self.subTest(event=event):
                self.assertIsInstance(progress.event_info(event), dict)
        text = progress.render(self.card(model=[], effort={}, queue_count=True,
            plan=[{'step': {'private': 'raw'}, 'status': 'pending'}],
            files=[{'path': {'private': 'raw'}, 'added': -1}], token_usage={'used': True}), now=20)
        self.assertNotIn('raw', text)
        self.assertNotIn('上下文', text)
        self.assertNotIn('运行：', text)

    def test_all_new_fields_redact_credentials_and_remain_utf16_bounded(self):
        secret = 'known-secret-token'
        info = progress.event_info(self.event('turn/plan/updated', plan=[
            {'step': secret + '\u202e\x00' + '😀' * 100, 'status': 'pending'}]), secrets=(secret,))
        card = self.card(model=secret, effort=secret, secrets=(secret,),
                         plan=info['plan'] * 10, files=[{'path': secret + '😀' * 100}] * 10)
        text = progress.render(card, now=20)
        self.assertNotIn(secret, text)
        self.assertNotIn('\u202e', text)
        self.assertNotIn('\x00', text)
        self.assertIn('[凭据已隐藏]', text)
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 1000)
        text.encode('utf-8')

    def test_terminal_extras_are_frozen_and_inputs_are_not_mutated(self):
        card = self.card(state='ended', finished=25,
                         plan=[{'step': '验证', 'status': 'completed'}],
                         files=[{'path': 'src/a.py', 'added': 1}],
                         token_usage={'used': 100, 'window': 1000})
        original = copy.deepcopy(card)
        self.assertEqual(progress.render(card, now=30), progress.render(card, now=9999))
        self.assertEqual(card, original)
        self.assertIn('✓ 验证', progress.render(card, now=30))
        self.assertEqual(progress.CARD_INTERVAL, 10)


if __name__ == '__main__':
    unittest.main()
