"""Offline Telegram panel regressions; no network, subprocess, or live workers."""
import json
import unittest
from unittest.mock import Mock, patch

import test_bridge as support
import test_reliability as reliability

b = support.b


class PanelTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting and rerunning either complete test suite.
    setUp = reliability.ReliabilityTests.setUp
    tearDown = reliability.ReliabilityTests.tearDown
    update = support.BridgeTests.update
    rows = support.BridgeTests.rows
    menu_callback = support.BridgeTests.menu_callback
    action_callback = support.BridgeTests.action_callback
    make_thread = reliability.ReliabilityTests.make_thread
    append_event = reliability.ReliabilityTests.append_event
    select_default = reliability.ReliabilityTests.select_default

    def fake_api(self):
        api = Mock()
        next_receipt = [800]

        def call(method, **payload):
            if method == 'sendMessage':
                receipt = next_receipt[0]
                next_receipt[0] += 1
                return {'message_id': receipt}
            if method == 'editMessageText':
                return {'message_id': payload['message_id']}
            self.fail('Unexpected API method: ' + method)

        api.call.side_effect = call
        return api

    def prepared_page_edit(self):
        self.cfg['projects'] = {f'project-{i}': str(b.ROOT / str(i)) for i in range(10)}
        b.accept(self.cfg, self.update(1, '/projects'))
        api = self.fake_api()
        self.assertTrue(b.deliver(api, self.cfg))
        next_page = next(r for r in self.rows('actions')
                         if r['menu_id'] == 'reply:1' and r['kind'] == 'projects')
        response = b.accept(self.cfg, self.action_callback(next_page['token'], 100, 800))
        self.assertIn('更新', response)
        return api, next_page

    def test_page_edit_reuses_receipt_and_rejects_old_tokens_before_and_after_send(self):
        api, old_page = self.prepared_page_edit()
        with b.db() as c:
            self.assertFalse(b.valid_menu(c, 'reply:1', 800))
        self.assertIn('更新', b.accept(self.cfg,
                                      self.action_callback(old_page['token'], 101, 800)))
        self.assertTrue(b.deliver(api, self.cfg))
        self.assertEqual([call.args[0] for call in api.call.call_args_list],
                         ['sendMessage', 'editMessageText'])
        edit_call = api.call.call_args_list[-1]
        self.assertEqual(edit_call.kwargs['message_id'], 800)
        messages = {r['id']: r for r in self.rows('outbox')}
        self.assertEqual(messages['reply:1']['receipt'], 800)
        self.assertEqual(messages['navigation:cb100']['receipt'], 800)
        with b.db() as c:
            self.assertTrue(b.valid_menu(c, 'navigation:cb100', 800))
            self.assertFalse(b.valid_menu(c, 'reply:1', 800))
        self.assertIn('更新', b.accept(self.cfg,
                                      self.action_callback(old_page['token'], 102, 800)))
        self.assertEqual(len(self.rows('outbox')), 2)
        self.assertEqual(self.rows('jobs'), [])

    def test_edit_rate_limit_keeps_pending_without_sending_a_duplicate_panel(self):
        api, _ = self.prepared_page_edit()
        api.call.side_effect = b.Rejected(429, retry=60)
        self.assertTrue(b.deliver(api, self.cfg))
        edit = next(r for r in self.rows('outbox') if r['id'] == 'navigation:cb100')
        self.assertEqual(edit['state'], 'pending')
        self.assertIsNone(edit['receipt'])
        self.assertFalse(b.deliver(api, self.cfg))
        self.assertEqual([call.args[0] for call in api.call.call_args_list],
                         ['sendMessage', 'editMessageText'])
        self.assertEqual(len(self.rows('outbox')), 2)

    def test_edit_not_modified_is_success_with_the_existing_receipt(self):
        api, _ = self.prepared_page_edit()
        api.call.side_effect = b.Rejected(400, not_modified=True)
        self.assertTrue(b.deliver(api, self.cfg))
        edit = next(r for r in self.rows('outbox') if r['id'] == 'navigation:cb100')
        self.assertEqual(edit['state'], 'sent')
        self.assertEqual(edit['receipt'], 800)
        with b.db() as c:
            self.assertTrue(b.valid_menu(c, 'navigation:cb100', 800))
        self.assertFalse(b.deliver(api, self.cfg))
        self.assertEqual(len(self.rows('outbox')), 2)

    def test_deleted_panel_reports_failure_once_without_resending_the_panel(self):
        api, _ = self.prepared_page_edit()
        api.call.side_effect = b.Rejected(400)
        self.assertTrue(b.deliver(api, self.cfg))
        messages = {r['id']: r for r in self.rows('outbox')}
        self.assertEqual(messages['navigation:cb100']['state'], 'failed')
        failure = messages['menu-error:navigation:cb100']
        self.assertEqual(failure['state'], 'pending')
        self.assertIn('菜单更新失败', failure['text'])
        self.assertIn('/projects', failure['text'])
        api.call.side_effect = None
        api.call.return_value = {'message_id': 801}
        self.assertTrue(b.deliver(api, self.cfg))
        self.assertFalse(b.deliver(api, self.cfg))
        self.assertEqual([call.args[0] for call in api.call.call_args_list],
                         ['sendMessage', 'editMessageText', 'sendMessage'])
        self.assertEqual(api.call.call_args_list[-1].kwargs['text'], failure['text'])

    def test_edit_timeout_remains_uncertain_and_is_not_retried(self):
        api, _ = self.prepared_page_edit()
        api.call.side_effect = TimeoutError('The edit may have reached Telegram')
        self.assertTrue(b.deliver(api, self.cfg))
        edit = next(r for r in self.rows('outbox') if r['id'] == 'navigation:cb100')
        self.assertEqual(edit['state'], 'uncertain')
        self.assertFalse(b.deliver(api, self.cfg))
        self.assertEqual(api.call.call_count, 2)
        self.assertEqual(len(self.rows('outbox')), 2)

    def test_detail_is_read_only_until_select_thread_is_clicked(self):
        self.make_thread('original')
        target = self.make_thread('target')
        self.select_default('original')
        api = self.fake_api()
        with patch.object(b, 'running_threads', return_value=([target], 0)), \
                patch.object(b, 'interrupted_threads', return_value=[]):
            with b.db() as c:
                b.status_menu(c, self.cfg, 'status:test')
        b.deliver(api, self.cfg)
        detail = next(r for r in self.rows('actions')
                      if r['menu_id'] == 'status:test' and r['kind'] == 'detail')
        b.accept(self.cfg, self.action_callback(detail['token'], 200, 800))
        with b.db() as c:
            self.assertEqual(b.get(c, 'project'), 'demo')
            self.assertEqual(b.get(c, 'selected:demo'), '')
            self.assertEqual(b.get(c, 'thread:demo'), 'original')
        b.deliver(api, self.cfg)
        choose = next(r for r in self.rows('actions')
                      if r['menu_id'] == 'navigation:cb200' and r['kind'] == 'select-thread')
        b.accept(self.cfg, self.action_callback(choose['token'], 201, 800))
        with b.db() as c:
            self.assertEqual(b.get(c, 'selected:demo'), 'target')
            self.assertEqual(b.get(c, 'thread:demo'), 'original')
            self.assertEqual(b.get(c, 'await-session:demo'), '0')
        self.assertEqual(self.rows('jobs'), [])

    def test_old_cancel_button_cannot_cancel_a_new_running_job(self):
        meta = self.make_thread()
        with b.db() as c:
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                      (300, 'demo', 'OLD PRIVATE INPUT', 'running', 'original'))
            b.thread_panel(c, self.cfg, meta, 'panel:old-job')
        api = self.fake_api()
        b.deliver(api, self.cfg)
        cancel = next(r for r in self.rows('actions') if r['kind'] == 'cancel-job')
        self.assertEqual(json.loads(cancel['data'])['job'], 300)
        with b.db() as c:
            c.execute("UPDATE jobs SET state='turn-ended',prompt='' WHERE id=300")
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                      (301, 'demo', 'NEW PRIVATE INPUT', 'running', 'original'))
        b.accept(self.cfg, self.action_callback(cancel['token'], 302, 800))
        with b.db() as c:
            self.assertNotEqual(b.get(c, 'cancel-job'), '301')
        jobs = {r['id']: r for r in self.rows('jobs')}
        self.assertEqual(jobs[301]['state'], 'running')
        self.assertEqual(jobs[301]['prompt'], 'NEW PRIVATE INPUT')
        self.assertNotIn('PRIVATE INPUT', json.dumps(self.rows('outbox')))

    def test_split_status_binds_all_buttons_to_last_part_receipt(self):
        rows = []
        for i in range(32):
            meta = self.make_thread('thread-' + str(i))
            meta['name'] = 'Long conversation ' + str(i) + ' very descriptive title' * 8
            rows.append(meta)
        with patch.object(b, 'running_threads', return_value=(rows, 0)), \
                patch.object(b, 'interrupted_threads', return_value=[]):
            with b.db() as c:
                b.status_menu(c, self.cfg, 'status:long')
        parts = self.rows('outbox')
        self.assertGreater(len(parts), 1)
        last_id = parts[-1]['id']
        self.assertEqual({r['menu_id'] for r in self.rows('actions')}, {last_id})
        self.assertEqual([r['id'] for r in self.rows('keyboards')], [last_id])
        api = self.fake_api()
        while b.deliver(api, self.cfg):
            pass
        sent = {r['id']: r for r in self.rows('outbox')}
        last_receipt = sent[last_id]['receipt']
        calls = api.call.call_args_list
        self.assertTrue(all(call.args[0] == 'sendMessage' for call in calls))
        self.assertTrue(all('reply_markup' not in call.kwargs for call in calls[:-1]))
        self.assertIn('reply_markup', calls[-1].kwargs)
        with b.db() as c:
            self.assertTrue(b.valid_menu(c, last_id, last_receipt))
            self.assertFalse(b.valid_menu(c, last_id, sent['status:long']['receipt']))
        detail = next(r for r in self.rows('actions') if r['kind'] == 'detail')
        b.accept(self.cfg, self.action_callback(detail['token'], 400, last_receipt))
        b.deliver(api, self.cfg)
        self.assertEqual(api.call.call_args_list[-1].args[0], 'editMessageText')
        self.assertEqual(api.call.call_args_list[-1].kwargs['message_id'], last_receipt)
        self.assertEqual(self.rows('jobs'), [])


if __name__ == '__main__':
    unittest.main()
