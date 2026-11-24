import hashlib
import io
import re
import zipfile
from pathlib import Path, PurePosixPath

SUPPORTED = {'.xlsx', '.pdf', '.png', '.jpg', '.jpeg', '.webp', '.txt', '.csv', '.json', '.docx', '.eml'}
JUNK = {'.ds_store', 'thumbs.db', 'desktop.ini'}


def is_junk(name: str) -> bool:
    """Operating-system clutter that zips and folder pickers pick up."""
    parts = name.replace('\\', '/').split('/')
    return '__MACOSX' in parts or parts[-1].lower() in JUNK or parts[-1].startswith('._')


def relative_name(name: str) -> Path:
    name = name.replace('\\', '/')
    p = PurePosixPath(name)
    if not name or p.is_absolute() or any(part in ('..', '') for part in p.parts):
        raise ValueError('Invalid relative file path')
    for part in p.parts:
        if re.search(r'[\x00-\x1f<>:"|?*]', part) or part.endswith((' ', '.')):
            raise ValueError('File name is not Windows-compatible')
        if part.split('.')[0].upper() in {
            'CON',
            'PRN',
            'AUX',
            'NUL',
            *(f'COM{i}' for i in range(1, 10)),
            *(f'LPT{i}' for i in range(1, 10)),
        }:
            raise ValueError('Reserved Windows file name')
    return Path(*p.parts)


def within(root: Path, name: str) -> Path:
    p = root / relative_name(name)
    if not p.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path is outside job workspace')
    return p


def atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.pending')
    try:
        temp.write_bytes(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def add_file(root, role, name, data, manifest):
    path = within(root / role, name)
    # Retain duplicate content under different paths, but reject colliding filenames.
    if any(f['path'].casefold() == path.relative_to(root).as_posix().casefold() for f in manifest):
        raise ValueError('Duplicate or case-colliding relative path')
    atomic_bytes(path, data)
    manifest.append(
        {
            'path': path.relative_to(root).as_posix(),
            'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
            'role': role,
            'supported': path.suffix.lower() in SUPPORTED,
        }
    )


def add_zip(root, name, data, manifest, max_files, remaining_bytes, depth=0):
    if depth > 5:
        raise ValueError('Archive nesting exceeds five levels')
    initial_size = sum(item['size'] for item in manifest)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries = [i for i in z.infolist() if not i.is_dir() and not is_junk(i.filename)]
        if len(manifest) + len(entries) > max_files:
            raise ValueError('Too many files in archive')
        if sum(i.file_size for i in entries) > remaining_bytes:
            raise ValueError('Archive exceeds expanded upload limit')
        prefix = relative_name(name).with_suffix('')
        for info in entries:
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Archive links are unsupported')
            rel = relative_name(info.filename)
            payload = z.read(info)
            if rel.suffix.lower() == '.zip':
                remaining = remaining_bytes - (sum(item['size'] for item in manifest) - initial_size)
                add_zip(root, (prefix / rel).as_posix(), payload, manifest, max_files, remaining, depth + 1)
            else:
                add_file(root, 'inputs', (prefix / rel).as_posix(), payload, manifest)
            if len(manifest) > max_files or sum(item['size'] for item in manifest) - initial_size > remaining_bytes:
                raise ValueError('Nested archive exceeds expanded job limits')
