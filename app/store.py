import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONTEXT = {
    'payroll': 'Prepare the monthly payroll ingestion workbook from the supplied files. Match employee IDs; reconcile joiners, changes, allowances, one-time payments and resignations. Read template instructions and lookup tabs. Preserve formulas. Investigate conflicts and ask when material facts are missing. Continue independent records.',
    'ap': 'Prepare the accounts payable upload. Match supplier IDs and invoice numbers, identify duplicates, reconcile amounts, and preserve the supplied template. Ask about material unresolved conflicts.',
}

MODULE_LABELS = {'payroll': 'Payroll', 'ap': 'Accounts payable'}


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    if isinstance(value, dict):
        if value.get('kind') in ('binary', 'binary-content') or value.get('media_type', '').startswith('image/'):
            return {'kind': 'image', 'media_type': value.get('media_type'), 'data': '[image payload omitted]'}
        return {
            k: ('[redacted]' if any(x in k.lower() for x in ('api_key', 'authorization', 'access_token')) else clean(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', '[image payload omitted]', value)
        value = re.sub(r'sk-[A-Za-z0-9_-]{12,}', '[credential omitted]', value)
        return value[:24000] + (' [truncated]' if len(value) > 24000 else '')
    return value


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = root / 'jobs.db'
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id, seq);
                CREATE TABLE IF NOT EXISTS profiles(module TEXT PRIMARY KEY, context TEXT NOT NULL);
            """)
            for module, context in DEFAULT_CONTEXT.items():
                c.execute('INSERT OR IGNORE INTO profiles VALUES (?,?)', (module, context))

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=15)
        try:
            c.execute('PRAGMA journal_mode=WAL')
            with c:
                yield c
        finally:
            c.close()

    def create(self, module, period, instructions, context):
        job = {
            'id': uuid.uuid4().hex,
            'module': module,
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
            raise ValueError('Invalid job ID')
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
        job = self.get(job_id)
        job.update(values)
        self.save(job)
        return job

    def jobs(self):
        with self.connect() as c:
            rows = c.execute('SELECT data FROM jobs').fetchall()
        return sorted([json.loads(r[0]) for r in rows], key=lambda j: j['created_at'], reverse=True)

    def summaries(self):
        """Run-history rows without file manifests, prompts or summaries."""
        fields = ('id', 'module', 'period', 'status', 'created_at', 'updated_at')
        with self.connect() as c:
            rows = c.execute(
                'SELECT '
                + ','.join(f"json_extract(data,'$.{f}')" for f in fields)
                + " FROM jobs ORDER BY json_extract(data,'$.created_at') DESC"
            ).fetchall()
        return [dict(zip(fields, r, strict=True)) for r in rows]

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

    def profile(self, module):
        with self.connect() as c:
            return c.execute('SELECT context FROM profiles WHERE module=?', (module,)).fetchone()[0]

    def save_profile(self, module, context):
        with self.connect() as c:
            c.execute('UPDATE profiles SET context=? WHERE module=?', (context, module))
