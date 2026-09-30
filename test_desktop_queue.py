"""Offline tests for exactly-once Desktop submission and correlated completion."""
import json
from pathlib import Path
import sqlite3
import subprocess
import time
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import test_reliability as support
b = support.b
THREAD = '00000000-0000-4000-8000-000000000001'
QUEUE = '00000000-0000-4000-8000-000000000002'

class DesktopQueueTests(unittest.TestCase):
    setUp = support.ReliabilityTests.setUp
    tearDown = support.ReliabilityTests.tearDown
    rows = support.ReliabilityTests.rows
    make_thread = support.ReliabilityTests.make_thread
    append_event = support.ReliabilityTests.append_event

    def prepare(self, uid=950):
        meta = self.make_thread(THREAD, source='vscode')
        with patch.object(b,'desktop_available',return_value=True), b.db() as c:
            result = b.enqueue_existing(c,self.cfg,uid,'A harmless request',meta)
            self.assertIn('将交给桌面',result)
            c.execute("UPDATE jobs SET state='running' WHERE id=?",(uid,))
            job = dict(c.execute('SELECT * FROM jobs WHERE id=?',(uid,)).fetchone())
            execution = dict(c.execute('SELECT * FROM execution_targets WHERE job_id=?',(uid,)).fetchone())
        return meta,job,execution

    def submit(self, job, execution, outcome=None):
        result = Mock(returncode=0,stdout=f'Queued message {QUEUE} for thread {THREAD}.\n')
        with patch.object(b,'desktop_available',return_value=True), \
             patch.object(b.subprocess,'run',side_effect=outcome,return_value=result) as run:
            b.dispatch_desktop(job,execution)
        return run

    def record_user(self, meta, turn='matched'):
        marker=self.rows('desktop_dispatch')[0]['marker']
        event={'type':'response_item','payload':{'role':'user',
               'content':[{'text':b.transport_prompt('test',marker)}],
               'internal_chat_message_metadata_passthrough':{'turn_id':turn}}}
        with open(meta['rollout_path'],'a') as f: f.write(json.dumps(event)+'\n')

    def test_acceptance_is_not_completion_and_does_not_override_settings(self):
        meta,job,execution=self.prepare()
        run=self.submit(job,execution)
        args=run.call_args.args[0]
        self.assertEqual(args[:4],[b.CODEX,'queue','--thread',THREAD])
        self.assertNotIn('-m',args)
        self.assertNotIn('-c',args)
        self.assertNotIn('shell',run.call_args.kwargs)
        self.assertEqual(self.rows('jobs')[0]['state'],'running')
        self.assertEqual(self.rows('jobs')[0]['prompt'],'')
        self.assertEqual(self.rows('desktop_dispatch')[0]['queue_id'],QUEUE)
        self.assertEqual(self.rows('outbox'),[])

    def test_double_dispatch_and_restart_never_submit_twice(self):
        meta,job,execution=self.prepare()
        self.submit(job,execution)
        with b.db() as c: b.recover_interrupted_jobs(c,include_queued=True)
        run=self.submit(job,execution)
        run.assert_not_called()
        self.assertEqual(self.rows('jobs')[0]['state'],'running')
        self.record_user(meta)
        self.append_event(meta['rollout_path'],'matched')
        b.reconcile_desktop_jobs()
        self.assertEqual(self.rows('jobs')[0]['state'],'turn-ended')
        self.assertEqual(self.rows('desktop_dispatch')[0]['turn_id'],'matched')

    def test_unrelated_turn_and_incomplete_record_cannot_complete_job(self):
        meta,job,execution=self.prepare(); self.submit(job,execution)
        self.append_event(meta['rollout_path'],'unrelated')
        b.reconcile_desktop_jobs()
        self.assertEqual(self.rows('jobs')[0]['state'],'running')
        self.record_user(meta)
        b.reconcile_desktop_jobs()
        event=json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':'matched'}})
        with open(meta['rollout_path'],'a') as f: f.write(event)
        b.reconcile_desktop_jobs()
        self.assertEqual(self.rows('jobs')[0]['state'],'running')
        with open(meta['rollout_path'],'a') as f: f.write('\n')
        b.reconcile_desktop_jobs()
        self.assertEqual(self.rows('jobs')[0]['state'],'turn-ended')
        self.assertEqual(self.rows('outbox'),[])  # Existing notify/observer owns success notices.

    def test_queue_timeout_remains_uncertain_and_is_not_retried(self):
        meta,job,execution=self.prepare()
        self.submit(job,execution,subprocess.TimeoutExpired('queue',15))
        self.assertEqual(self.rows('desktop_dispatch')[0]['phase'],'uncertain')
        self.submit(job,execution).assert_not_called()
        with b.db() as c: c.execute('UPDATE desktop_dispatch SET submitted=?',(time.time()-90,))
        b.reconcile_desktop_jobs(); b.reconcile_desktop_jobs()
        self.assertEqual(len(self.rows('outbox')),1)
        self.assertIn('不会重复提交',self.rows('outbox')[0]['text'])
        self.record_user(meta); self.append_event(meta['rollout_path'],'matched')
        b.reconcile_desktop_jobs()
        self.assertEqual(self.rows('jobs')[0]['state'],'turn-ended')

    def test_reply_receipt_mismatch_is_not_claimed_accepted(self):
        meta,job,execution=self.prepare()
        bad=Mock(returncode=0,stdout=f'Queued message {QUEUE} for thread {QUEUE}.\n')
        with patch.object(b,'desktop_available',return_value=True), patch.object(b.subprocess,'run',return_value=bad):
            b.dispatch_desktop(job,execution)
        self.assertEqual(self.rows('desktop_dispatch')[0]['phase'],'uncertain')

    def test_changed_settings_prevent_submission(self):
        meta,job,execution=self.prepare()
        with closing(sqlite3.connect(b.THREAD_DB)) as c, c:
            c.execute("UPDATE threads SET model='changed'")
        self.submit(job,execution).assert_not_called()
        self.assertEqual(self.rows('job_diagnostics')[0]['code'],'SETTINGS_CHANGED')

    def test_active_thread_prevents_submission(self):
        meta,job,execution=self.prepare()
        with patch.object(b,'thread_activity',return_value='active'):
            self.submit(job,execution).assert_not_called()
        self.assertEqual(self.rows('job_diagnostics')[0]['code'],'TARGET_BUSY')

    def test_expired_prepared_request_is_not_sent(self):
        meta,job,execution=self.prepare()
        with b.db() as c: c.execute('UPDATE desktop_dispatch SET created=?',(time.time()-1000,))
        self.submit(job,execution).assert_not_called()
        self.assertEqual(self.rows('job_diagnostics')[0]['code'],'EXPIRED')

    def test_offline_desktop_refuses_intake(self):
        meta=self.make_thread(THREAD,source='vscode')
        with patch.object(b,'desktop_available',return_value=False), b.db() as c:
            self.assertIn('未运行',b.enqueue_existing(c,self.cfg,950,'request',meta))
        self.assertEqual(self.rows('jobs'),[])

    def test_cancel_submitted_desktop_does_not_claim_stop(self):
        meta,job,execution=self.prepare(); self.submit(job,execution)
        with b.db() as c:
            self.assertIn('暂不支持远程停止',b.cancel_job(c))
            self.assertEqual(b.get(c,'cancel-job'),'')
        self.assertEqual(self.rows('jobs')[0]['state'],'running')
        self.record_user(meta); self.append_event(meta['rollout_path'],'matched',kind='turn_aborted')
        b.reconcile_desktop_jobs()
        self.assertEqual(self.rows('jobs')[0]['state'],'cancelled')

    def test_unsubmitted_job_can_still_be_cancelled(self):
        meta,job,execution=self.prepare()
        with b.db() as c:
            c.execute("UPDATE jobs SET state='queued'")
            self.assertIn('已取消',b.cancel_job(c))
        self.submit(job,execution).assert_not_called()
        self.assertEqual(self.rows('jobs')[0]['state'],'cancelled')

    def test_launch_failure_records_no_submission(self):
        meta,job,execution=self.prepare()
        self.submit(job,execution,FileNotFoundError())
        self.assertEqual(self.rows('jobs')[0]['state'],'failed')
        self.assertIn('本次未提交',self.rows('outbox')[0]['text'])

    def test_desktop_retains_workspace_policy_without_cli_conversion(self):
        meta=self.make_thread(THREAD,source='vscode')
        meta['sandbox_policy']='{"type":"workspace-write","writable_roots":["/safe"]}'
        snapshot=b.continuation_snapshot(meta)
        self.assertEqual(snapshot['sandbox_policy'],meta['sandbox_policy'])

    def test_idle_reconciliation_never_starts_process_or_calls_model(self):
        b.reconcile_desktop_jobs()
        self.assertEqual(self.rows('desktop_dispatch'),[])

if __name__=='__main__': unittest.main()
