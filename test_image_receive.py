"""Offline photo input validation, private storage, and tamper regressions."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import image_receive as photos

PNG = b'\x89PNG\r\n\x1a\n' + b'example-image-bytes'
JPEG = b'\xff\xd8\xff' + b'example-image-bytes'


class PhotoReceiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    @staticmethod
    def message(**overrides):
        value = {'caption': ' 帮我看看这个报错 ', 'photo': [
            {'file_id': 'small', 'width': 100, 'height': 50, 'file_size': 1000},
            {'file_id': 'large', 'width': 1000, 'height': 500, 'file_size': 10000},
        ]}
        value.update(overrides)
        return value

    def test_resolution_variants_are_one_photo_and_largest_is_selected(self):
        self.assertEqual(photos.validate_photo(self.message()),
                         {'file_id': 'large', 'caption': '帮我看看这个报错'})

    def test_missing_sizes_use_area_and_ties_use_area(self):
        message = self.message()
        for photo in message['photo']:
            photo.pop('file_size')
        self.assertEqual(photos.validate_photo(message)['file_id'], 'large')
        for photo in message['photo']:
            photo['file_size'] = 10
        self.assertEqual(photos.validate_photo(message)['file_id'], 'large')

    def test_albums_and_documents_are_rejected(self):
        for message in (self.message(media_group_id='album'),
                        self.message(document={'file_id': 'file'})):
            with self.subTest(message=message):
                with self.assertRaises(photos.PhotoError):
                    photos.validate_photo(message)

    def test_caption_is_required(self):
        for caption in (None, '', ' \n\t', 12, '\x00hidden'):
            with self.subTest(caption=caption):
                with self.assertRaisesRegex(photos.PhotoError, '文字说明'):
                    photos.validate_photo(self.message(caption=caption))

    def test_malformed_message_and_variants_are_rejected(self):
        for message in (None, [], self.message(photo=None), self.message(photo=[]),
                        self.message(photo=[None, {}, {'file_id': 'x', 'width': True, 'height': 1}]),
                        self.message(photo=[{'file_id': 'x', 'width': 1, 'height': 1,
                                             'file_size': True}])):
            with self.subTest(message=message):
                with self.assertRaises(photos.PhotoError):
                    photos.validate_photo(message)

    def test_oversized_variants_are_skipped_and_all_oversized_are_rejected(self):
        message = self.message()
        message['photo'][1]['file_size'] = photos.PHOTO_LIMIT + 1
        self.assertEqual(photos.validate_photo(message)['file_id'], 'small')
        message['photo'][0]['file_size'] = photos.PHOTO_LIMIT + 1
        with self.assertRaisesRegex(photos.PhotoError, '20 MB'):
            photos.validate_photo(message)

    def test_png_and_jpeg_are_private_content_addressed_files(self):
        for data, mime, extension in ((PNG, 'image/png', '.png'), (JPEG, 'image/jpeg', '.jpg')):
            with self.subTest(mime=mime):
                metadata = photos.save_photo(self.root, data)
                digest = hashlib.sha256(data).hexdigest()
                self.assertEqual(metadata, {'path': str(self.root / 'incoming' / (digest + extension)),
                                            'sha256': digest, 'size': len(data), 'mime': mime})
                path = Path(metadata['path'])
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(photos.read_photo(self.root, metadata), str(path))

    def test_save_repairs_directory_permissions_and_reuses_content_hash(self):
        directory = self.root / 'incoming'
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        first = photos.save_photo(self.root, PNG)
        second = photos.save_photo(str(self.root), bytearray(PNG))
        self.assertEqual(first, second)
        self.assertEqual(len(list(directory.iterdir())), 1)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_unknown_or_oversized_bytes_are_rejected_without_files(self):
        for data in (b'', b'GIF89aexample', b'RIFFxxxxWEBPexample', b'private text', 'text'):
            with self.subTest(data=data):
                with self.assertRaises(photos.PhotoError):
                    photos.save_photo(self.root, data)
        with patch.object(photos, 'PHOTO_LIMIT', 10):
            with self.assertRaisesRegex(photos.PhotoError, '20 MB'):
                photos.save_photo(self.root, PNG)
        self.assertFalse((self.root / 'incoming').exists())

    def test_missing_and_changed_files_are_rejected(self):
        metadata = photos.save_photo(self.root, PNG)
        path = Path(metadata['path'])
        path.write_bytes(PNG[:-1] + b'X')
        with self.assertRaises(photos.PhotoError):
            photos.read_photo(self.root, metadata)
        path.unlink()
        with self.assertRaises(photos.PhotoError):
            photos.read_photo(self.root, metadata)

    def test_symlink_file_and_outside_path_are_rejected(self):
        metadata = photos.save_photo(self.root, PNG)
        path = Path(metadata['path'])
        outside = self.root / 'outside.png'
        outside.write_bytes(PNG)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(photos.PhotoError):
            photos.read_photo(self.root, metadata)
        metadata['path'] = str(outside)
        with self.assertRaises(photos.PhotoError):
            photos.read_photo(self.root, metadata)

    def test_symlink_incoming_directory_is_rejected_for_save_and_read(self):
        metadata = photos.save_photo(self.root, PNG)
        incoming = self.root / 'incoming'
        moved = self.root / 'moved'
        incoming.rename(moved)
        incoming.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(photos.PhotoError):
            photos.read_photo(self.root, metadata)
        with self.assertRaises(photos.PhotoError):
            photos.save_photo(self.root, PNG)

    def test_invalid_metadata_and_nonregular_file_are_rejected(self):
        metadata = photos.save_photo(self.root, PNG)
        for changes in ({'sha256': 'wrong'}, {'size': True}, {'size': photos.PHOTO_LIMIT + 1},
                        {'mime': 'image/gif'}, {'path': 'relative.png'}, {'path': 'bad\x00path'},
                        {'mime': 'image/jpeg'}):
            with self.subTest(changes=changes):
                with self.assertRaises(photos.PhotoError):
                    photos.read_photo(self.root, dict(metadata, **changes))
        path = Path(metadata['path'])
        path.unlink()
        os.mkfifo(path, mode=0o600)
        with self.assertRaises(photos.PhotoError):
            photos.read_photo(self.root, metadata)

    def test_failed_atomic_replace_leaves_no_partial_file(self):
        with patch.object(photos.os, 'replace', side_effect=OSError('test failure')):
            with self.assertRaises(photos.PhotoError):
                photos.save_photo(self.root, PNG)
        self.assertEqual(list((self.root / 'incoming').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
