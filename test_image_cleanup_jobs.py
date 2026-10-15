"""Offline cache cleanup, active consumers, and expired-button integration."""
import os
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

import test_bridge as support

b = support.b
PNG = b'\x89PNG\r\n\x1a\n' + b'cleanup-test-image'


class ImageCleanupJobTests(unittest.TestCase):
    rows = support.BridgeTests.rows
    tearDown = support.BridgeTests.tearDown

    def setUp(self):
        support.BridgeTests.setUp(self)
        self.cfg['image_cleanup_enabled'] = True
        self.now = time.time()
        self.next_id = 100
        self.next_receipt = 200
        for target, options in (
                ('running_threads', {'return_value': ([], 0)}),
                ('API', {'side_effect': AssertionError('Unexpected network access')})):
            guard = patch.object(b, target, **options)
            guard.start()
            self.addCleanup(guard.stop)

    def age(self, path, days=50):
        stamp = self.now - days * b.retention.DAY
        os.utime(path, (stamp, stamp))
        with b.db() as c:
            c.execute('UPDATE image_retention SET last_used=? WHERE path=?', (stamp, str(path)))
        return stamp

    def incoming(self, state='turn-ended', thread='original', data=PNG, tracked=True):
        metadata = b.received_images.save_photo(b.ROOT, data)
        stamp = self.age(metadata['path'])
        self.next_id += 1
        with b.db() as c:
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                      (self.next_id, 'demo', 'Inspect image', state, thread))
            c.execute('INSERT INTO job_photos VALUES (?,?,?,?,?,?,?)',
                      (self.next_id, '', metadata['path'], metadata['sha256'], metadata['size'],
                       metadata['mime'], stamp))
            if tracked:
                b.track_image(c, metadata['path'], 'incoming', stamp)
        return metadata

    def media(self, state='sent', thread='original', data=PNG, age=50, turn=None):
        self.next_id += 1
        self.next_receipt += 1
        source = b.ROOT / ('project-source-' + str(self.next_id) + '.png')
        source.write_bytes(data)
        metadata = b.images.snapshot_image(b.ROOT, source)
        key = f'notice:{thread}:{turn or self.next_id}:media:{metadata["sha256"]}:photo'
        with b.db() as c:
            b.queue_media(c, key, metadata, 'photo', 'Image preview')
            c.execute('UPDATE outbox SET state=?,receipt=? WHERE id=?',
                      (state, self.next_receipt, key))
            token = c.execute('SELECT token FROM media_tokens WHERE media_id=?', (key,)).fetchone()[0]
        if age is not None:
            self.age(metadata['path'], age)
        return dict(metadata, id=key, token=token, receipt=self.next_receipt, source=str(source))

    @staticmethod
    def callback(media, user=123, chat=123, receipt=None, bot=456):
        return {'id': 'original-click', 'from': {'id': user, 'is_bot': False},
                'data': 'original:' + media['token'],
                'message': {'message_id': media['receipt'] if receipt is None else receipt,
                            'chat': {'id': chat, 'type': 'private'},
                            'from': {'id': bot, 'is_bot': True}}}

    def cleanup(self):
        return b.cleanup_images(self.cfg, now=self.now)

    def test_disabled_gate_never_inspects_or_deletes(self):
        metadata = self.incoming()
        self.cfg['image_cleanup_enabled'] = False
        with patch.object(b, 'running_threads', side_effect=AssertionError('Gate bypassed')):
            self.assertIsNone(self.cleanup())
        self.assertTrue(Path(metadata['path']).exists())
        with b.db() as c:
            self.assertEqual(b.get(c, 'cleanup:last'), '')

    def test_legacy_and_orphan_cache_backfill_expires_with_tombstones_only(self):
        incoming = b.received_images.save_photo(b.ROOT, PNG)
        source = b.ROOT / 'project-original.png'
        source.write_bytes(PNG + b'original')
        media = b.images.snapshot_image(b.ROOT, source)
        self.age(incoming['path'])
        self.age(media['path'])
        self.age(source)
        temporary = b.ROOT / 'media' / ('a' * 64 + '.tmp')
        temporary.write_bytes(b'partial')
        result = self.cleanup()
        self.assertEqual(set(result['deleted']), {incoming['path'], media['path']})
        self.assertEqual(result['bytes'], incoming['size'] + media['size'])
        rows = self.rows('image_retention')
        self.assertEqual({row['path'] for row in rows}, set(result['deleted']))
        self.assertTrue(all(row['expired_at'] == self.now for row in rows))
        self.assertEqual(source.read_bytes(), PNG + b'original')
        self.assertEqual(temporary.read_bytes(), b'partial')
        with b.db() as c:
            self.assertEqual(b.get(c, 'cleanup:deleted'), '2')
            self.assertEqual(b.get(c, 'cleanup:bytes'), str(result['bytes']))
            self.assertEqual(b.get(c, 'cleanup:errors'), '0')
        self.assertEqual(self.cleanup()['deleted'], [])

    def test_queued_and_running_incoming_jobs_protect_old_copies(self):
        queued = self.incoming(state='queued', thread='queued', data=PNG + b'queued')
        running = self.incoming(state='running', thread='running', data=PNG + b'running')
        ended = self.incoming(state='turn-ended', thread='ended', data=PNG + b'ended')
        result = self.cleanup()
        self.assertEqual(result['deleted'], [ended['path']])
        self.assertTrue(Path(queued['path']).exists())
        self.assertTrue(Path(running['path']).exists())
        self.assertEqual({row['path'] for row in result['skipped']
                          if row['reason'] == 'protected'}, {queued['path'], running['path']})

    def test_pending_sending_uncertain_and_shared_media_paths_are_protected(self):
        protected = [self.media(state=state, data=PNG + state.encode())
                     for state in ('pending', 'sending', 'uncertain')]
        # An ended/failed reference shares one content-addressed pending copy.
        shared = self.media(state='failed', data=PNG + b'pending')
        disposable = self.media(state='failed', data=PNG + b'disposable')
        result = self.cleanup()
        self.assertEqual(result['deleted'], [disposable['path']])
        self.assertEqual(shared['path'], protected[0]['path'])
        self.assertTrue(all(Path(media['path']).exists() for media in protected))
        self.assertTrue(Path(shared['path']).exists())

    def test_active_original_session_protects_ended_job_and_sent_media(self):
        incoming = self.incoming(thread='active', data=PNG + b'active-incoming')
        media = self.media(thread='active', data=PNG + b'active-media')
        disposable_incoming = self.incoming(thread='ended', data=PNG + b'ended-incoming')
        disposable_media = self.media(thread='ended', data=PNG + b'ended-media')
        with patch.object(b, 'running_threads', return_value=([{'id': 'active'}], 0)):
            result = self.cleanup()
        self.assertEqual(set(result['deleted']),
                         {disposable_incoming['path'], disposable_media['path']})
        self.assertTrue(Path(incoming['path']).exists())
        self.assertTrue(Path(media['path']).exists())

    def test_text_continuation_protects_historical_thread_images_until_released(self):
        incoming = self.incoming(thread='continued', data=PNG + b'continued-incoming')
        media = self.media(thread='continued', data=PNG + b'continued-media')
        unrelated_incoming = self.incoming(thread='unrelated', data=PNG + b'unrelated-incoming')
        unrelated_media = self.media(thread='unrelated', data=PNG + b'unrelated-media')
        text_job = 900
        with b.db() as c:
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                      (text_job, 'demo', 'Review the previous images again', 'queued', 'continued'))
            self.assertIsNone(c.execute('SELECT 1 FROM job_photos WHERE job_id=?',
                                        (text_job,)).fetchone())
        # Codex has not started yet; only the queued text continuation protects
        # its thread's historical incoming and outgoing copies.
        result = self.cleanup()
        self.assertEqual(set(result['deleted']), {unrelated_incoming['path'], unrelated_media['path']})
        self.assertTrue(Path(incoming['path']).exists())
        self.assertTrue(Path(media['path']).exists())
        with b.db() as c:
            c.execute("UPDATE jobs SET state='running' WHERE id=?", (text_job,))
        self.assertEqual(self.cleanup()['deleted'], [])
        self.assertTrue(Path(incoming['path']).exists())
        self.assertTrue(Path(media['path']).exists())
        with b.db() as c:
            c.execute("UPDATE jobs SET state='turn-ended' WHERE id=?", (text_job,))
        self.assertEqual(set(self.cleanup()['deleted']), {incoming['path'], media['path']})

    def test_unknown_original_activity_fails_closed_before_deletion(self):
        metadata = self.incoming()
        with patch.object(b, 'running_threads', return_value=([], 1)):
            with self.assertRaises(OSError):
                self.cleanup()
        self.assertTrue(Path(metadata['path']).exists())
        self.assertIsNone(self.rows('image_retention')[0]['expired_at'])
        with b.db() as c:
            self.assertEqual(b.get(c, 'cleanup:last'), '')

    def test_activity_inspection_error_fails_closed_before_deletion(self):
        metadata = self.media()
        with patch.object(b, 'running_threads', side_effect=OSError('Inspection unavailable')):
            with self.assertRaises(OSError):
                self.cleanup()
        self.assertTrue(Path(metadata['path']).exists())
        self.assertIsNone(self.rows('image_retention')[0]['expired_at'])

    def test_original_expiration_replies_clearly_without_queueing_or_upload(self):
        media = self.media(age=31)
        with b.db() as c:
            answer = b.select_callback(c, self.cfg, self.callback(media))
        self.assertIn('已过期或已清理', answer)
        self.assertIn('30 天', answer)
        self.assertEqual(len(self.rows('outbox_media')), 1)
        self.assertTrue(Path(media['path']).exists())
        self.cleanup()
        self.assertFalse(Path(media['path']).exists())
        with b.db() as c:
            self.assertTrue(b.original_expired(c, media))
            answer = b.select_callback(c, self.cfg, self.callback(media))
        self.assertIn('已过期或已清理', answer)
        self.assertEqual(len(self.rows('outbox_media')), 1)

    def test_original_expiration_still_enforces_owner_and_receipt_authentication(self):
        media = self.media(age=50)
        with b.db() as c:
            for options in ({'user': 999}, {'chat': 999}, {'bot': 999}):
                with self.subTest(options=options):
                    self.assertEqual(b.select_callback(c, self.cfg, self.callback(media, **options)),
                                     '无权操作。')
            answer = b.select_callback(c, self.cfg, self.callback(media, receipt=999))
        self.assertIn('无法验证', answer)
        self.assertNotIn('过期', answer)
        self.assertEqual(len(self.rows('outbox_media')), 1)

    def test_missing_original_copy_has_explicit_expiration_cue(self):
        media = self.media(age=None)
        Path(media['path']).unlink()
        with b.db() as c:
            answer = b.select_callback(c, self.cfg, self.callback(media))
        self.assertIn('已过期或已清理', answer)
        self.assertEqual(len(self.rows('outbox_media')), 1)

    def test_in_flight_original_delivery_takes_precedence_over_expiration(self):
        media = self.media(age=None)
        with b.db() as c:
            self.assertIn('已入队', b.select_callback(c, self.cfg, self.callback(media)))
            original = c.execute("SELECT id FROM outbox_media WHERE mode='document'").fetchone()[0]
        self.age(media['path'], 50)
        for state in ('pending', 'sending', 'uncertain'):
            with self.subTest(state=state), b.db() as c:
                c.execute('UPDATE outbox SET state=? WHERE id=?', (state, original))
                answer = b.select_callback(c, self.cfg, self.callback(media))
                self.assertNotIn('过期', answer)
                self.assertIn('待确认' if state == 'uncertain' else '排队或发送', answer)
        self.assertEqual(len(self.rows('outbox_media')), 2)
        self.assertEqual(self.cleanup()['deleted'], [])

    def test_new_delivery_renews_shared_cache_tombstone(self):
        old = self.media(age=50)
        self.cleanup()
        self.assertFalse(Path(old['path']).exists())
        self.assertIsNotNone(self.rows('image_retention')[0]['expired_at'])
        new = self.media(data=PNG, age=None)
        self.assertEqual(new['path'], old['path'])
        row = self.rows('image_retention')[0]
        self.assertIsNone(row['expired_at'])
        self.assertGreaterEqual(row['last_used'], self.now)
        with b.db() as c:
            self.assertFalse(b.original_expired(c, old))
            answer = b.select_callback(c, self.cfg, self.callback(old))
        self.assertIn('已入队', answer)

    def test_latest_shared_copy_timestamp_prevents_deletion_of_stale_record(self):
        media = self.media(age=50)
        # A fresh atomic file refresh can precede its database registration.
        replacement = b.ROOT / 'media' / '.fresh.tmp'
        replacement.write_bytes(PNG)
        os.utime(replacement, (self.now, self.now))
        os.replace(replacement, media['path'])
        result = self.cleanup()
        self.assertEqual(result['deleted'], [])
        self.assertTrue(Path(media['path']).exists())
        row = self.rows('image_retention')[0]
        self.assertEqual(row['last_used'], self.now)
        self.assertIsNone(row['expired_at'])

    def test_successful_aged_pending_upload_renews_retention_after_protection_ends(self):
        media = self.media(state='pending', age=50)
        self.assertEqual(self.cleanup()['deleted'], [])
        old_used = self.rows('image_retention')[0]['last_used']
        api = Mock()
        api.upload.return_value = {'message_id': 4242}
        with patch.object(b.time, 'time', return_value=self.now):
            self.assertTrue(b.deliver(api, self.cfg))
        api.upload.assert_called_once()
        self.assertEqual(api.upload.call_args.args[:3], ('sendPhoto', 'photo', PNG))
        outbox = self.rows('outbox')[0]
        self.assertEqual((outbox['state'], outbox['receipt']), ('sent', 4242))
        retention = self.rows('image_retention')[0]
        self.assertGreater(retention['last_used'], old_used)
        self.assertEqual(retention['last_used'], self.now)
        # It survives without pending protection for the full refreshed period.
        result = b.cleanup_images(self.cfg, now=self.now + 29 * b.retention.DAY)
        self.assertEqual(result['deleted'], [])
        self.assertTrue(Path(media['path']).exists())
        result = b.cleanup_images(self.cfg, now=self.now + 30 * b.retention.DAY)
        self.assertEqual(result['deleted'], [media['path']])
        self.assertEqual(Path(media['source']).read_bytes(), PNG)

    def test_retention_safety_rejection_cannot_undo_acknowledged_upload(self):
        media = self.media(state='pending', age=50)
        api = Mock()
        api.upload.return_value = {'message_id': 4343}
        with patch.object(b, 'track_image', side_effect=b.retention.RetentionError('invalid_path')):
            self.assertTrue(b.deliver(api, self.cfg))
        row = self.rows('outbox')[0]
        self.assertEqual((row['state'], row['receipt']), ('sent', 4343))
        self.assertEqual(self.rows('menu_versions'), [{'receipt': 4343, 'menu_id': media['id']}])
        self.assertFalse(b.deliver(api, self.cfg))
        api.upload.assert_called_once()
        self.assertEqual(api.upload.call_args.args[:3], ('sendPhoto', 'photo', PNG))
        self.assertTrue(Path(media['path']).exists())


if __name__ == '__main__':
    unittest.main()
