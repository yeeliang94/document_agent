"""Skills: packs of know-how a user uploads to a playbook, as a ZIP holding SKILL.md, optional notes and optional
Python programs.

A skill is text, and the only programs it may hold are Python files. A program is never run on this computer: the
agent runs it inside its sandbox, where it can reach that run's files and nothing else. The ZIP is checked before anything is stored: its size, what it holds, where its paths point,
and that SKILL.md opens with a name and a one-line description.

Each upload is stored once, in a folder named by the fingerprint of its contents, and is never changed after that.
The skill's row names the fingerprint in use, so the row and the files cannot disagree: replacing a skill adds a
folder and moves the row to it. A job takes its own copy when it is created, so a later change to a skill never
reaches a run part-way.
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import threading
import unicodedata
import uuid
import zipfile
import zlib
from pathlib import Path

from .cleaning import now
from .files import is_junk, relative_name

MAX_ZIP_BYTES = 2 * 1024**2  # also the cap on the unpacked size; skills are prose, not data
MAX_FILES = 50
MAX_SKILLS = 30  # per playbook; each one adds a line to every run's brief
MAX_DESCRIPTION = 1024
MAX_PATH = 200  # characters in one file path inside a skill
TEXT_TYPES = frozenset({'.md', '.txt', '.csv', '.json', '.yaml', '.yml'})
PROGRAM_TYPES = frozenset({'.py'})  # run by the agent in its sandbox only; shown to the user before use
NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
MAIN = 'SKILL.md'


class SkillRefused(ValueError):
    """The upload cannot be used; the message says why in words for the screen."""


def front_matter(text: str) -> dict:
    """The fields between the two --- lines that open SKILL.md. Values are single lines; an indented line
    continues the one above."""
    lines = text.lstrip('﻿').splitlines()
    if not lines or lines[0].strip() != '---':
        raise SkillRefused(f'{MAIN} must open with a name and a description between two --- lines')
    fields, key = {}, None
    for line in lines[1:]:
        if line.strip() == '---':
            return fields
        if line[:1] in (' ', '\t') and key:
            fields[key] = f'{fields[key]} {line.strip()}'.strip()
            continue
        name, colon, value = line.partition(':')
        if colon:
            key = name.strip().lower()
            value = value.strip()
            fields[key] = '' if value in ('>', '|', '>-', '|-') else value.strip('"\'')
    raise SkillRefused(f'{MAIN} must open with a name and a description between two --- lines')


def _plain(text: str) -> str:
    """One line of ordinary text: no control characters, single spaces."""
    return ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in text).split())


def fingerprint(files: dict[str, bytes]) -> str:
    """One value for the contents of a skill ({path: bytes}): the same files give the same value however they
    were packed."""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode() + b'\0' + hashlib.sha256(files[path]).digest())
    return digest.hexdigest()


def read_skill_zip(data: bytes) -> dict:
    """Check an uploaded ZIP and return the skill: name, description, files ({path: bytes}) and a fingerprint of
    the contents. Raises SkillRefused with the reason when any check fails; nothing is written here."""
    megabytes = MAX_ZIP_BYTES // 1024**2
    if len(data) > MAX_ZIP_BYTES:
        raise SkillRefused(f'The ZIP is larger than {megabytes} MB')
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillRefused('This is not a ZIP file') from None
    with archive:
        entries = [i for i in archive.infolist() if not i.is_dir() and not is_junk(i.filename)]
        if len(entries) > MAX_FILES:
            raise SkillRefused(f'The ZIP holds more than {MAX_FILES} files')
        # The sizes a ZIP declares are checked before anything is unpacked, and again as each file is read.
        if sum(i.file_size for i in entries) > MAX_ZIP_BYTES:
            raise SkillRefused(f'The ZIP unpacks to more than {megabytes} MB')
        files, total = {}, 0
        for info in entries:
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise SkillRefused('The ZIP contains a link. Text and Python files only.')
            if info.flag_bits & 0x1:
                raise SkillRefused('The ZIP is password-protected')
            try:
                path = relative_name(unicodedata.normalize('NFC', info.filename)).as_posix()
            except ValueError:
                raise SkillRefused(f'The ZIP contains an unsafe file path: {_plain(info.filename)[:80]}') from None
            if Path(path).suffix.lower() not in TEXT_TYPES | PROGRAM_TYPES:
                raise SkillRefused(
                    f'Contains a file that is neither text nor a Python program ({Path(path).name}). '
                    'Text and Python only.'
                )
            try:
                with archive.open(info) as member:
                    content = member.read(MAX_ZIP_BYTES - total + 1)
            except (zipfile.BadZipFile, zlib.error, NotImplementedError, OSError, EOFError):
                raise SkillRefused('The ZIP is damaged or packed in a way that cannot be read') from None
            total += len(content)
            if total > MAX_ZIP_BYTES:
                raise SkillRefused(f'The ZIP unpacks to more than {megabytes} MB')
            try:
                if b'\x00' in content:
                    raise UnicodeDecodeError('utf-8', content, 0, 1, 'binary data')
                content.decode('utf-8')
            except UnicodeDecodeError:
                raise SkillRefused(f'{Path(path).name} is not a text file. Text and Python only.') from None
            if path in files:
                raise SkillRefused(f'The ZIP contains {Path(path).name} twice')
            files[path] = content
    # A ZIP made from a folder holds everything under that one folder; the skill is what is inside it.
    tops = {p.split('/', 1)[0] for p in files}
    if len(tops) == 1 and all('/' in p for p in files):
        files = {p.split('/', 1)[1]: content for p, content in files.items()}
    # Paths are compared as a disk that ignores case would see them, after the folder above is gone: no two the
    # same, none that is also a folder of another, none too long to write.
    seen = set()
    for path in files:
        if len(path) > MAX_PATH:
            raise SkillRefused(f'A file path in the ZIP is longer than {MAX_PATH} characters')
        key = path.casefold()
        if key in seen:
            raise SkillRefused(f'The ZIP contains {Path(path).name} twice')
        seen.add(key)
    for key in seen:
        parts = key.split('/')
        if any('/'.join(parts[:n]) in seen for n in range(1, len(parts))):
            raise SkillRefused(f'The ZIP uses {parts[0]} as both a file and a folder')
    main = next((p for p in files if p.casefold() == MAIN.casefold()), None)
    if main is None:
        raise SkillRefused(f'No {MAIN} inside')
    files[MAIN] = files.pop(main)
    fields = front_matter(files[MAIN].decode('utf-8'))
    name, description = fields.get('name', ''), _plain(fields.get('description', ''))
    if not NAME.fullmatch(name):
        raise SkillRefused(f'{MAIN} needs a name in lower-case letters, digits and hyphens, such as payroll-proration')
    if not description:
        raise SkillRefused(f'{MAIN} needs a one-line description')
    if len(description) > MAX_DESCRIPTION:
        raise SkillRefused(f'The description in {MAIN} is longer than {MAX_DESCRIPTION} characters')
    return {'name': name, 'description': description, 'files': files, 'sha256': fingerprint(files),
            'programs': programs(files)}  # fmt: skip


def programs(paths) -> list[str]:
    """The programs among a skill's file paths, in order."""
    return sorted(p for p in paths if Path(p).suffix.lower() in PROGRAM_TYPES)


