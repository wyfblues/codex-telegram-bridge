"""Local final-answer image discovery and immutable private upload snapshots."""
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import unquote

PHOTO_LIMIT = 10_000_000
FILE_LIMIT = 50_000_000
IMAGE_LIMIT = 10


class ImageError(Exception):
    """A local preparation failure, safe to report without paths or file contents."""


def image_references(text):
    # Examples in fenced or inline code are not delivered artifacts.
    lines, fence = [], None
    for line in text.splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            value = marker[1]
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines.append(line)
    text = '\n'.join(lines)
    text = re.sub(r'`[^`\n]*`', '', text)
    pattern = r'(?<!\\)!\[((?:\\.|[^\]\\])*)\]\(\s*(<[^>\r\n]+>|(?:\\.|[^()\r\n]|\([^()\r\n]*\))+)\s*\)'
    seen = set()
    for match in re.finditer(pattern, text):
        alt, target = match.groups()
        target = target.strip()
        if target.startswith('<') and target.endswith('>'):
            target = target[1:-1]
        else:
            target = re.sub(r'\s+"[^"\n]*"\s*$', '', target)
        target = unquote(re.sub(r'\\([\\ ()\[\]])', r'\1', target))
        if not target.startswith('/') or '\x00' in target:
            continue
        mode = 'document' if alt.strip().casefold().startswith(('原图', 'original')) else 'photo'
        identity = (target, mode)
        if identity not in seen:
            seen.add(identity)
            yield target, mode


def image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', '.png', True
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', '.jpg', True
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif', '.gif', False
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp', '.webp', False
    raise ImageError('图片格式无法识别，仅支持 PNG、JPEG、GIF 和 WebP。')


def snapshot_image(root, source):
    source = Path(source)
    if not source.is_absolute() or source.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
        raise ImageError('图片必须是本机的 PNG、JPEG、GIF 或 WebP 文件。')
    try:
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NONBLOCK), 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ImageError('图片不是普通文件。')
            if os.fstat(stream.fileno()).st_size > FILE_LIMIT:
                raise ImageError('图片超过 50 MB，无法发送。')
            data = stream.read(FILE_LIMIT + 1)
        if len(data) > FILE_LIMIT:
            raise ImageError('图片超过 50 MB，无法发送。')
        mime, extension, preview = image_type(data)
        digest = hashlib.sha256(data).hexdigest()
        directory = root.resolve() / 'media'
        directory.mkdir(mode=0o700, exist_ok=True)
        destination = directory / (digest + extension)
        # Atomic replacement also repairs any damaged copy of an identical artifact.
        fd, temporary = tempfile.mkstemp(dir=directory, suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        raise ImageError('图片不存在、无法读取或无法保存发送副本。') from None
    name = re.sub(r'[\x00-\x1f\x7f"\\]', '_', source.name)[:180]
    return {'path': str(destination), 'name': name, 'mime': mime, 'sha256': digest,
            'size': len(data), 'preview': preview and len(data) <= PHOTO_LIMIT}


def upload_bytes(root, media):
    """Revalidate our snapshot before any network request, including later originals."""
    try:
        original = Path(media['path'])
        path = original.resolve(strict=True)
        if path.parent != (root / 'media').resolve() or original.is_symlink():
            raise ImageError('图片发送副本无效。')
        with path.open('rb') as stream:
            data = stream.read(FILE_LIMIT + 1)
        if len(data) != media['size'] or hashlib.sha256(data).hexdigest() != media['sha256']:
            raise ImageError('图片发送副本缺失或已变化。')
        return data
    except OSError:
        raise ImageError('图片发送副本无法读取。') from None
