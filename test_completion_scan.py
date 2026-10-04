"""Offline regressions for moved rollouts and historical completion filtering."""
import json
from pathlib import Path
import sqlite3
import time
import unittest
from contextlib import closing

import test_reliability as support

b = support.b


class CompletionScanTests(unittest.TestCase):
    setUp = support.ReliabilityTests.setUp
    tearDown = support.ReliabilityTests.tearDown
    rows = support.ReliabilityTests.rows
    update = support.ReliabilityTests.update
    make_thread = support.ReliabilityTests.make_thread
    append_event = support.ReliabilityTests.append_event
    final_payload = support.ReliabilityTests.final_payload

    def move_thread(self, meta, directory):
        destination = Path(directory) / Path(meta['rollout_path']).name
        destination.parent.mkdir(exist_ok=True)
        Path(meta['rollout_path']).rename(destination)
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute('UPDATE threads SET rollout_path=?,updated_at=? WHERE id=?',
                      (str(destination), int(time.time()), meta['id']))
        return destination

    def test_archive_and_restore_keep_progress_and_deliver_new_turn_once(self):
        meta = self.make_thread()
        b.baseline_completions()
        archive = self.move_thread(meta, b.ROOT / 'archived_sessions')
        b.scan_completions()
        self.assertEqual(self.rows('outbox'), [])
        self.append_event(archive, 'fresh')
        b.scan_completions()
        restored = self.move_thread(b.thread_metadata(meta['id']), b.ROOT / 'sessions')
        b.scan_completions()
        self.append_event(restored, 'next')
        b.scan_completions()
        b.scan_completions()
        self.assertEqual([r['id'] for r in self.rows('outbox')],
                         ['notice:original:fresh', 'notice:original:next'])
        self.assertEqual(self.rows('completion_cursors')[0]['path'], str(restored))

    def test_legacy_checkpoint_moves_without_losing_unread_new_completion(self):
        meta = self.make_thread('legacy')
        old = Path(meta['rollout_path'])
        canonical = old.with_name('rollout-test-legacy.jsonl')
        old.rename(canonical)
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(canonical), 'legacy'))
        with b.db() as c:
            b.put(c, 'monitor_since', int(time.time()) - 1)
            c.execute('INSERT INTO cursors VALUES (?,?)', (str(canonical), canonical.stat().st_size))
        self.append_event(canonical, 'unread')
        self.move_thread(b.thread_metadata('legacy'), b.ROOT / 'archived_sessions')
        b.scan_completions()
        self.assertEqual([r['id'] for r in self.rows('outbox')], ['notice:legacy:unread'])

    def test_unknown_old_rollout_filters_history_but_delivers_recent_completion(self):
        b.baseline_completions()
        meta = self.make_thread('discovered')
        path = Path(meta['rollout_path'])
        historical = {'type': 'event_msg', 'timestamp': '2026-08-02T12:10:17Z',
                      'payload': {'type': 'task_complete', 'turn_id': 'historical',
                                  'last_agent_message': 'Historical answer'}}
        path.write_text(json.dumps(historical) + '\n')
        self.append_event(path, 'fresh')
        b.scan_completions()
        self.assertEqual([r['id'] for r in self.rows('outbox')], ['notice:discovered:fresh'])

    def test_truncated_rollout_does_not_replay_history(self):
        meta = self.make_thread()
        b.baseline_completions()
        path = Path(meta['rollout_path'])
        path.write_text(json.dumps({'type': 'event_msg', 'timestamp': '2000-01-01T00:00:00Z',
                                   'payload': {'type': 'task_complete', 'turn_id': 'old'}}) + '\n')
        b.scan_completions()
        self.assertEqual(self.rows('outbox'), [])
        self.append_event(path, 'new')
        b.scan_completions()
        self.assertEqual([r['id'] for r in self.rows('outbox')], ['notice:original:new'])

    def test_undated_or_invalid_completion_is_not_assumed_fresh(self):
        meta = self.make_thread()
        b.baseline_completions()
        for stamp in (None, 'invalid', '2026-10-04T18:04:00'):
            with Path(meta['rollout_path']).open('a') as f:
                f.write(json.dumps({'type': 'event_msg', 'timestamp': stamp,
                                    'payload': {'type': 'task_complete', 'turn_id': str(stamp)}}) + '\n')
        b.scan_completions()
        self.assertEqual(self.rows('outbox'), [])
        self.append_event(meta['rollout_path'], 'fresh')
        b.scan_completions()
        self.assertEqual([r['id'] for r in self.rows('outbox')], ['notice:original:fresh'])

    def test_observed_notice_uses_completion_time_and_callback_still_deduplicates(self):
        meta = self.make_thread()
        b.baseline_completions()
        with b.db() as c:
            b.put(c, 'monitor_since', int(time.time()) - 3600)
        completed = b.datetime.fromtimestamp(time.time() - 120, b.ZoneInfo('UTC'))
        with Path(meta['rollout_path']).open('a') as f:
            f.write(json.dumps({'type': 'event_msg', 'timestamp': completed.isoformat(),
                                'payload': {'type': 'task_complete', 'turn_id': 'recent',
                                            'last_agent_message': 'Recent answer'}}) + '\n')
        b.scan_completions()
        b.notify(self.final_payload('original', 'recent', 'Recent answer'))
        notices = self.rows('outbox')
        self.assertEqual(len(notices), 1)
        self.assertIn(completed.astimezone(b.ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M'),
                      notices[0]['text'])
