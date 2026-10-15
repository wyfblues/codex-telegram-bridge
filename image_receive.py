"""Validate Telegram photo input and retain bounded private image snapshots."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

PHOTO_LIMIT = 20_000_000


class PhotoError(Exception):
    """An input failure safe to report without paths or downloaded contents."""


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_photo(message):
    """Select one photo's largest usable resolution, retaining its caption."""
    if not isinstance(message, dict):
        raise PhotoError('图片消息格式无效，请重新发送单张图片并添加文字说明。')
    if message.get('media_group_id'):
        raise PhotoError('暂不支持相册或多张图片，请单独发送一张图片并添加文字说明。')
    if message.get('document') is not None:
        raise PhotoError('暂不支持图片文件附件，请使用 Telegram 的图片方式发送单张图片。')
    caption = message.get('caption')
    if not isinstance(caption, str) or not caption.strip() or '\x00' in caption:
        raise PhotoError('请在发送图片时添加文字说明，例如：帮我看看这个报错。')
    variants = message.get('photo')
    if not isinstance(variants, list) or not variants:
        raise PhotoError('暂时只支持单张图片，请使用图片方式发送并添加文字说明。')
    candidates, oversized = [], False
    for photo in variants:
        if not isinstance(photo, dict):
            continue
        file_id = photo.get('file_id')
        width, height, size = photo.get('width'), photo.get('height'), photo.get('file_size')
        if (not isinstance(file_id, str) or not file_id.strip() or '\x00' in file_id
                or len(file_id) > 1024 or not _positive_int(width) or not _positive_int(height)):
            continue
        if size is not None and not _positive_int(size):
            continue
        if size is not None and size > PHOTO_LIMIT:
            oversized = True
            continue
        # Telegram supplies resolution variants of the same photo, not separate images.
        candidates.append((size or 0, width * height, file_id))
    if not candidates:
        if oversized:
            raise PhotoError('图片超过 20 MB，请缩小后再发送。')
        raise PhotoError('无法识别这张图片，请重新发送单张图片并添加文字说明。')
    return {'file_id': max(candidates)[2], 'caption': caption.strip()}


def _image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', '.jpg'
    raise PhotoError('收到的文件不是 PNG 或 JPEG 图片，请重新以图片方式发送。')


def _incoming_directory(root, create):
    """Open the real incoming directory without following its entry as a symlink."""
    root_path = Path(root).resolve(strict=True)
    root_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if create:
            try:
                os.mkdir('incoming', mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        directory_fd = os.open('incoming', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        if create:
            os.fchmod(directory_fd, 0o700)
        return root_path / 'incoming', directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def save_photo(root, data):
    """Atomically retain a private, content-addressed PNG/JPEG snapshot."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise PhotoError('图片下载失败，请重新发送。')
    if (data.nbytes if isinstance(data, memoryview) else len(data)) > PHOTO_LIMIT:
        raise PhotoError('图片超过 20 MB，请缩小后再发送。')
    data = bytes(data)
    if len(data) > PHOTO_LIMIT:
        raise PhotoError('图片超过 20 MB，请缩小后再发送。')
    mime, extension = _image_type(data)
    digest = hashlib.sha256(data).hexdigest()
    filename = digest + extension
    directory_fd, temporary = None, None
    try:
        directory, directory_fd = _incoming_directory(root, create=True)
        temporary = '.' + secrets.token_hex(16) + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary = None
        return {'path': str(directory / filename), 'sha256': digest,
                'size': len(data), 'mime': mime}
    except OSError:
        raise PhotoError('图片无法保存，请稍后重试。') from None
    finally:
        if directory_fd is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)


def read_photo(root, metadata):
    """Return an absolute path only after revalidating the private snapshot."""
    invalid = '图片接收副本缺失或已变化，请重新发送。'
    if not isinstance(metadata, dict):
        raise PhotoError(invalid)
    path, digest, size, mime = (metadata.get(key) for key in ('path', 'sha256', 'size', 'mime'))
    if (not isinstance(path, str) or '\x00' in path or not isinstance(digest, str)
            or re.fullmatch(r'[0-9a-f]{64}', digest) is None or not _positive_int(size)
            or size > PHOTO_LIMIT or mime not in ('image/png', 'image/jpeg')):
        raise PhotoError(invalid)
    directory_fd = None
    try:
        directory, directory_fd = _incoming_directory(root, create=False)
        original = Path(path)
        extension = '.png' if mime == 'image/png' else '.jpg'
        if (not original.is_absolute() or original.parent != directory
                or original.name != digest + extension):
            raise PhotoError(invalid)
        fd = os.open(original.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                     dir_fd=directory_fd)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise PhotoError(invalid)
            data = stream.read(PHOTO_LIMIT + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise PhotoError(invalid)
        if _image_type(data)[0] != mime:
            raise PhotoError(invalid)
        return str(original)
    except (OSError, ValueError):
        raise PhotoError(invalid) from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
