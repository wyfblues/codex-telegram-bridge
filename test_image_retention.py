"""Offline retention boundaries and nofollow ownership regressions."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import image_retention as retention


class ImageRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.now = 2_000_000_000.0

    def candidate(self, kind='incoming', age=8, data=b'image', extension='.png'):
        directory = self.root / kind
        directory.mkdir(exist_ok=True)
        path = directory / (hashlib.sha256(data).hexdigest() + extension)
        path.write_bytes(data)
        last_used = self.now - age * retention.DAY
        os.utime(path, (last_used, last_used))
        return {'path': str(path), 'kind': kind, 'last_used': last_used}

    def clean(self, candidates, protected=()):
        return retention.clean(self.root, candidates, protected, now=self.now)

    def test_fixed_durations_and_exact_seven_day_boundary(self):
        self.assertEqual(retention.DAY, 86400)
        old = self.candidate(age=7, data=b'old')
        young = self.candidate(age=7 - 1 / retention.DAY, data=b'young')
        result = self.clean([old, young])
        self.assertEqual(result['deleted'], [old['path']])
        self.assertEqual(result['expired'], [old['path']])
        self.assertEqual(result['bytes'], len(b'old'))
        self.assertTrue(Path(young['path']).exists())

    def test_media_retention_is_thirty_days(self):
        old = self.candidate(kind='media', age=30, data=b'old')
        young = self.candidate(kind='media', age=29, data=b'young')
        result = self.clean([old, young])
        self.assertEqual(result['deleted'], [old['path']])
        self.assertTrue(Path(young['path']).exists())

    def test_protected_active_or_queued_copies_survive(self):
        incoming = self.candidate(age=50, data=b'incoming')
        media = self.candidate(kind='media', age=50, data=b'media')
        result = self.clean([incoming, media], [incoming['path'], Path(media['path'])])
        self.assertEqual(result['deleted'], [])
        self.assertEqual(result['expired'], [])
        self.assertEqual({entry['reason'] for entry in result['skipped']}, {'protected'})
        self.assertTrue(Path(incoming['path']).exists())
        self.assertTrue(Path(media['path']).exists())

    def test_recent_last_use_or_file_refresh_survives(self):
        used = self.candidate(age=50, data=b'used')
        used['last_used'] = self.now
        refreshed = self.candidate(age=50, data=b'refreshed')
        os.utime(refreshed['path'], (self.now, self.now))
        result = self.clean([used, refreshed])
        self.assertEqual(result['deleted'], [])
        self.assertEqual([entry['reason'] for entry in result['skipped']],
                         ['not_expired', 'not_expired'])

    def test_project_original_and_unregistered_file_are_untouched(self):
        original = self.root / 'project' / ('a' * 64 + '.png')
        original.parent.mkdir()
        original.write_bytes(b'original')
        cache = self.candidate(age=50, data=b'unregistered')
        candidate = {'path': str(original), 'kind': 'incoming', 'last_used': 0}
        result = self.clean([candidate])
        self.assertEqual(result['deleted'], [])
        self.assertEqual(original.read_bytes(), b'original')
        self.assertEqual(Path(cache['path']).read_bytes(), b'unregistered')

    def test_invalid_names_nested_and_relative_paths_are_rejected(self):
        directory = self.root / 'incoming'
        directory.mkdir()
        paths = [directory / 'ordinary.png', directory / ('A' * 64 + '.png'),
                 directory / ('a' * 64 + '.jpeg'), directory / ('a' * 64 + '.tmp'),
                 directory / 'nested' / ('a' * 64 + '.png')]
        for path in paths:
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'safe')
        candidates = [{'path': str(path), 'kind': 'incoming', 'last_used': 0}
                      for path in paths]
        candidates.append({'path': 'incoming/' + 'a' * 64 + '.png',
                           'kind': 'incoming', 'last_used': 0})
        result = self.clean(candidates)
        self.assertEqual(result['deleted'], [])
        self.assertEqual(len(result['skipped']), len(candidates))
        self.assertTrue(all(path.exists() for path in paths))

    def test_symlink_file_never_follows_or_deletes_target(self):
        candidate = self.candidate(age=50)
        path = Path(candidate['path'])
        original = self.root / 'original.png'
        original.write_bytes(b'original')
        path.unlink()
        path.symlink_to(original)
        result = self.clean([candidate])
        self.assertEqual(result['expired'], [])
        self.assertTrue(path.is_symlink())
        self.assertEqual(original.read_bytes(), b'original')
        with self.assertRaises(retention.RetentionError):
            retention.inspect(self.root, path, 'incoming')

    def test_symlink_cache_directory_is_rejected(self):
        candidate = self.candidate(age=50)
        incoming = self.root / 'incoming'
        moved = self.root / 'outside'
        incoming.rename(moved)
        incoming.symlink_to(moved, target_is_directory=True)
        result = self.clean([candidate])
        self.assertEqual(result['expired'], [])
        self.assertTrue(Path(candidate['path']).exists())
        self.assertEqual(retention.discover(self.root), [])
        with self.assertRaises(retention.RetentionError):
            retention.inspect(self.root, candidate['path'], 'incoming')

    def test_symlink_root_and_special_files_are_rejected(self):
        candidate = self.candidate(age=50)
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(retention.clean(alias, [candidate], now=self.now)['deleted'], [])
        with self.assertRaises(retention.RetentionError):
            retention.inspect(alias, candidate['path'], 'incoming')
        path = Path(candidate['path'])
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(self.clean([candidate])['expired'], [])
        with self.assertRaises(retention.RetentionError):
            retention.inspect(self.root, path, 'incoming')

    def test_atomic_replacement_between_open_and_unlink_is_preserved(self):
        candidate = self.candidate(age=50)
        path = Path(candidate['path'])
        replacement = path.parent / '.new.tmp'
        replacement.write_bytes(b'fresh')
        os.utime(replacement, (self.now, self.now))
        actual_fstat = retention.os.fstat
        replaced = []

        def replace_after_open(fd):
            info = actual_fstat(fd)
            if not replaced:
                os.replace(replacement, path)
                replaced.append(True)
            return info

        with patch.object(retention.os, 'fstat', side_effect=replace_after_open):
            result = self.clean([candidate])
        self.assertEqual(result['expired'], [])
        self.assertEqual(result['skipped'][0]['reason'], 'changed')
        self.assertEqual(path.read_bytes(), b'fresh')

    def test_mtime_change_between_open_and_unlink_is_preserved(self):
        candidate = self.candidate(age=50)
        actual_fstat = retention.os.fstat

        def refresh_after_open(fd):
            info = actual_fstat(fd)
            os.utime(candidate['path'], (self.now, self.now))
            return info

        with patch.object(retention.os, 'fstat', side_effect=refresh_after_open):
            result = self.clean([candidate])
        self.assertEqual(result['expired'], [])
        self.assertEqual(result['skipped'][0]['reason'], 'changed')
        self.assertTrue(Path(candidate['path']).exists())

    def test_missing_expired_entries_retain_tombstone_evidence(self):
        expired = self.candidate(age=50)
        young = self.candidate(age=1, data=b'young')
        Path(expired['path']).unlink()
        Path(young['path']).unlink()
        result = self.clean([expired, young])
        self.assertEqual(result['deleted'], [])
        self.assertEqual(result['expired'], [expired['path']])
        self.assertEqual(result['bytes'], 0)
        info = retention.inspect(self.root, expired['path'], 'incoming')
        self.assertFalse(info['exists'])
        (self.root / 'incoming').rmdir()
        self.assertEqual(self.clean([expired])['expired'], [expired['path']])

    def test_one_deletion_error_does_not_prevent_other_candidates(self):
        failed = self.candidate(age=50, data=b'failed')
        good = self.candidate(kind='media', age=50, data=b'good')
        actual_unlink = retention.os.unlink

        def unlink(name, **options):
            if name == Path(failed['path']).name:
                raise PermissionError('test failure')
            return actual_unlink(name, **options)

        with patch.object(retention.os, 'unlink', side_effect=unlink):
            result = self.clean([failed, good])
        self.assertEqual(result['deleted'], [good['path']])
        self.assertEqual(result['expired'], [good['path']])
        self.assertEqual(result['errors'], [{'path': failed['path'], 'reason': 'filesystem_error'}])
        self.assertTrue(Path(failed['path']).exists())

    def test_repeat_clean_and_duplicate_candidate_are_safe(self):
        candidate = self.candidate(age=50)
        first = self.clean([candidate, candidate])
        second = self.clean([candidate])
        self.assertEqual(first['deleted'], [candidate['path']])
        self.assertEqual(first['skipped'][0]['reason'], 'duplicate')
        self.assertEqual(second['deleted'], [])
        self.assertEqual(second['expired'], [candidate['path']])

    def test_invalid_candidates_timestamps_and_kind_are_safe(self):
        candidate = self.candidate(age=50)
        variants = [None, {}, dict(candidate, kind='project'),
                    dict(candidate, kind=[]), dict(candidate, path=None)]
        variants.extend(dict(candidate, last_used=value)
                        for value in (None, '', True, -1, float('nan'), float('inf')))
        for value in variants:
            with self.subTest(value=value):
                result = self.clean([value])
                self.assertEqual(result['deleted'], [])
                self.assertTrue(Path(candidate['path']).exists())
        for now in (True, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                retention.clean(self.root, [], now=now)

    def test_inspect_metadata_and_discovery_only_include_owned_regular_files(self):
        incoming = self.candidate(age=8, extension='.jpg')
        media = self.candidate(kind='media', age=31, data=b'original', extension='.webp')
        info = retention.inspect(self.root, incoming['path'], 'incoming')
        self.assertTrue(info['exists'])
        self.assertEqual(info['size'], len(b'image'))
        self.assertEqual(info['mtime'], incoming['last_used'])
        directory = self.root / 'incoming'
        (directory / 'other.png').write_bytes(b'other')
        (directory / ('b' * 64 + '.tmp')).write_bytes(b'temp')
        (directory / ('c' * 64 + '.png')).mkdir()
        (directory / ('d' * 64 + '.png')).symlink_to(media['path'])
        nested = directory / 'nested'
        nested.mkdir()
        (nested / ('e' * 64 + '.png')).write_bytes(b'nested')
        self.assertEqual(retention.discover(self.root), [incoming, media])

    def test_discovery_tolerates_absent_directories_and_one_unreadable_file(self):
        self.assertEqual(retention.discover(self.root), [])
        first = self.candidate(age=8, data=b'first')
        second = self.candidate(age=8, data=b'second')
        actual_open = retention.os.open

        def open_file(path, *args, **options):
            if path == Path(first['path']).name:
                raise PermissionError('test failure')
            return actual_open(path, *args, **options)

        with patch.object(retention.os, 'open', side_effect=open_file):
            self.assertEqual(retention.discover(self.root), [second])


if __name__ == '__main__':
    unittest.main()
