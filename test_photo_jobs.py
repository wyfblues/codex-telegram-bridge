"""Offline incoming-photo intake, worker preparation, and continuation regressions."""
import io
import json
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import Mock, patch

import test_reliability as support

b = support.b
API_CLASS = b.API
PNG = b'\x89PNG\r\n\x1a\n' + b'incoming-test-image'
THREAD = '00000000-0000-4000-8000-000000000011'
QUEUE = '00000000-0000-4000-8000-000000000012'


class PhotoJobTests(unittest.TestCase):
    tearDown = support.ReliabilityTests.tearDown
    rows = support.ReliabilityTests.rows
    update = support.ReliabilityTests.update
    make_thread = support.ReliabilityTests.make_thread
    append_event = support.ReliabilityTests.append_event
    select_default = support.ReliabilityTests.select_default

    def setUp(self):
        support.ReliabilityTests.setUp(self)
        self.cfg['image_receive_enabled'] = True
        self.api = Mock()
        self.api.download_photo.return_value = PNG
        api_patch = patch.object(b, 'API', return_value=self.api)
        self.api_constructor = api_patch.start()
        self.addCleanup(api_patch.stop)

    def photo_update(self, uid=100, caption='帮我检查这张报错截图', **overrides):
        update = self.update(uid, '')
        message = update['message']
        message.pop('text')
        message.update({'caption': caption, 'photo': [
            {'file_id': 'small', 'width': 100, 'height': 80, 'file_size': 500},
            {'file_id': 'large', 'width': 1000, 'height': 800, 'file_size': 5000},
        ]})
        message.update(overrides)
        return update

    def select_thread(self, thread=THREAD, source='exec'):
        meta = self.make_thread(thread, source=source)
        self.select_default(thread)
        return meta

    def accept_photo(self, update=None):
        with patch.object(b, 'desktop_available', return_value=True):
            b.accept(self.cfg, update or self.photo_update())

    def prepared_desktop(self):
        meta = self.select_thread(source='vscode')
        self.accept_photo()
        with b.db() as c:
            c.execute("UPDATE jobs SET state='running' WHERE id=100")
            job = dict(c.execute('SELECT * FROM jobs WHERE id=100').fetchone())
            execution = dict(c.execute('SELECT * FROM execution_targets WHERE job_id=100').fetchone())
        return meta, job, execution

    def dispatch(self, job, execution, outcome=None):
        result = Mock(returncode=0, stdout=f'Queued message {QUEUE} for thread {THREAD}.\n')
        with patch.object(b, 'desktop_available', return_value=True), \
                patch.object(b, 'image_config', return_value=False), \
                patch.object(b.subprocess, 'run', side_effect=outcome, return_value=result) as run:
            b.dispatch_desktop(job, execution, self.cfg)
        return run

    def test_receive_and_execution_switches_both_gate_intake(self):
        self.select_thread()
        self.cfg['image_receive_enabled'] = False
        self.accept_photo(self.photo_update(101))
        self.assertIn('图片接收尚未启用', self.rows('outbox')[-1]['text'])
        self.cfg['image_receive_enabled'] = True
        self.cfg['execution_enabled'] = False
        self.accept_photo(self.photo_update(102))
        self.assertIn('远程执行尚未启用', self.rows('outbox')[-1]['text'])
        self.assertEqual(self.rows('jobs'), [])
        self.assertEqual(self.rows('job_photos'), [])
        self.api_constructor.assert_not_called()

    def test_untrusted_stale_album_and_captionless_updates_never_download_or_enqueue(self):
        self.select_thread()
        updates = [self.photo_update(101, **{'from': {'id': 999, 'is_bot': False}}),
                   self.photo_update(102, chat={'id': 123, 'type': 'group'}),
                   self.photo_update(103, date=time.time() - 901),
                   self.photo_update(104, media_group_id='album'),
                   self.photo_update(105, caption=' \n\t')]
        for update in updates:
            self.accept_photo(update)
        self.assertEqual(self.rows('jobs'), [])
        self.assertEqual(self.rows('job_photos'), [])
        self.assertEqual(len(self.rows('outbox')), 3)
        replies = '\n'.join(row['text'] for row in self.rows('outbox'))
        self.assertIn('15 分钟', replies)
        self.assertIn('相册', replies)
        self.assertIn('文字说明', replies)
        self.api_constructor.assert_not_called()

    def test_missing_selection_and_new_thread_mode_reject_photos(self):
        self.accept_photo(self.photo_update(101))
        self.assertIn('选择', self.rows('outbox')[-1]['text'])
        with b.db() as c:
            b.put(c, 'project', 'demo')
            b.put(c, 'await-session:demo', '0')
        self.accept_photo(self.photo_update(102))
        self.assertIn('已有会话', self.rows('outbox')[-1]['text'])
        self.assertEqual(self.rows('jobs'), [])
        self.api_constructor.assert_not_called()

    def test_selected_thread_binding_and_duplicate_update_are_durable(self):
        meta = self.select_thread()
        update = self.photo_update()
        self.accept_photo(update)
        self.accept_photo(update)
        self.assertEqual(len(self.rows('jobs')), 1)
        self.assertEqual(len(self.rows('job_photos')), 1)
        self.assertEqual(self.rows('jobs')[0]['thread'], meta['id'])
        self.assertEqual(self.rows('jobs')[0]['prompt'], update['message']['caption'])
        photo = self.rows('job_photos')[0]
        self.assertEqual(photo['file_id'], 'large')
        self.assertEqual(photo['received'], update['message']['date'])
        self.assertIsNone(photo['path'])
        self.assertEqual(self.rows('execution_targets')[0]['cwd'], meta['cwd'])
        self.assertEqual(json.loads(self.rows('execution_targets')[0]['args']), b.resume_arguments(meta))
        self.api_constructor.assert_not_called()

    def test_reply_receipt_overrides_selection_without_changing_it(self):
        self.select_thread('selected')
        target = self.make_thread('replied', source='exec')
        with b.db() as c:
            b.queue(c, 'notice:replied:turn', 'A completed result')
            c.execute("UPDATE outbox SET state='sent',receipt=900 WHERE id='notice:replied:turn'")
        update = self.photo_update()
        update['message']['reply_to_message'] = {'message_id': 900, 'chat': {'id': 123},
                                                'from': {'id': 456, 'is_bot': True}}
        self.accept_photo(update)
        self.assertEqual(self.rows('jobs')[0]['thread'], target['id'])
        self.assertEqual(self.rows('reply_jobs')[0]['thread'], target['id'])
        with b.db() as c:
            self.assertEqual(b.get(c, 'thread:demo'), 'selected')

    def test_unknown_reply_never_falls_back_to_selected_thread(self):
        self.select_thread()
        self.accept_photo(self.photo_update(reply_to_message={
            'message_id': 999, 'chat': {'id': 123}, 'from': {'id': 456, 'is_bot': True}}))
        self.assertEqual(self.rows('jobs'), [])
        self.assertIn('未关联', self.rows('outbox')[-1]['text'])
        self.api_constructor.assert_not_called()

    def test_download_waits_for_worker_and_cached_snapshot_never_redownloads(self):
        self.select_thread()
        self.accept_photo()
        self.api_constructor.assert_not_called()
        path = b.prepare_photo(100, self.cfg)
        self.assertEqual(Path(path).read_bytes(), PNG)
        self.assertEqual(b.prepare_photo(100, self.cfg), path)
        self.api_constructor.assert_called_once_with(self.cfg)
        self.api.download_photo.assert_called_once_with('large')
        self.assertEqual(self.rows('job_photos')[0]['file_id'], '')

    def test_tampered_cache_is_rejected_without_redownload(self):
        self.select_thread()
        self.accept_photo()
        path = b.prepare_photo(100, self.cfg)
        Path(path).write_bytes(PNG[:-1] + b'X')
        with self.assertRaises(b.JobRejected) as error:
            b.prepare_photo(100, self.cfg)
        self.assertEqual(error.exception.code, 'IMAGE_UNAVAILABLE')
        self.api.download_photo.assert_called_once_with('large')

    def test_expired_photo_never_downloads_or_dispatches(self):
        _, job, execution = self.prepared_desktop()
        with b.db() as c:
            c.execute('UPDATE job_photos SET received=?', (time.time() - 901,))
        self.dispatch(job, execution).assert_not_called()
        self.assertEqual(self.rows('job_diagnostics')[0]['code'], 'EXPIRED')
        self.assertEqual(self.rows('jobs')[0]['state'], 'failed')
        self.api_constructor.assert_not_called()

    def test_cancellation_during_download_prevents_desktop_submission(self):
        _, job, execution = self.prepared_desktop()

        def cancel_during_download(file_id):
            with b.db() as c:
                b.put(c, 'cancel-job', job['id'])
            return PNG

        self.api.download_photo.side_effect = cancel_during_download
        self.dispatch(job, execution).assert_not_called()
        self.api.download_photo.assert_called_once_with('large')
        self.assertEqual(self.rows('job_diagnostics')[0]['code'], 'CANCELLED')
        self.assertNotEqual(self.rows('jobs')[0]['state'], 'running')
        self.assertNotEqual(self.rows('desktop_dispatch')[0]['phase'], 'accepted')

    def test_expiry_during_download_prevents_desktop_submission(self):
        _, job, execution = self.prepared_desktop()
        now = [time.time()]

        def finish_after_expiry(file_id):
            now[0] += 901
            return PNG

        self.api.download_photo.side_effect = finish_after_expiry
        with patch.object(b.time, 'time', side_effect=lambda: now[0]):
            self.dispatch(job, execution).assert_not_called()
        self.api.download_photo.assert_called_once_with('large')
        self.assertEqual(self.rows('job_diagnostics')[0]['code'], 'EXPIRED')
        self.assertEqual(self.rows('jobs')[0]['state'], 'failed')
        self.assertNotEqual(self.rows('desktop_dispatch')[0]['phase'], 'accepted')

    def test_download_failure_does_not_submit_or_expose_raw_error(self):
        _, job, execution = self.prepared_desktop()
        self.api.download_photo.side_effect = TimeoutError('SECRET-TOKEN raw network error')
        self.dispatch(job, execution).assert_not_called()
        self.assertEqual(self.rows('job_diagnostics')[0]['code'], 'IMAGE_DOWNLOAD_FAILED')
        self.assertEqual(self.rows('jobs')[0]['state'], 'failed')
        self.assertNotIn('SECRET-TOKEN', json.dumps(self.rows('outbox')))
        self.assertNotIn('raw network', json.dumps(self.rows('outbox')))

    def test_oversized_download_fails_before_dispatch_and_storage(self):
        _, job, execution = self.prepared_desktop()
        with patch.object(b.received_images, 'PHOTO_LIMIT', 10):
            self.dispatch(job, execution).assert_not_called()
        self.assertEqual(self.rows('job_diagnostics')[0]['code'], 'IMAGE_UNAVAILABLE')
        self.assertFalse((b.ROOT / 'incoming').exists())

    def test_desktop_prompt_carries_validated_image_and_original_thread(self):
        _, job, execution = self.prepared_desktop()
        self.make_thread('later-selection', source='vscode')
        self.select_default('later-selection')
        run = self.dispatch(job, execution)
        args = run.call_args.args[0]
        path = self.rows('job_photos')[0]['path']
        self.assertEqual(args[:4], [b.CODEX, 'queue', '--thread', THREAD])
        self.assertNotIn('--image', args)
        self.assertNotIn('-m', args)
        prompt = args[args.index('--message') + 1]
        self.assertIn(path, prompt)
        self.assertIn('image-viewing tool', prompt)
        self.assertIn('inspect its pixels', prompt)
        self.assertIn(job['prompt'], prompt)
        self.assertEqual(self.rows('desktop_dispatch')[0]['phase'], 'accepted')
        self.assertEqual(self.rows('jobs')[0]['thread'], THREAD)

    def test_timeout_and_restart_never_redownload_or_resubmit_image_request(self):
        _, job, execution = self.prepared_desktop()
        first = self.dispatch(job, execution, subprocess.TimeoutExpired('queue', 15))
        first.assert_called_once()
        self.assertEqual(self.rows('desktop_dispatch')[0]['phase'], 'uncertain')
        with b.db() as c:
            b.recover_interrupted_jobs(c, include_queued=True)
        self.dispatch(job, execution).assert_not_called()
        self.api.download_photo.assert_called_once_with('large')
        self.assertEqual(self.rows('jobs')[0]['prompt'], '')

    def test_cli_resume_receives_native_image_and_preserves_original_settings(self):
        meta = self.select_thread()
        self.accept_photo()

        class StopWorker(BaseException):
            pass

        class CapturedInput(io.StringIO):
            def close(self):
                self.captured = self.getvalue()
                super().close()

        process = Mock(pid=999900)
        process.stdin = CapturedInput()
        process.stdout = io.StringIO(json.dumps({'type': 'thread.started', 'thread_id': THREAD}) + '\n'
                                     + json.dumps({'type': 'turn.completed'}) + '\n')
        process.stderr = io.StringIO('')
        process.poll.return_value = 0
        process.wait.return_value = 0
        awake = Mock()
        awake.poll.return_value = 0
        with patch.object(b.subprocess, 'Popen', side_effect=[process, awake]) as popen, \
                patch.object(b.time, 'sleep', side_effect=StopWorker), \
                patch.object(b, 'image_config', return_value=False):
            with self.assertRaises(StopWorker):
                b.worker(self.cfg)
        args = popen.call_args_list[0].args[0]
        path = self.rows('job_photos')[0]['path']
        settings = b.resume_arguments(meta)
        self.assertEqual(args[4:4 + len(settings)], settings)
        self.assertEqual(args[-5:], ['resume', THREAD, '--image', path, '-'])
        self.assertIn(path, process.stdin.captured)
        self.assertEqual(self.rows('jobs')[0]['state'], 'turn-ended')
        self.api.download_photo.assert_called_once_with('large')