FIELDS = ('name', 'description', 'enabled', 'sha256', 'files', 'bytes', 'added_at', 'programs')


class Skills:
    """The skills of each playbook: rows in SQLite, files under <data>/playbooks/<playbook>/skills/<name>/<fingerprint>/."""

    def __init__(self, playbooks, root: Path):
        self.playbooks, self.connect, self.root = playbooks, playbooks.connect, root / 'playbooks'
        # One writer or copier at a time, so a folder is never removed while a job is copying it.
        self.lock = threading.Lock()
        with self.connect() as c:
            c.execute(
                'CREATE TABLE IF NOT EXISTS playbook_skills(playbook_id TEXT NOT NULL, name TEXT NOT NULL, '
                'description TEXT NOT NULL, enabled INTEGER NOT NULL, sha256 TEXT NOT NULL, files INTEGER NOT NULL, '
                'bytes INTEGER NOT NULL, added_at TEXT NOT NULL, PRIMARY KEY(playbook_id, name))'
            )
            # How many programs the skill holds; added after the table first shipped.
            if 'programs' not in {r[1] for r in c.execute('PRAGMA table_info(playbook_skills)')}:
                c.execute('ALTER TABLE playbook_skills ADD COLUMN programs INTEGER NOT NULL DEFAULT 0')

    def folder(self, playbook_id: str, name: str = '', sha256: str = '') -> Path:
        """Where a playbook's skills, one skill's uploads, or one upload's files live. The playbook id comes from
        the database; a skill name from a web address is checked here before it becomes part of a path."""
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', playbook_id) or (name and not NAME.fullmatch(name)):
            raise KeyError((playbook_id, name))
        return self.root / playbook_id / 'skills' / name / sha256

    def _rows(self, c, playbook_id: str, name: str | None = None) -> list[dict]:
        self.playbooks._latest(c, playbook_id)  # an unknown playbook is an error, not an empty list
        rows = c.execute(
            f'SELECT {",".join(FIELDS)} FROM playbook_skills WHERE playbook_id=? AND name=COALESCE(?,name) ORDER BY name',
            (playbook_id, name),
        ).fetchall()
        return [{**dict(zip(FIELDS, r, strict=True)), 'enabled': bool(r[2])} for r in rows]

    def _row(self, c, playbook_id: str, name: str) -> dict:
        found = self._rows(c, playbook_id, name)
        if not found:
            raise KeyError((playbook_id, name))
        return found[0]

    def list(self, playbook_id: str) -> list[dict]:
        with self.connect() as c:
            return self._rows(c, playbook_id)

    def get(self, playbook_id: str, name: str) -> dict:
        """One skill with the text of SKILL.md and the paths of its files."""
        self.folder(playbook_id, name)
        with self.connect() as c:
            skill = self._row(c, playbook_id, name)
        folder = self.folder(playbook_id, name, skill['sha256'])
        if not (folder / MAIN).is_file():  # the files were removed by hand; the skill is as good as gone
            raise KeyError((playbook_id, name))
        paths = sorted(p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file())
        # Every program in full: the user reads what will run before switching the skill on.
        code = [{'path': p, 'text': (folder / p).read_text(encoding='utf-8')} for p in programs(paths)]
        return {**skill, 'text': (folder / MAIN).read_text(encoding='utf-8'), 'paths': paths, 'code': code}

    def add(self, playbook_id: str, data: bytes, filename: str = '') -> dict:
        """Check and store an uploaded ZIP. A skill with the same name is replaced and keeps its on/off setting.
        A refused upload is logged with its reason and stores nothing.
        Programs are the exception to the setting: a skill that arrives with programs, or whose programs differ
        from the upload it replaces, is switched off, so no run uses code the user has not had the chance to read.
        The result says so with "review": the screen then shows the programs and the user switches the skill on."""
        self.playbooks.get(playbook_id)
        filename = _plain(filename)[:200]
        try:
            skill = read_skill_zip(data)
            size = sum(len(content) for content in skill['files'].values())
            with self.lock:
                home = self.folder(playbook_id, skill['name'])
                try:
                    self._write(home / skill['sha256'], skill['files'])
                except OSError:
                    raise SkillRefused('The files in the ZIP could not be stored') from None
                with self.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    held = {s['name']: s for s in self._rows(c, playbook_id)}
                    before = held.get(skill['name'])
                    if not before and len(held) >= MAX_SKILLS:
                        raise SkillRefused(f'This playbook already has {MAX_SKILLS} skills')
                    code = {p: skill['files'][p] for p in skill['programs']}
                    review = bool(code) and code != self._code(playbook_id, before)
                    c.execute(
                        'INSERT INTO playbook_skills VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(playbook_id, name) DO '
                        'UPDATE SET description=excluded.description, sha256=excluded.sha256, files=excluded.files, '
                        'bytes=excluded.bytes, added_at=excluded.added_at, programs=excluded.programs'
                        + (', enabled=0' if review else ''),
                        (
                            playbook_id,
                            skill['name'],
                            skill['description'],
                            0 if review else 1,
                            skill['sha256'],
                            len(skill['files']),
                            size,
                            now(),
                            len(skill['programs']),
                        ),
                    )
                    self.playbooks._log(
                        c, playbook_id, 'skill.replaced' if before else 'skill.added',
                        f'{"Replaced" if before else "Added"} skill {skill["name"]}', skill=skill['name'],
                        sha256=skill['sha256'], previous_sha256=before['sha256'] if before else None,
                        files=len(skill['files']), bytes=size, file=filename, programs=skill['programs'],
                        switched_off_for_review=review,
                    )  # fmt: skip
                self._tidy(home, skill['sha256'])
        except SkillRefused as refusal:
            self.playbooks.event(
                playbook_id, 'skill.refused', f'Refused {filename or "an upload"}: {refusal}', 'WARN', file=filename,
                reason=str(refusal), bytes=len(data),
            )  # fmt: skip
            raise
        return {**self.get(playbook_id, skill['name']), 'review': review}

    def _code(self, playbook_id: str, row: dict | None) -> dict[str, bytes]:
        """The programs of the upload a skill's row names, {path: bytes}; empty when there is no such skill."""
        if not row:
            return {}
        folder = self.folder(playbook_id, row['name'], row['sha256'])
        paths = (p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file())
        return {p: (folder / p).read_bytes() for p in programs(paths)}

    @staticmethod
    def _write(folder: Path, files: dict) -> None:
        """Store one upload under its fingerprint: built beside the target, then moved in whole. The same contents
        uploaded again are already there and are left as they are."""
        if folder.exists():
            return
        incoming = folder.with_name(f'.incoming-{uuid.uuid4().hex}')
        try:
            for path, content in files.items():
                target = incoming / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            incoming.rename(folder)
        finally:
            shutil.rmtree(incoming, ignore_errors=True)

    @staticmethod
    def _tidy(home: Path, keep: str = '') -> None:
        """Remove the uploads of a skill that its row no longer names (all of them when keep is empty)."""
        if home.is_dir():
            for old in home.iterdir():
                if old.name != keep:
                    shutil.rmtree(old, ignore_errors=True)
            if not keep:
                shutil.rmtree(home, ignore_errors=True)

    def enable(self, playbook_id: str, name: str, enabled: bool) -> dict:
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            before = self._row(c, playbook_id, name)
            if before['enabled'] != enabled:
                c.execute(
                    'UPDATE playbook_skills SET enabled=? WHERE playbook_id=? AND name=?',
                    (int(enabled), playbook_id, name),
                )
                word = 'on' if enabled else 'off'
                self.playbooks._log(c, playbook_id, f'skill.turned_{word}', f'Turned skill {name} {word}', skill=name)
        return {**before, 'enabled': enabled}

    def remove(self, playbook_id: str, name: str) -> None:
        with self.lock:
            with self.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                before = self._row(c, playbook_id, name)
                c.execute('DELETE FROM playbook_skills WHERE playbook_id=? AND name=?', (playbook_id, name))
                self.playbooks._log(
                    c, playbook_id, 'skill.removed', f'Removed skill {name}', skill=name, sha256=before['sha256']
                )
            self._tidy(self.folder(playbook_id, name))

    def copy(self, source_id: str, target_id: str) -> None:
        """Give a playbook that was copied from another the same skills, with their on/off settings."""
        with self.lock:
            for skill in self.list(source_id):
                name, sha256 = skill['name'], skill['sha256']
                shutil.copytree(self.folder(source_id, name, sha256), self.folder(target_id, name, sha256))
                with self.connect() as c:
                    c.execute(
                        'INSERT INTO playbook_skills VALUES (?,?,?,?,?,?,?,?,?)',
                        (
                            target_id,
                            *(int(skill[f]) if f == 'enabled' else skill[f] for f in FIELDS[:-2]),
                            now(),
                            skill['programs'],
                        ),  # fmt: skip
                    )
                    self.playbooks._log(
                        c,
                        target_id,
                        'skill.added',
                        f'Copied skill {name}',
                        skill=name,
                        sha256=sha256,
                        copied_from=source_id,
                    )

    def discard(self, playbook_id: str) -> None:
        """Remove every skill of a playbook that is itself being removed."""
        with self.lock:
            with self.connect() as c:
                c.execute('DELETE FROM playbook_skills WHERE playbook_id=?', (playbook_id,))
            shutil.rmtree(self.root / playbook_id, ignore_errors=True)

    def copy_to_job(self, playbook_id: str, target: Path) -> list[dict]:
        """Copy the skills that are switched on into a job's folder; returns what the job should record. The copy
        is taken from the upload each row names, so the fingerprint recorded is the fingerprint of what was copied."""
        taken = []
        with self.lock:
            for skill in self.list(playbook_id):
                if skill['enabled']:
                    try:
                        shutil.copytree(
                            self.folder(playbook_id, skill['name'], skill['sha256']), target / skill['name']
                        )
                    except OSError:
                        raise ValueError(
                            f'The files of skill {skill["name"]} could not be read. Upload it again or switch it off.'
                        ) from None
                    held = (
                        p.relative_to(target / skill['name']).as_posix() for p in (target / skill['name']).rglob('*')
                    )
                    taken.append(
                        {**{k: skill[k] for k in ('name', 'description', 'sha256')}, 'programs': programs(held)}
                    )
        return taken
