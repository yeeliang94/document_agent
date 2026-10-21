"""Durable, job-local facts. Conversation summaries are never the source of truth."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager

from .files import atomic_bytes

# Delegated task statuses that still need settling before a job can complete.
UNSETTLED_TASKS = ('queued', 'running', 'interrupted')
# Operational bookkeeping that does not change job facts, so it leaves validation current.
UNVERSIONED_KINDS = (
    'operation',
    'task',
    'validation',
    'context',
    'activity',
    'progress_event',
    'supervision',
    'supervision_request',
    'instruction_read',
    'checkpoint',
    'plan',
    'plan_history',
    'evidence',
    'approach',
    'memory',
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


# The resume card stays this small however large the job grows.
RESUME_CARD_BYTES = 4000
# Progressively tighter caps until a shortened result fits: (list items kept, string characters kept).
FIT_STEPS = ((50, 2000), (20, 1000), (10, 400), (5, 200), (2, 100), (1, 60))


def shorten(value, items, chars):
    if isinstance(value, dict):
        return {key: shorten(item, items, chars) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        kept = [shorten(item, items, chars) for item in value[:items]]
        return kept + ([{'omitted_items': len(value) - items}] if len(value) > items else [])
    if isinstance(value, str) and len(value) > chars:
        return value[:chars] + f'… [{len(value) - chars} more characters]'
    return value


def fit_result(result, limit, envelope=None):
    """Fit the complete serialized response, including its envelope, within a UTF-8 byte limit."""
    envelope = envelope or {}
    if not isinstance(result, dict):
        result = {'value': result}
    if len(encode(result | envelope).encode()) <= limit:
        return result | envelope
    for items, chars in FIT_STEPS:
        shortened = shorten(result, items, chars) | envelope
        if len(encode(shortened).encode()) <= limit:
            return shortened
    preview = encode(result)
    low, high = 0, min(len(preview), limit)
    if len(encode({'preview': ''} | envelope).encode()) > limit:
        raise ValueError('Result envelope exceeds the byte limit')
    # JSON escaping and multi-byte characters make a character count insufficient.
    while low < high:
        middle = (low + high + 1) // 2
        if len(encode({'preview': preview[:middle]} | envelope).encode()) <= limit:
            low = middle
        else:
            high = middle - 1
    return {'preview': preview[:low]} | envelope


def identity(*parts):
    return hashlib.sha256(encode(parts).encode()).hexdigest()[:32]


def check_page(offset, limit):
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError('Use a nonnegative offset and a limit between 1 and 100')


def page_result(items, offset, total):
    return {'items': items, 'total': total, 'next_offset': offset + len(items) if offset + len(items) < total else None}


def paginate(items, offset, limit):
    check_page(offset, limit)
    return page_result(items[offset : offset + limit], offset, len(items))


# Facts a user instruction can invalidate individually.
INVALIDATABLE = (
    'dataset',
    'mapping',
    'decision',
    'inventory',
    'task',
    'region_role',
    'region_mapping',
    'requirement',
    'plan',
)


class JobState:
    def __init__(self, root):
        self.root = root
        self.path = root / 'working/state.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS facts(kind TEXT, id TEXT, data TEXT NOT NULL,
                    PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO meta VALUES('revision',0);
                INSERT OR IGNORE INTO meta VALUES('instruction_revision',0);
                CREATE TABLE IF NOT EXISTS instructions(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL, kind TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute('PRAGMA table_info(instructions)')}
            if 'applied' not in columns:
                # Instructions recorded before scoped application already invalidated everything.
                try:
                    db.execute('ALTER TABLE instructions ADD COLUMN applied INTEGER NOT NULL DEFAULT 1')
                    db.execute('ALTER TABLE instructions ADD COLUMN impact TEXT')
                except sqlite3.OperationalError as error:
                    # Another connection upgraded the table first.
                    if 'duplicate column' not in str(error):
                        raise

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            with db:
                yield db
        finally:
            db.close()

    def revision(self, key='revision'):
        with self.connect() as db:
            return db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0]

    def get(self, kind, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT data FROM facts WHERE kind=? AND id=?', (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    def existing(self, kind, keys):
        """The subset of keys already stored for kind, in one connection."""
        keys, found = list(keys), set()
        with self.connect() as db:
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                marks = ','.join('?' * len(chunk))
                query = f'SELECT id FROM facts WHERE kind=? AND id IN ({marks})'
                found.update(row[0] for row in db.execute(query, (kind, *chunk)))
        return found

    def has_reference(self, key):
        """Opaque durable references for the format-independent work plan."""
        with self.connect() as db:
            return db.execute('SELECT 1 FROM facts WHERE id=? LIMIT 1', (key,)).fetchone() is not None

    def put(self, kind, key, data):
        self.put_many(kind, [(key, data)])

    def patch(self, kind, key, updates):
        """Merge worker metadata without overwriting cancellation or newer findings."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM facts WHERE kind=? AND id=?', (kind, key)).fetchone()
            if not row:
                raise ValueError('Cannot update a missing fact')
            value = json.loads(row[0]) | updates
            db.execute('UPDATE facts SET data=? WHERE kind=? AND id=?', (encode(value), kind, key))
        return value

    def put_many(self, kind, entries):
        with self.connect() as db:
            changed = False
            for key, data in entries:
                payload = encode(data)
                old = db.execute('SELECT data FROM facts WHERE kind=? AND id=?', (kind, key)).fetchone()
                if old and old[0] == payload:
                    continue
                db.execute('INSERT OR REPLACE INTO facts VALUES(?,?,?)', (kind, key, payload))
                changed = True
            if changed and kind not in UNVERSIONED_KINDS:
                db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")

    def count(self, kind):
        with self.connect() as db:
            return db.execute('SELECT count(*) FROM facts WHERE kind=?', (kind,)).fetchone()[0]

    def all(self, kind):
        with self.connect() as db:
            rows = db.execute('SELECT id,data FROM facts WHERE kind=? ORDER BY id', (kind,)).fetchall()
        return [(key, json.loads(data)) for key, data in rows]

    def page(self, kind, offset=0, limit=50):
        check_page(offset, limit)
        with self.connect() as db:
            count = db.execute('SELECT count(*) FROM facts WHERE kind=?', (kind,)).fetchone()[0]
            rows = db.execute(
                'SELECT id,data FROM facts WHERE kind=? ORDER BY id LIMIT ? OFFSET ?', (kind, limit, offset)
            ).fetchall()
        return page_result([{'id': key, **json.loads(data)} for key, data in rows], offset, count)

    def file_entry(self, path):
        """The inventory ID and entry of a submitted file, or (None, None)."""
        return next(
            ((key, item) for key, item in self.all('inventory') if item['path'] == path and item['kind'] == 'file'),
            (None, None),
        )

    def delete_many(self, kind, keys):
        with self.connect() as db:
            db.executemany('DELETE FROM facts WHERE kind=? AND id=?', [(kind, key) for key in keys])
            if db.total_changes:
                db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")

    def instruction(self, text, kind='message'):
        """Record a user instruction. Validation goes stale; the agent then decides what it affects."""
        with self.connect() as db:
            db.execute('INSERT INTO instructions(text,kind,applied) VALUES(?,?,0)', (text, kind))
            db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")

    def instructions(self):
        with self.connect() as db:
            return [
                {'seq': seq, 'kind': kind, 'text': text, 'applied': bool(applied), 'impact': impact}
                for seq, kind, text, applied, impact in db.execute(
                    'SELECT seq,kind,text,applied,impact FROM instructions ORDER BY seq'
                )
            ]

    def apply_instructions(self, seqs, reason, invalidate=(), reset_all=False):
        """Settle user instructions, invalidating only the named facts unless everything is affected."""
        pending = {item['seq'] for item in self.instructions() if not item['applied']}
        if not seqs or not set(seqs) <= pending:
            raise ValueError('Name pending instruction numbers from read_job_state')
        targets = []
        for key in invalidate:
            kind = next((kind for kind in INVALIDATABLE if self.get(kind, key) is not None), None)
            if kind is None:
                raise ValueError('Unknown fact to invalidate: ' + key)
            targets.append((kind, key))
        note = {'stale': reason}
        for kind, key in targets:
            if kind == 'decision':
                self.delete_many('decision', [key])
            elif kind == 'inventory':
                # The source needs a fresh disposition; structural discovery remains valid.
                item = self.get('inventory', key)
                status = 'inspected' if item['status'] in ('reviewed', 'excluded') else item['status']
                self.put('inventory', key, item | {'status': status, 'reason': reason})
            elif kind == 'task':
                self.patch('task', key, {'status': 'cancelled', 'error': 'Superseded by user instruction'})
            else:
                self.put(kind, key, self.get(kind, key) | note)
        plan = self.get('plan', 'current')
        if plan:
            affected = set(invalidate)
            changed = False
            for item in plan.get('items', []):
                if reset_all or affected.intersection(item.get('evidence', [])):
                    item.update(status='pending', blocker='New instructions require re-evaluation: ' + reason)
                    changed = True
            if changed:
                self.put('plan', 'current', plan | {'stale': reason})
        with self.connect() as db:
            impact = encode({'reason': reason, 'invalidated': [key for _, key in targets], 'reset_all': reset_all})
            db.executemany('UPDATE instructions SET applied=1, impact=? WHERE seq=?', [(impact, seq) for seq in seqs])
            keys = "('revision','instruction_revision')" if reset_all else "('revision')"
            db.execute(f'UPDATE meta SET value=value+1 WHERE key IN {keys}')
        return {'applied': sorted(seqs), 'invalidated': [key for _, key in targets], 'reset_all': reset_all}

    def inventory(self, files):
        for entry in files:
            key = identity(entry['path'], entry['sha256'])
            if self.get('inventory', key) is None:
                self.put('inventory', key, {**entry, 'kind': 'file', 'status': 'discovered'})

    def artifact(self, folder, value):
        payload = encode(value).encode()
        relative = f'working/{folder}/{hashlib.sha256(payload).hexdigest()}.json'
        atomic_bytes(self.root / relative, payload)
        return relative

    def record_read(
        self, path, page, pages, offset=0, length=0, total=0, vision=False, needs_vision=False, actor='main'
    ):
        if not any(item['path'] == path and item.get('role') != 'template' for _, item in self.all('inventory')):
            return
        # Only the main agent's own reads establish document coverage; a worker's reads stay separate.
        key = path if actor == 'main' else f'{actor}:{path}'
        reads = self.get('read', key, {'pages': pages, 'content': {}})
        content = reads['content'].get(str(page), {'intervals': [], 'vision': False})
        intervals = sorted(content['intervals'] + [[offset, min(offset + length, total)]])
        merged = []
        for start, end in intervals:
            if end < start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        reads['content'][str(page)] = {
            'intervals': merged,
            'total': total,
            'vision': content['vision'] or vision,
            'needs_vision': needs_vision,
        }
        reads['pages'] = pages

        def complete(number):
            entry = reads['content'].get(str(number), {})
            return entry.get('vision') or (
                not entry.get('needs_vision', True) and entry.get('intervals') == [[0, entry.get('total')]]
            )

        reads['complete'] = all(complete(number) for number in range(1, pages + 1))
        self.put('read', key, reads)

    def coverage(self):
        inventory = self.all('inventory')
        observations = self.all('observation')
        decisions = dict(self.all('decision'))
        assignments = self.all('assignment')
        read_coverage = dict(self.all('read'))
        assigned = {source for _, item in assignments for source in item['sources']}
        counts = {'pending': 0, 'included': 0, 'excluded': 0, 'unresolved': 0}
        instruction_revision = self.revision('instruction_revision')
        excluded_parents = {
            key
            for key, item in inventory
            if item['status'] == 'excluded'
            and item.get('instruction_revision', instruction_revision) == instruction_revision
        }
        issues = [
            {'id': f'instruction:{item["seq"]}', 'reason': 'User instruction has not been applied'}
            for item in self.instructions()
            if not item['applied']
        ]
        template_ids = {key for key, item in inventory if item.get('role') == 'template'}
        for key, item in inventory:
            if key in template_ids or item.get('parent') in template_ids or item.get('parent') in excluded_parents:
                continue
            status = item['status']
            if (
                status in ('reviewed', 'excluded')
                and item.get('instruction_revision', instruction_revision) != instruction_revision
            ):
                status = 'unresolved'
            document_unreviewed = (
                item['kind'] == 'file'
                and not item['path'].lower().endswith('.xlsx')
                and (
                    status not in ('reviewed', 'excluded')
                    or item.get('instruction_revision', instruction_revision) != instruction_revision
                    or (status == 'reviewed' and not read_coverage.get(item['path'], {}).get('complete'))
                )
            )
            if status not in ('reviewed', 'excluded', 'inspected') or document_unreviewed:
                issues.append({'id': key, 'reason': item.get('error', 'Source has no disposition')})
            if item['kind'] == 'sheet' and status != 'excluded' and not item.get('scan_complete'):
                issues.append({'id': key, 'reason': 'Sheet discovery is incomplete'})
            if (
                item['kind'] == 'file'
                and item['path'].lower().endswith('.xlsx')
                and status != 'excluded'
                and not any(child.get('parent') == key for _, child in inventory)
            ):
                issues.append({'id': key, 'reason': 'Workbook sheets have not been discovered'})
        for key, item in observations:
            if item.get('file') in template_ids:
                continue
            if item['source'] in excluded_parents or item.get('file') in excluded_parents:
                counts['excluded'] += 1
                continue
            decision = decisions.get(key, {})
            if decision.get('instruction_revision', instruction_revision) != instruction_revision:
                decision = {}
            status = decision.get('status', 'pending')
            if key in assigned and status not in ('excluded', 'unresolved'):
                status = 'included'
            if status == 'included' and key not in assigned:
                status = 'unresolved'
            counts[status] += 1
            if status in ('pending', 'unresolved'):
                issues.append(
                    {'id': key, 'reason': decision.get('reason', 'Record has no output assignment or exclusion')}
                )
        from .regions import accounting_version, region_coverage

        region_counts = {}
        if accounting_version(self) == 2:
            scoped = region_coverage(self)
            # Region accounting replaces legacy row obligations, while user instructions still block.
            issues = [issue for issue in issues if issue['id'].startswith('instruction:')] + scoped['issues']
            counts, region_counts = scoped['records'], scoped['regions']
        # A source can back several output rows; one successful sibling cannot settle a rejected row.
        issues.extend(
            {'id': key, 'reason': 'Unresolved output record: ' + '; '.join(item['issues'])}
            for key, item in self.all('output_record')
            if item['issues']
        )
        return {
            'accounting_version': accounting_version(self),
            'regions': region_counts,
            'inventory_items': len(inventory),
            'source_rows': sum('row' in item and item.get('file') not in template_ids for _, item in observations),
            'source_observations': sum(item.get('file') not in template_ids for _, item in observations),
            'records': counts,
            'output_assignments': len(assignments),
            'open_items': len(issues),
            'issues': issues[:50],
            'passed': not issues,
            'revision': self.revision(),
            'instruction_revision': self.revision('instruction_revision'),
        }

    def pending_vision(self):
        """Pages the main agent found to have no usable text but never looked at, unless the file was excluded."""
        inventory = [item for _, item in self.all('inventory')]
        excluded = {item['path'] for item in inventory if item.get('status') == 'excluded'}
        known = {item['path'] for item in inventory}
        pending = []
        for path, reads in self.all('read'):
            # Worker reads are keyed actor:path and do not establish coverage.
            if path in excluded or path not in known:
                continue
            for page, entry in sorted(reads['content'].items(), key=lambda item: int(item[0])):
                if entry.get('needs_vision') and not entry.get('vision'):
                    pending.append({'path': path, 'page': int(page)})
        return pending

    def resume_card(self):
        """A small, always-current account of where the job stands, injected after the context is reduced.

        It points to details instead of copying them, so it can never be the oversized result it replaces.
        """
        plan = self.get('plan', 'current', {})
        coverage = self.coverage()
        instructions = self.instructions()
        approaches = sorted((item for _, item in self.all('approach')), key=lambda item: -item['count'])
        card = {
            'objective': plan.get('objective', ''),
            'focus': plan.get('focus', ''),
            'work_items': [
                {key: item[key] for key in ('id', 'status', 'objective', 'blocker') if item.get(key)}
                for item in plan.get('items', [])
            ],
            'next_actions': plan.get('next_actions', []),
            'last_result': self.get('checkpoint', 'main', {}).get('message'),
            'saved': {
                'requirements': self.count('requirement'),
                'interpretations': self.count('region_role') + self.count('decision'),
                'datasets': self.count('dataset'),
                'observations': self.count('observation'),
                'output_assignments': coverage['output_assignments'],
                'open_items': coverage['open_items'],
            },
            'top_open_items': coverage['issues'][:5],
            'unviewed_scanned_pages': self.pending_vision()[:10],
            # Applied instructions still constrain the work; the full records stay retrievable by sequence ID.
            'user_instructions': {
                'total': len(instructions),
                'pending': sum(not item['applied'] for item in instructions),
                'recent': [
                    {field: item[field] for field in ('seq', 'kind', 'text', 'applied')} for item in instructions[-10:]
                ],
                'read': 'Read all pages with read_job_state(kind="instruction", offset=0, limit=10), including '
                'applied instructions, before continuing. Read full text with key=seq, offset=0, limit=4000; '
                'follow next_offset until null.',
            },
            'most_repeated_approaches': [
                {'tool': item['tool'], 'count': item['count'], 'outcome': item['outcome']}
                for item in approaches[:5]
                if item['count'] > 1
            ],
            'details': 'Use read_job_state with a kind (plan, requirement, region_role, dataset, mapping, memory, '
            'approach, validation, instruction) and key/offset for anything listed here.',
        }
        return fit_result(card, RESUME_CARD_BYTES)

    def snapshot(self):
        snapshot = {
            'plan': self.get('plan', 'current'),
            'checkpoint': self.get('checkpoint', 'main'),
            'attempted_approaches': self.page('approach', limit=5),
            'revision': self.revision(),
            'instruction_revision': self.revision('instruction_revision'),
            'instructions': self.instructions(),
            'coverage': self.coverage(),
            'datasets': self.page('dataset', limit=10),
            'regions': self.page('region', limit=10),
            'evidence': self.page('evidence', limit=8),
            'requirements': self.page('requirement', limit=3),
            'inventory': self.page('inventory', limit=10),
            'mappings': self.page('mapping', limit=10),
            'tasks': self.page('task', limit=10),
            'memory': self.page('memory', limit=10),
        }
        # A handover is an index to facts, not another copy of large mappings or notes.
        for kind in ('mappings', 'tasks', 'memory', 'requirements', 'regions', 'inventory'):
            snapshot[kind]['items'] = [
                {'id': item['id'], 'summary': encode(item)[:400]} for item in snapshot[kind]['items']
            ]
        return snapshot