class PhotoDownloadTests(unittest.TestCase):
    setUp = support.ReliabilityTests.setUp
    tearDown = support.ReliabilityTests.tearDown

    def api(self, info, data=PNG, content_length=None):
        (b.ROOT / 'token').write_text('fake-test-token')
        api = API_CLASS(self.cfg)
        api.call = Mock(return_value=info)
        response = io.BytesIO(data)
        response.headers = {} if content_length is None else {'Content-Length': str(content_length)}
        response.read = Mock(wraps=response.read)
        api.http = Mock()
        api.http.open.return_value = response
        return api, response

    def test_getfile_size_and_download_content_length_are_bounded(self):
        with patch.object(b.received_images, 'PHOTO_LIMIT', 10):
            api, _ = self.api({'file_path': 'photos/photo.jpg', 'file_size': 11})
            with self.assertRaises(b.received_images.PhotoError):
                api.download_photo('test-file')
            api.http.open.assert_not_called()
            api, response = self.api({'file_path': 'photos/photo.jpg'}, content_length=11)
            with self.assertRaises(b.received_images.PhotoError):
                api.download_photo('test-file')
            response.read.assert_not_called()

    def test_unknown_content_length_reads_only_limit_plus_one(self):
        with patch.object(b.received_images, 'PHOTO_LIMIT', 10):
            api, response = self.api({'file_path': 'photos/photo.jpg'}, b'x' * 100)
            with self.assertRaises(b.received_images.PhotoError):
                api.download_photo('test-file')
            response.read.assert_called_once_with(11)

    def test_invalid_download_paths_never_reach_network(self):
        for path in ('../secret', '/absolute', 'photos//x', 'https://example.com/photo',
                     'photos/../secret', '', None):
            with self.subTest(path=path):
                api, _ = self.api({'file_path': path})
                with self.assertRaises(b.received_images.PhotoError):
                    api.download_photo('test-file')
                api.http.open.assert_not_called()

    def test_valid_file_download_is_requested_and_returned_without_transformation(self):
        api, response = self.api({'file_path': 'photos/file_1.jpg', 'file_size': len(PNG)})
        self.assertEqual(api.download_photo('test-file'), PNG)
        api.call.assert_called_once_with('getFile', file_id='test-file')
        response.read.assert_called_once_with(b.received_images.PHOTO_LIMIT + 1)
        self.assertEqual(api.http.open.call_args.kwargs['timeout'], 40)


if __name__ == '__main__':
    unittest.main()
