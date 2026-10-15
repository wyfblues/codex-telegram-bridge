"""Offline image queue, upload, authentication, and replay regressions."""
import io
import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

import image_delivery as images
import test_bridge as support

b = support.b
PNG = b'\x89PNG\r\n\x1a\n' + b'example-image-bytes'


class ImageDeliveryTests(unittest.TestCase):
    rows = support.BridgeTests.rows
    update = support.BridgeTests.update
    tearDown = support.BridgeTests.tearDown

    def setUp(self):
        support.BridgeTests.setUp(self)
        self.cfg['image_delivery_enabled'] = True
        (b.ROOT / 'settings.json').write_text(json.dumps(self.cfg))
        self.image = b.ROOT / '页面 (验收).png'
        self.image.write_bytes(PNG)
        self.meta = support.BridgeTests.original_task(self)
        with b.db() as c:
            c.execute('DELETE FROM outbox')
        self.payload = {'type': 'agent-turn-complete', 'thread-id': self.meta['id'],
                        'turn-id': 'image-turn', 'cwd': str(b.ROOT),
                        'last-assistant-message': f'交付图片\n![截图](<{self.image}>)'}

    def notify(self):
        b.notify(self.payload, trusted=True)

    def media_rows(self):
        return self.rows('outbox_media')

    def mark_sent(self, key, receipt):
        with b.db() as c:
            c.execute("UPDATE outbox SET state='sent',receipt=? WHERE id=?", (receipt, key))

    def callback(self, token, receipt=202, user=123, chat=123, callback_id='image-click'):
        return {'id': callback_id, 'from': {'id': user, 'is_bot': False},
                'data': 'original:' + token,
                'message': {'message_id': receipt, 'chat': {'id': chat, 'type': 'private'},
                            'from': {'id': 456, 'is_bot': True}}}

    def test_only_explicit_final_image_references_are_selected(self):
        path = str(self.image)
        text = (f'普通链接 [文件](<{path}>)\n'
                f'![预览](<{path}>)\n![重复](<{path}>)\n'
                '![remote](https://example.com/a.png)\n![relative](a.png)\n'
                f'`![inline](<{path}>)`\n```md\n![example](<{path}>)\n```\n')
        self.assertEqual(list(images.image_references(text)), [(path, 'photo')])
        escaped = path.replace('(', '\\(').replace(')', '\\)').replace(' ', '%20')
        self.assertEqual(list(images.image_references(f'![原图]({escaped})')), [(path, 'document')])
        self.assertEqual(list(images.image_references(f'\\![escaped](<{path}>)')), [])
        self.assertEqual(list(images.image_references(f'```md\n![unclosed](<{path}>)')), [])

    def test_preview_uses_private_snapshot_and_original_button(self):
        self.notify()
        media = self.media_rows()[0]
        self.assertEqual(media['mode'], 'photo')
        self.assertEqual(Path(media['path']).read_bytes(), PNG)
        self.assertEqual(Path(media['path']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(media['path']).parent.stat().st_mode & 0o777, 0o700)
        self.assertIn('发送原图', self.rows('keyboards')[0]['markup'])
        self.assertEqual(self.rows('jobs'), [])

    def test_disabled_feature_still_sends_text_without_images(self):
        self.cfg['image_delivery_enabled'] = False
        (b.ROOT / 'settings.json').write_text(json.dumps(self.cfg))
        self.notify()
        self.assertEqual(self.media_rows(), [])
        self.assertEqual(len(self.rows('outbox')), 1)

    def test_duplicate_callback_and_observer_do_not_repeat_images(self):
        self.notify()
        self.notify()
        self.assertEqual(len(self.media_rows()), 1)
        self.assertEqual(len(self.rows('outbox')), 2)

    def test_existing_historical_notice_never_gets_new_image_backfill(self):
        with b.db() as c:
            b.queue(c, 'notice:original:image-turn', 'Previously delivered text')
        self.notify()
        self.assertEqual(self.media_rows(), [])

    def test_completed_before_image_enable_does_not_upload_history(self):
        with b.db() as c:
            b.put(c, 'image_delivery_since', time.time())
        self.payload['completed-at'] = '2026-08-02T12:10:17Z'
        self.notify()
        self.assertEqual(self.media_rows(), [])
        self.assertEqual(len(self.rows('outbox')), 1)

    def test_missing_or_non_image_file_does_not_break_final_answer(self):
        self.image.write_text('PRIVATE CREDENTIAL CONTENT')
        self.notify()
        self.assertEqual(self.media_rows(), [])
        notices = self.rows('outbox')
        self.assertEqual(len(notices), 2)
        self.assertIn('交付图片', notices[0]['text'])
        self.assertNotIn('PRIVATE CREDENTIAL', notices[1]['text'])

    def test_unsupported_and_oversized_files_are_rejected_before_upload(self):
        self.image.unlink()
        with self.assertRaises(images.ImageError): images.snapshot_image(b.ROOT, str(self.image))
        self.image.write_bytes(PNG)
        with patch.object(images, 'FILE_LIMIT', 5):
            with self.assertRaises(images.ImageError): images.snapshot_image(b.ROOT, str(self.image))

    def test_preview_limit_and_other_image_formats_use_original_document(self):
        with patch.object(images, 'PHOTO_LIMIT', 5): self.notify()
        self.assertEqual(self.media_rows()[0]['mode'], 'document')
        self.assertEqual(self.rows('media_tokens'), [])
        gif = b.ROOT / 'animated.gif'
        gif.write_bytes(b'GIF89a' + b'example')
        self.assertFalse(images.snapshot_image(b.ROOT, str(gif))['preview'])

    def test_original_alt_text_sends_document_directly(self):
        self.payload['last-assistant-message'] = f'![original](<{self.image}>)'
        self.notify()
        self.assertEqual(self.media_rows()[0]['mode'], 'document')
        self.mark_sent('notice:original:image-turn', 201)
        api = Mock()
        api.upload.return_value = {'message_id': 202}
        b.deliver(api, self.cfg)
        self.assertEqual(api.upload.call_args.args[:3], ('sendDocument', 'document', PNG))
        message = self.update(200, 'Continue')['message']
        message['reply_to_message'] = self.callback('', 202)['message']
        with b.db() as c:
            self.assertEqual(b.resolve_reply(c, self.cfg, message)['id'], self.meta['id'])

    def test_preview_upload_and_receipt_can_resume_original_conversation(self):
        self.notify()
        api = Mock()
        api.call.return_value = {'message_id': 201}
        api.upload.return_value = {'message_id': 202}
        b.deliver(api, self.cfg)
        b.deliver(api, self.cfg)
        b.deliver(api, self.cfg)
        self.assertEqual(api.upload.call_count, 1)
        self.assertEqual(api.upload.call_args.args[:3], ('sendPhoto', 'photo', PNG))
        self.assertIn('reply_markup', api.upload.call_args.kwargs)
        message = self.update(200, 'Continue')['message']
        message['reply_to_message'] = self.callback('', 202)['message']
        with b.db() as c:
            self.assertEqual(b.resolve_reply(c, self.cfg, message)['id'], self.meta['id'])

    def test_original_button_preserves_bytes_after_source_changes_and_deduplicates(self):
        self.notify()
        media = self.media_rows()[0]
        self.mark_sent(media['id'], 202)
        self.image.write_bytes(PNG + b'changed')
        token = self.rows('media_tokens')[0]['token']
        with b.db() as c:
            b.select_callback(c, self.cfg, self.callback(token))
            b.select_callback(c, self.cfg, self.callback(token, callback_id='second-click'))
        originals = [r for r in self.media_rows() if r['mode'] == 'document']
        self.assertEqual(len(originals), 1)
        self.assertEqual(images.upload_bytes(b.ROOT, originals[0]), PNG)

    def test_original_button_rejects_strangers_wrong_receipts_and_chats(self):
        self.notify()
        media = self.media_rows()[0]
        self.mark_sent(media['id'], 202)
        token = self.rows('media_tokens')[0]['token']
        with b.db() as c:
            for callback in (self.callback(token, user=999), self.callback(token, chat=999),
                             self.callback(token, receipt=999), self.callback('unknown')):
                b.select_callback(c, self.cfg, callback)
        self.assertEqual(len(self.media_rows()), 1)

    def test_original_button_reports_sent_failed_and_uncertain_without_resending(self):
        self.notify()
        self.mark_sent(self.media_rows()[0]['id'], 202)
        token = self.rows('media_tokens')[0]['token']
        with b.db() as c:
            b.select_callback(c, self.cfg, self.callback(token))
            original = c.execute("SELECT id FROM outbox_media WHERE mode='document'").fetchone()['id']
            for state, expected in [('sent', '已发送'), ('failed', '发送失败'), ('uncertain', '待确认')]:
                c.execute('UPDATE outbox SET state=? WHERE id=?', (state, original))
                self.assertIn(expected, b.select_callback(c, self.cfg, self.callback(token)))
        self.assertEqual(len(self.media_rows()), 2)

    def test_upload_timeout_never_retries_or_falls_back(self):
        self.notify()
        self.mark_sent('notice:original:image-turn', 201)
        api = Mock()
        api.upload.side_effect = TimeoutError()
        b.deliver(api, self.cfg)
        self.assertFalse(b.deliver(api, self.cfg))
        self.assertEqual(api.upload.call_count, 1)
        self.assertEqual(self.rows('outbox')[-1]['state'], 'uncertain')

    def test_definite_photo_rejection_falls_back_to_original(self):
        self.notify()
        self.mark_sent('notice:original:image-turn', 201)
        api = Mock()
        api.upload.side_effect = [b.Rejected(400), {'message_id': 202}]
        b.deliver(api, self.cfg)
        self.assertEqual([call.args[0] for call in api.upload.call_args_list], ['sendPhoto', 'sendDocument'])
        self.assertEqual(self.rows('outbox')[-1]['state'], 'sent')

    def test_rate_limit_preserves_pending_image_until_retry_time(self):
        self.notify()
        self.mark_sent('notice:original:image-turn', 201)
        api = Mock()
        api.upload.side_effect = b.Rejected(429, 60)
        b.deliver(api, self.cfg)
        self.assertFalse(b.deliver(api, self.cfg))
        self.assertEqual(api.upload.call_count, 1)
        self.assertEqual(self.rows('outbox')[-1]['state'], 'pending')

    def test_damaged_snapshot_fails_locally_without_network_and_reports_error(self):
        self.notify()
        self.mark_sent('notice:original:image-turn', 201)
        Path(self.media_rows()[0]['path']).write_text('changed')
        api = Mock()
        b.deliver(api, self.cfg)
        api.upload.assert_not_called()
        self.assertEqual(self.rows('outbox')[-2]['state'], 'failed')
        self.assertIn('图片发送失败', self.rows('outbox')[-1]['text'])

    def test_multipart_request_preserves_binary_and_serializes_keyboard(self):
        (b.ROOT / 'token').write_text('fake-test-token')
        api = b.API(self.cfg)
        api.http = Mock()
        api.http.open.return_value = io.BytesIO(b'{"ok":true,"result":{"message_id":202}}')
        result = api.upload('sendPhoto', 'photo', PNG, 'example.png', 'image/png',
                            chat_id=123, reply_markup={'inline_keyboard': []})
        self.assertEqual(result['message_id'], 202)
        request = api.http.open.call_args.args[0]
        self.assertIn('multipart/form-data; boundary=', request.headers['Content-type'])
        self.assertIn(b'filename="example.png"', request.data)
        self.assertIn(PNG, request.data)
        self.assertIn(b'{"inline_keyboard": []}', request.data)
        self.assertEqual(api.http.open.call_args.kwargs['timeout'], 120)

    def test_image_count_limit_is_reported_and_not_silently_dropped(self):
        self.payload['last-assistant-message'] = '\n'.join(
            f'![image](<{b.ROOT / (str(n) + ".png")}>)' for n in range(12))
        for n in range(12): (b.ROOT / (str(n) + '.png')).write_bytes(PNG + bytes([n]))
        self.notify()
        self.assertEqual(len(self.media_rows()), 10)
        self.assertIn('超过 10 张', self.rows('outbox')[-1]['text'])


if __name__ == '__main__':
    unittest.main()
