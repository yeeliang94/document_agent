"""Named texts kept as versions, with an activity log: what playbooks (instructions) and clients (notes) share.

A version is never edited or deleted: every change, a restore included, adds a new one, so a run can always name the
exact text it used. Each record also has an activity log for what happens to it outside a run (saves, restores,
refusals), next to the per-job log in store.py.
"""

from __future__ import annotations

import json
import uuid

from .cleaning import clean, now

MAX_NAME = 80
MAX_TEXT = 30_000  # characters in one version
SOURCES = frozenset({'created', 'edited', 'remembered', 'learned', 'tuned', 'tidied', 'restored'})


class StaleVersion(Exception):
    """A save was based on a version that is no longer the latest; nothing was written."""

    def __init__(self, current: int, what: str = 'instructions'):
        super().__init__(f'The {what} changed since you opened them (now version {current}).')
        self.current = current


class Versioned:
    """kind names the tables (playbooks, playbook_versions, playbook_events) and the log's events; label is how a
    person calls one (a playbook); text is what its versions hold (instructions)."""

    kind, label, text = 'record', 'record', 'text'

    def __init__(self, store):
        self.connect = store.connect
        # Set by the app once it runs: called with (id, version, source) after each saved version.
        self.on_change = None
        k = self.kind
        with self.connect() as c:
            c.executescript(f"""
                CREATE TABLE IF NOT EXISTS {k}s(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS {k}_versions(
                    {k}_id TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
                    source TEXT NOT NULL, change TEXT NOT NULL, text TEXT NOT NULL, detail TEXT NOT NULL,
                    PRIMARY KEY({k}_id, version));
                CREATE TABLE IF NOT EXISTS {k}_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, {k}_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS {k}_events_{k} ON {k}_events({k}_id, seq);
            """)

    def _latest(self, c, record_id: str) -> dict:
        k = self.kind
        r = c.execute(
            f'SELECT p.name, p.created_at, v.version, v.created_at, v.text FROM {k}s p '
            f'JOIN {k}_versions v ON v.{k}_id=p.id WHERE p.id=? ORDER BY v.version DESC LIMIT 1',
            (record_id,),
        ).fetchone()
        if not r:
            raise KeyError(record_id)
        return {'id': record_id, 'name': r[0], 'created_at': r[1], 'version': r[2], 'updated_at': r[3], 'text': r[4]}

    def _name(self, c, name: str, record_id: str = '') -> str:
        name = ' '.join((name or '').split())
        if not name:
            raise ValueError('Enter a name')
        if len(name) > MAX_NAME:
            raise ValueError(f'Use a name of at most {MAX_NAME} characters')
        if c.execute(f'SELECT 1 FROM {self.kind}s WHERE lower(name)=lower(?) AND id<>?', (name, record_id)).fetchone():
            raise ValueError(f'A {self.label} with this name already exists')
        return name

    def list(self) -> list[dict]:
        k = self.kind
        with self.connect() as c:
            rows = c.execute(
                f'SELECT p.id, p.name, p.created_at, MAX(v.version), MAX(v.created_at) FROM {k}s p '
                f'JOIN {k}_versions v ON v.{k}_id=p.id GROUP BY p.id ORDER BY p.created_at, p.rowid'
            ).fetchall()
        return [dict(zip(('id', 'name', 'created_at', 'version', 'updated_at'), r, strict=True)) for r in rows]

    def get(self, record_id: str) -> dict:
        """The record with its latest text."""
        with self.connect() as c:
            return self._latest(c, record_id)

    def _insert(self, c, name: str, text: str = '', detail: dict | None = None) -> str:
        record_id = uuid.uuid4().hex
        c.execute(f'INSERT INTO {self.kind}s VALUES (?,?,?)', (record_id, name, now()))
        c.execute(
            f'INSERT INTO {self.kind}_versions VALUES (?,?,?,?,?,?,?)',
            (record_id, 1, now(), 'created', '', text, json.dumps(detail or {})),
        )
        return record_id

    def rename(self, record_id: str, name: str) -> dict:
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            before = self._latest(c, record_id)['name']
            name = self._name(c, name, record_id)
            c.execute(f'UPDATE {self.kind}s SET name=? WHERE id=?', (name, record_id))
        if name != before:
            self.event(record_id, f'{self.kind}.renamed', f'Renamed "{before}" to "{name}"')
        return self.get(record_id)

    def save(
        self,
        record_id: str,
        text: str,
        source: str = 'edited',
        change: str = '',
        based_on: int | None = None,
        append: bool = False,
        announce: bool = True,
        **detail,
    ) -> dict:
        """Add a version. based_on is the version the caller started from: if another save landed since, nothing is
        written and StaleVersion is raised. Saving the text that is already the latest adds nothing. Returns the
        record with "saved" telling which happened; detail (a run, a comment round) is kept with the version.
        append adds text to the end of the latest version instead of replacing it. announce=False leaves telling
        on_change to the caller, who has more to write first (a lesson's test must exist before tests are replayed).
        The version and its log entry are written together, so there is never one without the other."""
        if source not in SOURCES:
            raise ValueError(f'Unknown source {source!r}')
        change, detail = clean(change), clean(detail)
        k = self.kind
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            latest = self._latest(c, record_id)
            if append:  # added to whatever is the latest text at this moment, so no edit is ever overwritten
                text = '\n\n'.join(part for part in (latest['text'].rstrip(), text.strip()) if part)
                if len(text) > MAX_TEXT:
                    raise ValueError(
                        f'The {self.text} of {latest["name"]} would pass {MAX_TEXT:,} characters. Shorten them first.'
                    )
            stale = based_on is not None and based_on != latest['version']
            saved = not stale and text != latest['text']
            number = latest['version'] + 1
            ids = {'source': source, 'detail': detail}
            if stale:
                message = f'A save based on version {based_on} was refused; the latest is version {latest["version"]}'
                self._log(c, record_id, f'{k}.save_refused', message, 'WARN', version=latest['version'], based_on=based_on, **ids)  # fmt: skip
            elif saved:
                c.execute(
                    f'INSERT INTO {k}_versions VALUES (?,?,?,?,?,?,?)',
                    (record_id, number, now(), source, change, text, json.dumps(detail, default=str)),
                )
                message = f'Saved version {number} ({source})'
                self._log(c, record_id, f'{k}.version_saved', message, version=number, previous=latest['version'], change=change, chars=len(text), **ids)  # fmt: skip
            result = self._latest(c, record_id)  # read inside the save, so a save landing just after is not returned
        if stale:
            raise StaleVersion(latest['version'], self.text)
        if saved and announce and self.on_change:  # after the version is committed; what follows never undoes it
            self.on_change(record_id, number, source)
        return {**result, 'saved': saved}

    def versions(self, record_id: str) -> list[dict]:
        """The history, newest first, without the full texts."""
        with self.connect() as c:
            self._latest(c, record_id)
            rows = c.execute(
                f'SELECT version, created_at, source, change, detail FROM {self.kind}_versions WHERE {self.kind}_id=? '
                'ORDER BY version DESC',
                (record_id,),
            ).fetchall()
        return [
            {'version': v, 'created_at': at, 'source': source, 'change': change, 'detail': json.loads(detail)}
            for v, at, source, change, detail in rows
        ]

    def version(self, record_id: str, version: int) -> dict:
        with self.connect() as c:
            r = c.execute(
                f'SELECT created_at, source, change, text, detail FROM {self.kind}_versions '
                f'WHERE {self.kind}_id=? AND version=?',
                (record_id, version),
            ).fetchone()
        if not r:
            raise KeyError((record_id, version))
        return {
            'version': version, 'created_at': r[0], 'source': r[1], 'change': r[2], 'text': r[3],
            'detail': json.loads(r[4]),
        }  # fmt: skip

    def restore(self, record_id: str, version: int, based_on: int | None = None) -> dict:
        """Bring an earlier version's text back as a new version; the versions in between stay in the history."""
        text = self.version(record_id, version)['text']
        return self.save(
            record_id, text, 'restored', f'Restored version {version}', based_on=based_on, restored_from=version
        )

    def _log(self, c, record_id: str, name: str, message: str, level: str = 'INFO', **details) -> dict:
        """Write one activity-log entry on the caller's connection, so it lands with the change it describes."""
        event = clean(
            {
                'id': uuid.uuid4().hex,
                f'{self.kind}_id': record_id,
                'timestamp': now(),
                'name': name,
                'message': message,
                'level': level,
                **details,
            }
        )
        seq = c.execute(
            f'INSERT INTO {self.kind}_events({self.kind}_id,data) VALUES (?,?)',
            (record_id, json.dumps(event, default=str)),
        ).lastrowid
        return {**event, 'seq': seq}

    def event(self, record_id: str, name: str, message: str, level: str = 'INFO', **details) -> dict:
        with self.connect() as c:
            return self._log(c, record_id, name, message, level, **details)

    def recent(self, record_id: str, limit: int = 100) -> list[dict]:
        """The latest entries of the activity log, newest first."""
        with self.connect() as c:
            self._latest(c, record_id)
            rows = c.execute(
                f'SELECT seq,data FROM {self.kind}_events WHERE {self.kind}_id=? ORDER BY seq DESC LIMIT ?',
                (record_id, limit),
            ).fetchall()
        return [{**json.loads(data), 'seq': seq} for seq, data in rows]

    def events(self, record_id: str, after: int = 0) -> list[dict]:
        with self.connect() as c:
            rows = c.execute(
                f'SELECT seq,data FROM {self.kind}_events WHERE {self.kind}_id=? AND seq>? ORDER BY seq LIMIT 500',
                (record_id, after),
            ).fetchall()
        return [{**json.loads(data), 'seq': seq} for seq, data in rows]
