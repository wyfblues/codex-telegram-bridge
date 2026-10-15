"""Expire registered Bot image copies without traversing project directories."""
import math
import os
from pathlib import Path
import re
import stat
import time

DAY = 24 * 60 * 60
RETENTION_DAYS = {'incoming': 7, 'media': 30}
_FILENAME = re.compile(r'[0-9a-f]{64}\.(?:png|jpg|gif|webp)\Z')


class RetentionError(Exception):
    """A path cannot safely be treated as an owned image copy."""


def _number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    except OverflowError:
        return False


def _root(root):
    original = Path(root)
    if original.is_symlink():
        raise RetentionError('unsafe_root')
    return original.resolve(strict=True)


def _owned_path(root, path, kind):
    if kind not in RETENTION_DAYS or not isinstance(path, (str, os.PathLike)):
        raise RetentionError('invalid_path')
    path = Path(path)
    if (not path.is_absolute() or path.parent != root / kind
            or _FILENAME.fullmatch(path.name) is None):
        raise RetentionError('invalid_path')
    return path


def _open_directory(root, kind):
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return os.open(kind, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                       dir_fd=root_fd)
    finally:
        os.close(root_fd)


def _missing(path, kind):
    return {'path': str(path), 'kind': kind, 'exists': False, 'size': 0,
            'mtime': None, 'device': None, 'inode': None}


def _metadata(path, kind, info):
    return {'path': str(path), 'kind': kind, 'exists': True, 'size': info.st_size,
            'mtime': info.st_mtime, 'device': info.st_dev, 'inode': info.st_ino}


def inspect(root, path, kind):
    """Inspect one exact cache entry; raise RetentionError for unsafe entries.

    An absent owned file or cache directory is reported with ``exists=False``.
    Symlinks, special files, and paths outside the two private directories are
    rejected. No file contents are read and no directories are created.
    """
    directory_fd = file_fd = None
    try:
        root = _root(root)
        path = _owned_path(root, path, kind)
        try:
            directory_fd = _open_directory(root, kind)
        except FileNotFoundError:
            return _missing(path, kind)
        try:
            file_fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
        except FileNotFoundError:
            return _missing(path, kind)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise RetentionError('not_regular_file')
        return _metadata(path, kind, info)
    except (OSError, ValueError, TypeError):
        raise RetentionError('unsafe_or_unreadable_path') from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def discover(root):
    """Find legacy/orphan owned copies only in the two direct cache directories.

    Return candidates with the current mtime as their initial last-use time.
    Unreadable or unsafe directories/files are ignored; discovery never creates
    directories, follows symlinks, descends into subdirectories, or reads data.
    Existing database records should preserve their more recent last-use time.
    """
    try:
        root = _root(root)
    except (RetentionError, OSError, ValueError, TypeError):
        return []
    found = []
    for kind in RETENTION_DAYS:
        directory_fd = None
        try:
            directory_fd = _open_directory(root, kind)
            for name in sorted(os.listdir(directory_fd)):
                if _FILENAME.fullmatch(name) is None:
                    continue
                file_fd = None
                try:
                    file_fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                                      dir_fd=directory_fd)
                    info = os.fstat(file_fd)
                    if stat.S_ISREG(info.st_mode):
                        found.append({'path': str(root / kind / name), 'kind': kind,
                                      'last_used': info.st_mtime})
                except OSError:
                    continue
                finally:
                    if file_fd is not None:
                        os.close(file_fd)
        except OSError:
            continue
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
    return found


def _protected_paths(paths):
    protected = set()
    for path in paths:
        try:
            if isinstance(path, (str, os.PathLike)):
                protected.add(os.path.abspath(path))
        except (OSError, ValueError, TypeError):
            continue
    return protected


def clean(root, candidates, protected_paths=(), now=None):
    """Delete only registered expired copies, using fixed 7/30 day durations.

    Candidates are dictionaries with ``path``, ``kind`` (incoming/media), and
    ``last_used`` Unix time. A copy expires when both its last use and current
    file mtime are at least the retention duration old. The caller serializes
    protection selection and deletion with its state database transaction.

    ``expired`` includes safely deleted entries and already missing expired
    entries, for the caller's original-download expiration tombstones. Errors
    and skipped entries contain only a path and a stable machine reason.
    """
    now = time.time() if now is None else now
    if not _number(now):
        raise ValueError('now must be a finite nonnegative Unix time')
    result = {'deleted': [], 'expired': [], 'bytes': 0, 'skipped': [], 'errors': []}
    protected = _protected_paths(protected_paths)
    seen = set()
    try:
        root = _root(root)
    except (RetentionError, OSError, ValueError, TypeError):
        result['errors'].append({'path': '', 'reason': 'unsafe_root'})
        return result

    for candidate in candidates:
        directory_fd = file_fd = None
        path_text = candidate.get('path', '') if isinstance(candidate, dict) else ''
        # Never let error reporting retain an arbitrary object from bad input.
        path_text = os.fspath(path_text) if isinstance(path_text, (str, os.PathLike)) else ''
        if not isinstance(path_text, str):
            path_text = ''
        try:
            if not isinstance(candidate, dict):
                raise RetentionError('invalid_candidate')
            kind, last_used = candidate.get('kind'), candidate.get('last_used')
            path = _owned_path(root, path_text, kind)
            path_text = str(path)
            if path_text in seen:
                result['skipped'].append({'path': path_text, 'reason': 'duplicate'})
                continue
            seen.add(path_text)
            if path_text in protected:
                result['skipped'].append({'path': path_text, 'reason': 'protected'})
                continue
            if not _number(last_used):
                raise RetentionError('invalid_last_used')
            duration = RETENTION_DAYS[kind] * DAY
            if now - last_used < duration:
                result['skipped'].append({'path': path_text, 'reason': 'not_expired'})
                continue
            try:
                directory_fd = _open_directory(root, kind)
                file_fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                                  dir_fd=directory_fd)
            except FileNotFoundError:
                result['expired'].append(path_text)
                continue
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise RetentionError('not_regular_file')
            if now - max(last_used, info.st_mtime) < duration:
                result['skipped'].append({'path': path_text, 'reason': 'not_expired'})
                continue
            # An atomic refresh after the open must not be removed by an old
            # candidate. Restat through the same anchored, nofollow directory.
            try:
                current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                result['expired'].append(path_text)
                continue
            identity = lambda value: (value.st_dev, value.st_ino, value.st_mtime_ns,
                                      value.st_size, value.st_mode)
            if identity(current) != identity(info):
                result['skipped'].append({'path': path_text, 'reason': 'changed'})
                continue
            try:
                os.unlink(path.name, dir_fd=directory_fd)
            except FileNotFoundError:
                result['expired'].append(path_text)
                continue
            result['deleted'].append(path_text)
            result['expired'].append(path_text)
            result['bytes'] += info.st_size
        except RetentionError as error:
            result['skipped'].append({'path': path_text, 'reason': str(error)})
        except (OSError, ValueError, TypeError):
            result['errors'].append({'path': path_text, 'reason': 'filesystem_error'})
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)
    return result
