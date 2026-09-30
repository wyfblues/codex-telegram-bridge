"""Offline regressions for per-turn delivery and safe conversation continuation."""
from contextlib import closing
import io
import fcntl
import json
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_bridge as support

b = support.b


class ReliabilityTests(unittest.TestCase):
    # Reuse the temporary databases, never the installed bridge state or credentials.
    update = support.BridgeTests.update
    rows = support.BridgeTests.rows

    def setUp(self):
        support.BridgeTests.setUp(self)
        self.cfg['projects']['demo'] = str(b.ROOT)
        self.guards = [
            patch.object(b.API, 'call', side_effect=AssertionError('Unexpected Bot API call')),
            patch.object(b.subprocess, 'Popen', side_effect=AssertionError('Unexpected process launch')),
            patch.object(b.subprocess, 'run', side_effect=AssertionError('Unexpected subprocess')),
            patch.object(b, 'open_rollout_paths', return_value=set()),
        ]
        for guard in self.guards:
            guard.start()

    def tearDown(self):
        for guard in reversed(self.guards):
            guard.stop()
        support.BridgeTests.tearDown(self)

    def test_loaded_desktop_writer_uses_queue_without_taking_lock(self):
        meta = self.make_thread('desktop', source='vscode')
        locks = b.THREAD_DB.parent / 'thread-writer-locks'
        locks.mkdir(exist_ok=True)
        lock = locks / 'desktop.lock'
        with lock.open('wb') as writer:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(b.thread_writer_busy('desktop'))
            with patch.object(b, 'desktop_available', return_value=True), b.db() as c:
                reply = b.enqueue_existing(c, self.cfg, 900, 'test', meta)
            self.assertIn('将交给桌面原任务', reply)
            self.assertEqual(self.rows('jobs')[0]['thread'], 'desktop')
            self.assertEqual(self.rows('desktop_dispatch')[0]['phase'], 'prepared')
            self.assertTrue(b.thread_writer_busy('desktop'))
        self.assertFalse(b.thread_writer_busy('desktop'))
        self.assertTrue(lock.exists())

    def test_stderr_categories_do_not_persist_secrets(self):
        stderr = 'Error: thread abc already has an active writer; token=PRIVATE\nUser prompt: SECRET'
        self.assertEqual(b.classify_failure(stderr), 'WRITER_BUSY')
        self.assertEqual(b.safe_process_error(stderr), 'already has an active writer')
        self.assertEqual(b.classify_failure('cannot resume paginated thread'), 'UNSUPPORTED_RESUME')
        self.assertNotIn('SECRET', b.safe_process_error('SECRET'))

    def test_early_cli_exit_is_recorded_without_claiming_completion(self):
        class StopWorker(BaseException):
            pass
        cli = Mock(pid=999900)
        cli.stdin = io.StringIO()
        cli.stdout = io.StringIO('')
        cli.stderr = io.StringIO('Error: thread original already has an active writer')
        cli.poll.return_value = 1
        cli.wait.return_value = 1
        awake = Mock()
        awake.poll.return_value = 0
        with b.db() as c:
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', (900, 'demo', 'test', 'queued', ''))
        with patch.object(b.subprocess, 'Popen', side_effect=[cli, awake]), \
                patch.object(b.time, 'sleep', side_effect=StopWorker):
            with self.assertRaises(StopWorker):
                b.worker(self.cfg)
        self.assertEqual(self.rows('jobs')[0]['state'], 'failed')
        self.assertEqual(self.rows('process_diagnostics')[0]['exit_code'], 1)
        self.assertEqual(self.rows('process_diagnostics')[0]['stage'], 'launch')
        self.assertEqual(self.rows('job_diagnostics')[0]['code'], 'WRITER_BUSY')
        self.assertNotIn('原始日志', self.rows('outbox')[0]['text'])

    def make_thread(self, thread='original', kind='task_complete', source='exec'):
        path = b.ROOT / (thread + '.jsonl')
        self.append_event(path, 'baseline', kind=kind)
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute('''INSERT INTO threads(id,source,cwd,rollout_path,updated_at,title)
                VALUES (?,?,?,?,?,?)''',
                (thread, source, str(b.ROOT), str(path), int(time.time()), 'Conversation ' + thread))
        return b.thread_metadata(thread)

    def append_event(self, path, turn, kind='task_complete', answer='Final answer'):
        event = {'type': 'event_msg', 'timestamp': '2026-09-30T12:00:00Z',
                 'payload': {'type': kind, 'turn_id': turn, 'last_agent_message': answer}}
        with Path(path).open('a') as f:
            f.write(json.dumps(event) + '\n')

    def select_default(self, thread='original'):
        with b.db() as c:
            b.put(c, 'project', 'demo')
            b.put(c, 'thread:demo', thread)
            b.put(c, 'await-session:demo', '0')

    def final_payload(self, thread, turn, text):
        return {'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                'cwd': str(b.ROOT), 'last-assistant-message': text,
                'input-messages': ['PRIVATE INPUT MUST NOT BE FORWARDED']}

    def test_legacy_bot_thread_receives_later_turns_once_in_both_delivery_orders(self):
        meta = self.make_thread()
        with b.db() as c:
            b.put(c, 'bridge-thread:original', '1')
        b.baseline_completions()

        # The observer and notify callback may arrive in either order.
        self.append_event(meta['rollout_path'], 'first', answer='First result')
        b.scan_completions()
        b.notify(self.final_payload('original', 'first', 'First result'))
        b.notify(self.final_payload('original', 'second', 'Second result'))
        self.append_event(meta['rollout_path'], 'second', answer='Second result')
        b.scan_completions()
        b.scan_completions()

        notices = self.rows('outbox')
        self.assertEqual({r['id'] for r in notices},
                         {'notice:original:first', 'notice:original:second'})
        self.assertEqual(len(notices), 2)
        self.assertIn('First result', notices[0]['text'])
        self.assertIn('Second result', notices[1]['text'])
        self.assertNotIn('PRIVATE INPUT', json.dumps(notices))

    def test_default_session_is_bound_when_accepted_and_uses_existing_thread_checks(self):
        self.make_thread()
        self.make_thread('other')
        self.select_default()
        b.accept(self.cfg, self.update(100, 'Continue the original conversation'))
        job = self.rows('jobs')[0]
        target = self.rows('reply_jobs')[0]
        self.assertEqual(job['thread'], 'original')
        self.assertEqual(target, {'job_id': 100, 'thread': 'original', 'cwd': str(b.ROOT)})
        execution = self.rows('execution_targets')[0]
        original_arguments = b.resume_arguments(b.thread_metadata('original'))
        self.assertEqual(execution['cwd'], str(b.ROOT))
        self.assertEqual(json.loads(execution['args']), original_arguments)

        # Navigation after acceptance must not retarget the queued request.
        with b.db() as c:
            b.put(c, 'thread:demo', 'other')
            b.put(c, 'selected:demo', 'other')
            b.put(c, 'project', 'another-project')
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute("UPDATE threads SET model='changed-model' WHERE id='original'")
        self.assertEqual(self.rows('jobs')[0]['thread'], 'original')
        self.assertEqual(self.rows('reply_jobs')[0]['thread'], 'original')
        self.assertEqual(json.loads(self.rows('execution_targets')[0]['args']), original_arguments)

    def test_default_session_rejects_archived_missing_and_unsupported_settings(self):
        for number, field, value in [
            (101, 'archived', 1),
            (102, 'sandbox_policy', '{"type":"workspace-write"}'),
            (103, 'model_provider', None),
        ]:
            thread = 'blocked-' + str(number)
            self.make_thread(thread)
            with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
                c.execute('UPDATE threads SET ' + field + '=? WHERE id=?', (value, thread))
            self.select_default(thread)
            with self.subTest(field=field):
                b.accept(self.cfg, self.update(number, 'Do not create a replacement'))
                self.assertEqual(self.rows('jobs'), [])
                self.assertEqual(self.rows('reply_jobs'), [])
        self.select_default('missing-thread')
        b.accept(self.cfg, self.update(104, 'Missing original'))
        self.assertEqual(self.rows('jobs'), [])

    def test_default_session_refuses_live_or_unverifiable_turn(self):
        meta = self.make_thread(kind='task_started')
        self.select_default()
        with patch.object(b, 'open_rollout_paths', return_value={meta['rollout_path']}):
            b.accept(self.cfg, self.update(110, 'Do not overlap the desktop task'))
        with patch.object(b, 'open_rollout_paths', side_effect=OSError('Inspection unavailable')):
            b.accept(self.cfg, self.update(111, 'Do not guess that the process ended'))
        self.assertEqual(self.rows('jobs'), [])
        self.assertEqual(self.rows('reply_jobs'), [])

    def test_interrupted_state_is_consistent_and_can_be_explicitly_resumed(self):
        meta = self.make_thread(kind='task_started')
        self.select_default()
        self.assertEqual(b.thread_activity(meta, paths={meta['rollout_path']}), 'active')
        self.assertEqual(b.thread_activity(meta, paths=set()), 'interrupted')
        with patch.object(b, 'open_rollout_paths', side_effect=OSError('Inspection unavailable')):
            self.assertEqual(b.thread_activity(meta), 'unknown')
        with b.db() as c:
            current = b.current_text(c, self.cfg)
            status = b.status_text(c, self.cfg)
        self.assertNotIn('运行中', current)
        self.assertIn('中断', current)
        self.assertIn('没有正在运行', status)
        b.accept(self.cfg, self.update(120, 'Continue after the interrupted process'))
        self.assertEqual(self.rows('jobs')[0]['thread'], 'original')
        self.assertEqual(self.rows('reply_jobs')[0]['thread'], 'original')

    def test_session_browsing_search_pagination_and_read_failure_preserve_target(self):
        self.make_thread()
        self.select_default()
        with b.db() as c:
            b.put(c, 'selected:demo', 'original')
        b.accept(self.cfg, self.update(130, '/sessions'))
        b.accept(self.cfg, self.update(131, '/sessions no-matching-title'))
        with b.db() as c:
            b.session_menu(c, self.cfg, 'demo', 'test:page', page=2)
        with patch.object(b, 'recent_threads', side_effect=sqlite3.OperationalError('Unavailable')):
            b.accept(self.cfg, self.update(132, '/sessions'))
        with b.db() as c:
            self.assertEqual(b.get(c, 'selected:demo'), 'original')
            self.assertEqual(b.get(c, 'thread:demo'), 'original')
            self.assertEqual(b.get(c, 'await-session:demo'), '0')
        b.accept(self.cfg, self.update(133, 'Continue without selecting again'))
        self.assertEqual(self.rows('jobs')[0]['thread'], 'original')

    def test_first_project_choice_requires_selection_but_browsing_keeps_new_mode(self):
        b.accept(self.cfg, self.update(140, '/use demo'))
        b.accept(self.cfg, self.update(141, 'Do not guess a conversation'))
        self.assertEqual(self.rows('jobs'), [])
        b.accept(self.cfg, self.update(142, '/new'))
        b.accept(self.cfg, self.update(143, '/sessions'))
        with b.db() as c:
            self.assertEqual(b.get(c, 'await-session:demo'), '0')
            self.assertEqual(b.get(c, 'selected:demo'), '')
            self.assertEqual(b.get(c, 'thread:demo'), '')
        b.accept(self.cfg, self.update(144, 'Create the explicitly requested new conversation'))
        self.assertEqual(len(self.rows('jobs')), 1)
        self.assertEqual(self.rows('reply_jobs'), [])

    def seed_recovery_jobs(self):
        with b.db() as c:
            c.executemany('INSERT INTO jobs VALUES (?,?,?,?,?)', [
                (200, 'demo', 'RUNNING PRIVATE INPUT', 'running', 'original'),
                (201, 'demo', 'QUEUED PRIVATE INPUT', 'queued', ''),
                (202, 'demo', '', 'turn-ended', 'complete'),
            ])

    def test_worker_recovery_interrupts_only_running_jobs_and_does_not_replay(self):
        self.seed_recovery_jobs()
        with b.db() as c:
            b.recover_interrupted_jobs(c)
            b.recover_interrupted_jobs(c)
        jobs = {r['id']: r for r in self.rows('jobs')}
        self.assertEqual(jobs[200]['state'], 'interrupted')
        self.assertEqual(jobs[200]['prompt'], '')
        self.assertEqual(jobs[201]['state'], 'queued')
        self.assertEqual(jobs[201]['prompt'], 'QUEUED PRIVATE INPUT')
        self.assertEqual(jobs[202]['state'], 'turn-ended')
        self.assertEqual([r['id'] for r in self.rows('outbox')], ['interrupted:200'])
        self.assertNotIn('PRIVATE INPUT', json.dumps(self.rows('outbox')))

    def test_process_restart_recovery_also_quarantines_queued_jobs_once(self):
        self.seed_recovery_jobs()
        with b.db() as c:
            b.recover_interrupted_jobs(c, include_queued=True)
            b.recover_interrupted_jobs(c, include_queued=True)
        jobs = {r['id']: r for r in self.rows('jobs')}
        self.assertEqual(jobs[200]['state'], 'interrupted')
        self.assertEqual(jobs[201]['state'], 'interrupted')
        self.assertEqual(jobs[201]['prompt'], '')
        self.assertEqual(jobs[202]['state'], 'turn-ended')
        self.assertEqual({r['id'] for r in self.rows('outbox')},
                         {'interrupted:200', 'interrupted:201'})

    def supervisor_stop(self, iterations=1):
        stop = threading.Event()
        calls = []

        def wait(_timeout):
            calls.append(None)
            if len(calls) >= iterations:
                stop.set()
            return stop.is_set()

        return stop, wait

    def test_supervisor_never_starts_second_worker_while_orphan_child_is_alive(self):
        self.seed_recovery_jobs()
        stop, wait = self.supervisor_stop(iterations=2)
        child = Mock()
        child.poll.return_value = None
        dead_worker = Mock()
        dead_worker.is_alive.return_value = False
        with patch.object(b, 'ACTIVE_PROCESS', child), \
                patch.object(b, 'WORKER_THREAD', dead_worker), \
                patch.object(b.threading, 'Thread') as create_thread, \
                patch.object(b, 'worker') as worker, \
                patch.object(b, 'recover_interrupted_jobs') as recover, \
                patch.object(stop, 'wait', side_effect=wait):
            b.worker_supervisor(self.cfg, stop_event=stop)
        create_thread.assert_not_called()
        worker.assert_not_called()
        recover.assert_not_called()
        self.assertEqual(self.rows('jobs')[0]['state'], 'running')

    def test_supervisor_recovers_dead_worker_once_before_starting_replacement(self):
        self.seed_recovery_jobs()
        stop, wait = self.supervisor_stop(iterations=2)
        exited_child = Mock()
        exited_child.poll.return_value = 1
        dead_worker = Mock()
        dead_worker.is_alive.return_value = False
        replacement = Mock()
        replacement.is_alive.return_value = True
        with patch.object(b, 'ACTIVE_PROCESS', exited_child), \
                patch.object(b, 'WORKER_THREAD', dead_worker), \
                patch.object(b.threading, 'Thread', return_value=replacement) as create_thread, \
                patch.object(b, 'worker') as worker, \
                patch.object(stop, 'wait', side_effect=wait):
            b.worker_supervisor(self.cfg, stop_event=stop)
        create_thread.assert_called_once()
        replacement.start.assert_called_once()
        worker.assert_not_called()  # The replacement is a mock thread, never a live executor.
        jobs = {r['id']: r for r in self.rows('jobs')}
        self.assertEqual(jobs[200]['state'], 'interrupted')
        self.assertEqual(jobs[201]['state'], 'queued')
        self.assertEqual(len(self.rows('outbox')), 1)

    def test_supervisor_does_not_restart_if_recovery_cannot_be_persisted(self):
        stop, wait = self.supervisor_stop()
        with patch.object(b, 'ACTIVE_PROCESS', None), \
                patch.object(b, 'WORKER_THREAD', None), \
                patch.object(b.threading, 'Thread') as create_thread, \
                patch.object(b, 'worker') as worker, \
                patch.object(b, 'recover_interrupted_jobs',
                             side_effect=sqlite3.OperationalError('Database unavailable')), \
                patch.object(stop, 'wait', side_effect=wait):
            b.worker_supervisor(self.cfg, stop_event=stop)
        create_thread.assert_not_called()
        worker.assert_not_called()

    def test_delayed_old_cancel_watcher_never_signals_the_next_job(self):
        """Pause after reading cancel=True, finish that job, then resume in the next job."""
        class StopWorker(Exception):
            pass

        actual_thread = threading.Thread
        heartbeat_entered = threading.Event()
        heartbeat_release = threading.Event()
        watcher_done = threading.Event()
        watchers, watcher_errors = [], []

        def heartbeat(_key, error=False):
            if threading.current_thread().name == 'delayed-cancel-regression':
                heartbeat_entered.set()
                if not heartbeat_release.wait(3):
                    raise AssertionError('The second fake job did not release the old watcher')

        class CapturedWatcher:
            def __new__(cls, target, **kwargs):
                if kwargs.get('name') == 'cli-stderr-reader':
                    return actual_thread(target=target, **kwargs)
                return super().__new__(cls)

            def __init__(self, target, **_kwargs):
                self.target, self.thread = target, None
                watchers.append(self)

            def start(self):
                if len(watchers) != 1:
                    return  # Only the old cancellation watcher needs to execute.

                def run():
                    try:
                        self.target()
                    except BaseException as error:
                        watcher_errors.append(error)
                    finally:
                        watcher_done.set()

                self.thread = actual_thread(target=run, daemon=True,
                                            name='delayed-cancel-regression')
                self.thread.start()

            def join(self, timeout=None):
                # Model the existing timeout expiring while the heartbeat remains blocked.
                if self.thread:
                    self.thread.join(timeout=0)

        case = self

        class FakeProcess:
            def __init__(self, thread, pid, first):
                self.pid, self.returncode = pid, None
                self.stdin = io.StringIO()
                self.stderr = io.StringIO()
                self.stdout = self.events(thread, first)

            def events(self, thread, first):
                if first:
                    case.assertTrue(heartbeat_entered.wait(2), 'Old watcher never read cancellation')
                else:
                    # The worker has now rebound its process/job/finished loop locals.
                    heartbeat_release.set()
                    case.assertTrue(watcher_done.wait(2), 'Delayed watcher did not finish')
                yield json.dumps({'type': 'thread.started', 'thread_id': thread}) + '\n'
                yield json.dumps({'type': 'turn.completed'}) + '\n'

            def poll(self):
                return self.returncode

            def wait(self, **_kwargs):
                self.returncode = 0
                return 0

        class FakeAwake:
            def poll(self):
                return 0

        with b.db() as c:
            c.executemany('INSERT INTO jobs VALUES (?,?,?,?,?)', [
                (410, 'demo', 'First isolated request', 'queued', ''),
                (411, 'demo', 'Second isolated request', 'queued', ''),
            ])
            b.put(c, 'cancel-job', 410)
        first, second = FakeProcess('first', 900410, True), FakeProcess('second', 900411, False)
        with patch.object(b.subprocess, 'Popen', side_effect=[first, FakeAwake(), second, FakeAwake()]), \
                patch.object(b.threading, 'Thread', CapturedWatcher), \
                patch.object(b, 'record_health', side_effect=heartbeat), \
                patch.object(b.time, 'sleep', side_effect=StopWorker), \
                patch.object(b, 'ACTIVE_PROCESS', None), \
                patch.object(b.os, 'killpg', side_effect=ProcessLookupError('Test-only signal guard')) as kill:
            try:
                with self.assertRaises(StopWorker):
                    b.worker(self.cfg)
            finally:
                heartbeat_release.set()
                for watcher in watchers:
                    if watcher.thread:
                        watcher.thread.join(timeout=3)
                        self.assertFalse(watcher.thread.is_alive())
            kill.assert_not_called()
        self.assertEqual(watcher_errors, [])
        self.assertEqual(len(watchers), 2)
        jobs = {r['id']: r for r in self.rows('jobs')}
        self.assertEqual(jobs[410]['state'], 'turn-ended')
        self.assertEqual(jobs[411]['state'], 'turn-ended')
        self.assertEqual(jobs[411]['thread'], 'second')


if __name__ == '__main__':
    unittest.main()
