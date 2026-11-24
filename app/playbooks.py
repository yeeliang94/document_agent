"""Playbooks: the named instructions the agent follows on every run of one kind of work.

A playbook's instructions are kept as versions. A version is never edited or deleted: every change, a restore
included, adds a new one, so a run can always name the exact text it used. Each playbook also has an activity log
for what happens to it outside a run (saves, restores, refusals), next to the per-job log in store.py.
"""

from __future__ import annotations

import json
import uuid

from .cleaning import clean, now
from .modules import DEFAULT_CONTEXT, MODULE_LABELS
from .skills import Skills

MAX_NAME = 80
MAX_TEXT = 30_000  # characters of instructions in one version
SOURCES = frozenset({'created', 'edited', 'remembered', 'learned', 'tuned', 'tidied', 'restored'})


class StaleVersion(Exception):
    """A save was based on a version that is no longer the latest; nothing was written."""

    def __init__(self, current: int):
        super().__init__(f'The instructions changed since you opened them (now version {current}).')
        self.current = current


class Playbooks:
    def __init__(self, store):
        self.connect = store.connect
        # Set by the app once it runs: called with (playbook id, version, source) after each saved version, so
        # the tests kept for the playbook can be reviewed and replayed (app/runtime.py).
        self.on_change = None
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS playbooks(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS playbook_versions(
                    playbook_id TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
                    source TEXT NOT NULL, change TEXT NOT NULL, text TEXT NOT NULL, detail TEXT NOT NULL,
                    PRIMARY KEY(playbook_id, version));
                CREATE TABLE IF NOT EXISTS playbook_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, playbook_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS playbook_events_playbook ON playbook_events(playbook_id, seq);
            """)
            # The first playbooks, made once. A database from before playbooks kept its instructions in a
            # `profiles` table; the text edited there is carried over, and that table is left as it was.
            first = dict(DEFAULT_CONTEXT)
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='profiles'").fetchone():
                first.update(c.execute('SELECT module, context FROM profiles ORDER BY rowid').fetchall())
            for module, context in first.items():
                made = c.execute(
                    'INSERT OR IGNORE INTO playbooks VALUES (?,?,?)', (module, MODULE_LABELS.get(module, module), now())
                ).rowcount
                if made:
                    c.execute(
                        'INSERT INTO playbook_versions VALUES (?,?,?,?,?,?,?)',
                        (module, 1, now(), 'created', '', context, '{}'),
                    )
        self.skills = Skills(self, store.root)

    @staticmethod
    def _latest(c, playbook_id: str) -> dict:
        r = c.execute(
            'SELECT p.name, p.created_at, v.version, v.created_at, v.text FROM playbooks p '
            'JOIN playbook_versions v ON v.playbook_id=p.id WHERE p.id=? ORDER BY v.version DESC LIMIT 1',
            (playbook_id,),
        ).fetchone()
        if not r:
            raise KeyError(playbook_id)
        return {'id': playbook_id, 'name': r[0], 'created_at': r[1], 'version': r[2], 'updated_at': r[3], 'text': r[4]}

    @staticmethod
    def _name(c, name: str, playbook_id: str = '') -> str:
        name = ' '.join((name or '').split())
        if not name:
            raise ValueError('Enter a name')
        if len(name) > MAX_NAME:
            raise ValueError(f'Use a name of at most {MAX_NAME} characters')
        if c.execute('SELECT 1 FROM playbooks WHERE lower(name)=lower(?) AND id<>?', (name, playbook_id)).fetchone():
            raise ValueError('A playbook with this name already exists')
        return name

    def list(self) -> list[dict]:
        with self.connect() as c:
            rows = c.execute(
                'SELECT p.id, p.name, p.created_at, MAX(v.version), MAX(v.created_at) FROM playbooks p '
                'JOIN playbook_versions v ON v.playbook_id=p.id GROUP BY p.id ORDER BY p.created_at, p.rowid'
            ).fetchall()
        return [dict(zip(('id', 'name', 'created_at', 'version', 'updated_at'), r, strict=True)) for r in rows]

    def get(self, playbook_id: str) -> dict:
        """The playbook with its latest instructions."""
        with self.connect() as c:
            return self._latest(c, playbook_id)

    def create(self, name: str, copy_of: str | None = None) -> dict:
        """A blank playbook, or one that starts from another's latest instructions and skills."""
        playbook_id = uuid.uuid4().hex
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            name = self._name(c, name)
            source = self._latest(c, copy_of) if copy_of else None
            detail = {'copied_from': copy_of, 'copied_version': source['version']} if source else {}
            c.execute('INSERT INTO playbooks VALUES (?,?,?)', (playbook_id, name, now()))
            c.execute(
                'INSERT INTO playbook_versions VALUES (?,?,?,?,?,?,?)',
                (playbook_id, 1, now(), 'created', '', source['text'] if source else '', json.dumps(detail)),
            )
        self.event(playbook_id, 'playbook.created', f'Created "{name}"', version=1, **detail)
        if copy_of:
            try:
                self.skills.copy(copy_of, playbook_id)
            except Exception:
                # A copy that cannot bring its skills along is not left behind half made.
                self.skills.discard(playbook_id)
                with self.connect() as c:
                    for table in ('playbook_versions', 'playbook_events'):
                        c.execute(f'DELETE FROM {table} WHERE playbook_id=?', (playbook_id,))
                    c.execute('DELETE FROM playbooks WHERE id=?', (playbook_id,))
                self.event(copy_of, 'playbook.copy_failed', f'A copy named "{name}" could not be made', 'WARN')
                raise
        return self.get(playbook_id)

    def rename(self, playbook_id: str, name: str) -> dict:
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            before = self._latest(c, playbook_id)['name']
            name = self._name(c, name, playbook_id)
            c.execute('UPDATE playbooks SET name=? WHERE id=?', (name, playbook_id))
        if name != before:
            self.event(playbook_id, 'playbook.renamed', f'Renamed "{before}" to "{name}"')
        return self.get(playbook_id)

    def save(
        self,
        playbook_id: str,
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
        playbook with "saved" telling which happened; detail (a run, a comment round) is kept with the version.
        append adds text to the end of the latest version instead of replacing it. announce=False leaves telling
        on_change to the caller, who has more to write first (a lesson's test must exist before tests are replayed).
        The version and its log entry are written together, so there is never one without the other."""
        if source not in SOURCES:
            raise ValueError(f'Unknown source {source!r}')
        change, detail = clean(change), clean(detail)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            latest = self._latest(c, playbook_id)
            if append:  # added to whatever is the latest text at this moment, so no edit is ever overwritten
                text = '\n\n'.join(part for part in (latest['text'].rstrip(), text.strip()) if part)
                if len(text) > MAX_TEXT:
                    raise ValueError(
                        f'The instructions of {latest["name"]} would pass {MAX_TEXT:,} characters. Shorten them first.'
                    )
            stale = based_on is not None and based_on != latest['version']
            saved = not stale and text != latest['text']
            number = latest['version'] + 1
            ids = {'source': source, 'detail': detail}
            if stale:
                message = f'A save based on version {based_on} was refused; the latest is version {latest["version"]}'
                self._log(c, playbook_id, 'playbook.save_refused', message, 'WARN', version=latest['version'], based_on=based_on, **ids)  # fmt: skip
            elif saved:
                c.execute(
                    'INSERT INTO playbook_versions VALUES (?,?,?,?,?,?,?)',
                    (playbook_id, number, now(), source, change, text, json.dumps(detail, default=str)),
                )
                message = f'Saved version {number} ({source})'
                self._log(c, playbook_id, 'playbook.version_saved', message, version=number, previous=latest['version'], change=change, chars=len(text), **ids)  # fmt: skip
            result = self._latest(c, playbook_id)  # read inside the save, so a save landing just after is not returned
        if stale:
            raise StaleVersion(latest['version'])
        if (
            saved and announce and self.on_change
        ):  # after the version is committed; what follows it never undoes the save
            self.on_change(playbook_id, number, source)
        return {**result, 'saved': saved}

    def versions(self, playbook_id: str) -> list[dict]:
        """The history, newest first, without the full texts."""
        with self.connect() as c:
            self._latest(c, playbook_id)
            rows = c.execute(
                'SELECT version, created_at, source, change, detail FROM playbook_versions WHERE playbook_id=? '
                'ORDER BY version DESC',
                (playbook_id,),
            ).fetchall()
        return [
            {'version': v, 'created_at': at, 'source': source, 'change': change, 'detail': json.loads(detail)}
            for v, at, source, change, detail in rows
        ]

    def version(self, playbook_id: str, version: int) -> dict:
        with self.connect() as c:
            r = c.execute(
                'SELECT created_at, source, change, text, detail FROM playbook_versions WHERE playbook_id=? AND version=?',
                (playbook_id, version),
            ).fetchone()
        if not r:
            raise KeyError((playbook_id, version))
        return {
            'version': version, 'created_at': r[0], 'source': r[1], 'change': r[2], 'text': r[3],
            'detail': json.loads(r[4]),
        }  # fmt: skip

    def restore(self, playbook_id: str, version: int, based_on: int | None = None) -> dict:
        """Bring an earlier version's text back as a new version; the versions in between stay in the history."""
        text = self.version(playbook_id, version)['text']
        return self.save(
            playbook_id, text, 'restored', f'Restored version {version}', based_on=based_on, restored_from=version
        )

    @staticmethod
    def _log(c, playbook_id: str, name: str, message: str, level: str = 'INFO', **details) -> dict:
        """Write one activity-log entry on the caller's connection, so it lands with the change it describes."""
        event = clean(
            {
                'id': uuid.uuid4().hex,
                'playbook_id': playbook_id,
                'timestamp': now(),
                'name': name,
                'message': message,
                'level': level,
                **details,
            }
        )
        seq = c.execute(
            'INSERT INTO playbook_events(playbook_id,data) VALUES (?,?)', (playbook_id, json.dumps(event, default=str))
        ).lastrowid
        return {**event, 'seq': seq}

    def event(self, playbook_id: str, name: str, message: str, level: str = 'INFO', **details) -> dict:
        with self.connect() as c:
            return self._log(c, playbook_id, name, message, level, **details)

    def recent(self, playbook_id: str, limit: int = 100) -> list[dict]:
        """The latest entries of the activity log, newest first."""
        with self.connect() as c:
            self._latest(c, playbook_id)
            rows = c.execute(
                'SELECT seq,data FROM playbook_events WHERE playbook_id=? ORDER BY seq DESC LIMIT ?',
                (playbook_id, limit),
            ).fetchall()
        return [{**json.loads(data), 'seq': seq} for seq, data in rows]

    def events(self, playbook_id: str, after: int = 0) -> list[dict]:
        with self.connect() as c:
            rows = c.execute(
                'SELECT seq,data FROM playbook_events WHERE playbook_id=? AND seq>? ORDER BY seq LIMIT 500',
                (playbook_id, after),
            ).fetchall()
        return [{**json.loads(data), 'seq': seq} for seq, data in rows]
