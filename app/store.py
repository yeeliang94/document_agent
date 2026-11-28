import json
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from . import app_settings
from .cleaning import clean, now  # noqa: F401 - clean is re-exported for the modules that log through the store
from .clients import Clients
from .playbooks import Playbooks

DAY_FLOOR = 50_000  # tokens; less than this cannot finish even a small run


class Store:
    def __init__(self, root: Path):
        self.root = root
        self._held, self._lock = {}, threading.Lock()  # tokens held for runs going now (hold)
        root.mkdir(parents=True, exist_ok=True)
        self.db = root / 'jobs.db'
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id, seq);
                CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS spend(day TEXT PRIMARY KEY, tokens INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS replay_spend(playbook_id TEXT PRIMARY KEY, runs INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL);
            """)
        self.playbooks = Playbooks(self)
        self.clients = Clients(self)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=15)
        try:
            c.execute('PRAGMA journal_mode=WAL')
            with c:
                yield c
        finally:
            c.close()

    def create(self, module, period, instructions, context, playbook=None):
        """module is the playbook's id; playbook names the version whose text was copied into context."""
        job = {
            'id': uuid.uuid4().hex,
            'module': module,
            'playbook': playbook,
            'skills': [],  # the playbook's skills this job copied: name, description and fingerprint
            'period': period,
            'instructions': instructions,
            'context': context,
            'status': 'ready',
            'created_at': now(),
            'updated_at': now(),
            'files': [],
            'artifacts': [],
            'question': None,
            'answer': None,
            'summary': '',
            'request_count': 0,
            'usage': {},
            'error': None,
        }
        self.save(job)
        for folder in ('inputs', 'template', 'working', 'outputs'):
            (self.path(job['id']) / folder).mkdir(parents=True, exist_ok=True)
        return job

    def path(self, job_id):
        if not re.fullmatch('[a-f0-9]{32}', job_id):
            raise ValueError('That run no longer exists.')
        return self.root / 'jobs' / job_id

    def save(self, job):
        job['updated_at'] = now()
        with self.connect() as c:
            c.execute(
                'INSERT INTO jobs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                (job['id'], json.dumps(job, default=str)),
            )

    def get(self, job_id):
        with self.connect() as c:
            r = c.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not r:
            raise KeyError(job_id)
        return json.loads(r[0])

    def update(self, job_id, **values):
        return self.mutate(job_id, lambda job: job.update(values))

    def mutate(self, job_id, change):
        """Read, change and save one job in a single write transaction, so concurrent writers never lose fields."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r = c.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not r:
                raise KeyError(job_id)
            job = json.loads(r[0])
            change(job)
            job['updated_at'] = now()
            c.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job, default=str), job_id))
        return job

    def jobs(self):
        with self.connect() as c:
            rows = c.execute('SELECT data FROM jobs').fetchall()
        return sorted([json.loads(r[0]) for r in rows], key=lambda j: j['created_at'], reverse=True)

    def summaries(self):
        """Run-history rows without file manifests, prompts or summaries. The hidden jobs that replay a past
        correction (app/replay.py) are not runs the user started, and are left out."""
        fields = ('id', 'name', 'module', 'period', 'status', 'created_at', 'updated_at')
        columns = [f"json_extract(data,'$.{f}')" for f in fields] + [
            "json_extract(data,'$.playbook.name')",
            "json_extract(data,'$.client.name')",
            "json_extract(data,'$.client.id')",
            "substr(json_extract(data,'$.instructions'),1,80)",  # what tells apart runs that have no month
        ]
        with self.connect() as c:
            rows = c.execute(
                'SELECT ' + ','.join(columns) + " FROM jobs WHERE json_extract(data,'$.replay') IS NULL "
                "ORDER BY json_extract(data,'$.created_at') DESC"
            ).fetchall()
        return [dict(zip((*fields, 'playbook', 'client', 'client_id', 'title'), r, strict=True)) for r in rows]

    def replay_jobs(self, playbook_id) -> list[str]:
        """The ids of the hidden jobs that replayed a playbook's past corrections (app/replay.py)."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT id FROM jobs WHERE json_extract(data,'$.module')=? AND json_extract(data,'$.replay') IS NOT NULL",
                (playbook_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def playbook_jobs(self, playbook_id):
        """The runs of one playbook the user started, whole; the hidden replay jobs are left out, as in summaries."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT data FROM jobs WHERE json_extract(data,'$.module')=? AND json_extract(data,'$.replay') IS NULL",
                (playbook_id,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def add_replay_spend(self, playbook_id: str, used: dict):
        """Tally a replayed test's tokens against its playbook. Its job is removed when a later check starts, so the
        tally is what keeps them."""
        with self.connect() as c:
            c.execute(
                'INSERT INTO replay_spend VALUES (?,1,?,?,?) ON CONFLICT(playbook_id) DO UPDATE SET runs=runs+1, '
                'input_tokens=input_tokens+excluded.input_tokens, cached_tokens=cached_tokens+excluded.cached_tokens, '
                'output_tokens=output_tokens+excluded.output_tokens',
                (playbook_id, used['input_tokens'], used['cached_tokens'], used['output_tokens']),
            )

    def replay_spend(self, playbook_id: str) -> dict:
        with self.connect() as c:
            row = c.execute(
                'SELECT runs, input_tokens, cached_tokens, output_tokens FROM replay_spend WHERE playbook_id=?',
                (playbook_id,),
            ).fetchone()
        return dict(zip(('runs', 'input_tokens', 'cached_tokens', 'output_tokens'), row or (0, 0, 0, 0), strict=True))

    def delete(self, job_id):
        with self.connect() as c:
            c.execute('DELETE FROM events WHERE job_id=?', (job_id,))
            c.execute('DELETE FROM jobs WHERE id=?', (job_id,))

    def event(self, job_id, name, message, level='INFO', **details):
        event = clean(
            {
                'id': uuid.uuid4().hex,
                'run_id': job_id,
                'trace_id': job_id,
                'span_id': details.pop('span_id', uuid.uuid4().hex),
                'timestamp': now(),
                'name': name,
                'message': message,
                'level': level,
                **details,
            }
        )
        with self.connect() as c:
            seq = c.execute(
                'INSERT INTO events(job_id,data) VALUES (?,?)', (job_id, json.dumps(event, default=str))
            ).lastrowid
        event['seq'] = seq
        try:
            path = self.path(job_id) / 'events.jsonl'
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
        except OSError:
            logging.exception('JSONL mirror unavailable; authoritative events remain in SQLite')
        return event

    def events(self, job_id, after=0):
        with self.connect() as c:
            rows = c.execute(
                'SELECT seq,data FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT 500', (job_id, after)
            ).fetchall()
        return [{**json.loads(data), 'seq': seq} for seq, data in rows]

    def add_spend(self, tokens: int):
        """Count tokens a run used towards today (this computer's date)."""
        with self.connect() as c:
            c.execute(
                'INSERT INTO spend VALUES (?,?) ON CONFLICT(day) DO UPDATE SET tokens=tokens+excluded.tokens',
                (date.today().isoformat(), int(tokens)),
            )

    def spent_today(self) -> int:
        with self.connect() as c:
            row = c.execute('SELECT tokens FROM spend WHERE day=?', (date.today().isoformat(),)).fetchone()
        return row[0] if row else 0

    def tokens_left_today(self) -> int | None:
        """What is left of the day's token limit from the Settings page, less what runs going now hold; None when
        there is no limit."""
        limit = self.settings()['daily_token_limit_thousands'] * 1000
        with self._lock:
            held = sum(self._held.values())
        return limit - self.spent_today() - held if limit else None

    def hold(self, job_id: str, tokens: int) -> None:
        """Set aside part of the day for a run going now. Its spend is written only when it ends, so without this
        several runs at once would each be given all that is left of the day."""
        with self._lock:
            self._held[job_id] = tokens

    def release(self, job_id: str) -> None:
        with self._lock:
            self._held.pop(job_id, None)

    def day_used_up(self) -> bool:
        """Is too little left of the day to start a run? A run that could only read its files and stop is not
        worth starting, so the last sliver counts as used up."""
        left = self.tokens_left_today()
        return left is not None and left < DAY_FLOOR

    def settings(self):
        """Settings-page values over their defaults."""
        values = app_settings.defaults()
        with self.connect() as c:
            for name, value in c.execute('SELECT name, value FROM settings').fetchall():
                if name in values:
                    values[name] = json.loads(value)
        return values

    def save_settings(self, changes):
        checked = app_settings.validate(changes)
        with self.connect() as c:
            for name, value in checked.items():
                c.execute(
                    'INSERT INTO settings VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                    (name, json.dumps(value)),
                )
        return self.settings()
