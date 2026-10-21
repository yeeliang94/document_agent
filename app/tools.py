import asyncio
import functools
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import column_index_from_string, coordinate_from_string
from pydantic import BaseModel
from pydantic_ai import Agent, BinaryContent, CallDeferred, RunContext, ToolReturn

from . import sheet_reads
from .config import Settings
from .files import SUPPORTED, atomic_bytes, relative_name, within
from .state import JobState, encode, fit_result, identity, paginate
from .store import Store
from .supervisor import failure, normalized, repetition
from .workplan import record_outcome

# Package parts openpyxl cannot carry through a load/save round trip.
DROPPED_PARTS = {
    'xl/drawings/': 'drawings or shapes',
    'xl/charts/': 'charts',
    'xl/media/': 'images',
    'xl/pivotTables/': 'pivot tables',
    'xl/slicers/': 'slicers',
    'xl/vbaProject.bin': 'macros',
}
# Output names that would claim a status the application has not verified.
# Whole words only, so names like "incomplete_items" or "abandoned" stay allowed.
STATUS_WORDS = re.compile(r'(?<![a-z])(completed?|final|validated|verified|approved|done)(?![a-z])', re.IGNORECASE)
# Reads of one source (any offset or range) within the recent action window that earn a warning.
# Identical calls with identical results within this many recent actions count as no progress.
ACTIVITY_WINDOW = 10
_job_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def job_lock(job_id):
    with _locks_guard:
        return _job_locks.setdefault(job_id, threading.Lock())


def package_features(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        features = Counter(
            {
                label: sum(n.startswith(prefix) and '/_rels/' not in n for n in names)
                for prefix, label in DROPPED_PARTS.items()
            }
        )
        # A surviving chart keeps its drawing part even when sibling shapes are lost.
        drawing_ns = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
        for name in names:
            if name.startswith('xl/drawings/') and name.endswith('.xml') and '/_rels/' not in name:
                drawing = ElementTree.fromstring(z.read(name))
                for tag, label in (
                    ('sp', 'shapes'),
                    ('cxnSp', 'connectors'),
                    ('grpSp', 'shape groups'),
                    ('pic', 'drawing images'),
                    ('graphicFrame', 'drawing frames'),
                ):
                    features[label] += sum(1 for _ in drawing.iter('{' + drawing_ns + '}' + tag))
    return features


def read_email(p, root, max_files=2000, max_bytes=200 * 1024 * 1024):
    msg = BytesParser(policy=policy.default).parsebytes(p.read_bytes())
    headers = '\n'.join(f'{k}: {msg[k]}' for k in ('From', 'To', 'Cc', 'Date', 'Subject') if msg[k])
    body = msg.get_body(preferencelist=('plain', 'html'))
    text = body.get_content() if body else ''
    if body is not None and body.get_content_type() == 'text/html':
        text = re.sub(r'<[^>]+>', ' ', text)
    source_id = hashlib.sha256(p.relative_to(root).as_posix().encode()).hexdigest()
    folder = Path('working/attachments') / source_id
    attachments = []
    if len(p.relative_to(root).parts) > 24:
        raise ValueError('Email attachment nesting limit reached')
    for i, part in enumerate(msg.iter_attachments(), 1):
        data = part.get_payload(decode=True) or b''
        if len(attachments) >= max_files or sum(item['size'] for item in attachments) + len(data) > max_bytes:
            raise ValueError('Email attachments exceed the remaining job limits')
        try:
            name = relative_name(Path(part.get_filename() or '').name).as_posix()
        except ValueError:
            name = f'attachment-{i}'
        # MIME part order gives even identical attachment names stable distinct paths.
        rel = (folder / str(i) / name).as_posix()
        atomic_bytes(within(root, rel), data)
        attachments.append({'path': rel, 'size': len(data), 'media_type': part.get_content_type()})
    return headers + '\n\n' + text, {'attachments': attachments} if attachments else {}


def validate_workbook(deps, path):
    output = deps.resolve(path, output=True)
    template = deps.resolve(next(f['path'] for f in deps.current()['files'] if f['role'] == 'template'))
    failures = [
        f'Template {x} were lost when saving; edit this workbook in Excel instead.'
        for x in sorted(package_features(template) - package_features(output))
    ]
    original = load_workbook(template)
    w = load_workbook(output)
    formula_count = 0
    try:
        for sh in original:
            if sh.title not in w.sheetnames:
                failures.append('Missing sheet: ' + sh.title)
                continue
            if sh.sheet_state != w[sh.title].sheet_state:
                failures.append('Visibility changed: ' + sh.title)
            for row in sh:
                for c in row:
                    if c.data_type == 'f':
                        formula_count += 1
                        if w[sh.title][c.coordinate].value != c.value:
                            failures.append('Formula changed: ' + sh.title + '!' + c.coordinate)

        result = {
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'path': output.relative_to(deps.root).as_posix(),
            'reopened': True,
            'formulas_preserved': formula_count,
            'failures': failures,
            'passed': not failures,
            'limitation': 'No formula recalculation; source accounting and business rules need separate checks.',
        }
        atomic_bytes(deps.root / 'working/validation.json', json.dumps(result).encode())
        return result
    finally:
        original.close()
        w.close()


class CellChange(BaseModel):
    sheet: str
    cell: str
    value: str | int | float | bool | None
    number_format: str | None = None
    # The source observation this value accounts for; recording it makes the cell an output assignment.
    record: str | None = None


def assign_cells(state, path, changes, instruction_revision=None):
    """Track source-backed cell writes as output assignments, keeping each retained cell's own provenance."""
    rows = {}
    for change in changes:
        if change.get('record'):
            letters, row = coordinate_from_string(change['cell'])
            rows.setdefault((change['sheet'], row), []).append((column_index_from_string(letters), change))
    mappings = dict(state.all('mapping'))
    revision = state.revision('instruction_revision') if instruction_revision is None else instruction_revision
    for (sheet, row), items in rows.items():
        key = identity(path, sheet, row)
        old = state.get('assignment', key) or {}
        old_mapping = mappings.get(old.get('operation'), {})
        fields = {column['column']: column['field'] for column in old_mapping.get('destination', {}).get('columns', [])}
        # A bulk-written row carries one provenance for all its cells; split it so edits replace only their cell.
        inherited = {
            'sources': old.get('sources', []),
            'dataset': old.get('dataset'),
            'instruction_revision': old_mapping.get('instruction_revision'),
        }
        provenance = old.get('provenance') or {field_name: inherited for field_name in fields.values()}
        values = dict(old.get('values', {}))
        numeric = set(old.get('numeric_fields', []))
        for column, change in items:
            field_name = fields.setdefault(column, get_column_letter(column))
            values[field_name] = change['value']
            is_number = isinstance(change['value'], (int, float)) and not isinstance(change['value'], bool)
            (numeric.add if is_number else numeric.discard)(field_name)
            provenance[field_name] = {'sources': [change['record']], 'dataset': None, 'instruction_revision': revision}
        operation = 'cells:' + key
        destination = {'sheet': sheet, 'columns': [{'field': f, 'column': c} for c, f in sorted(fields.items())]}
        state.put('mapping', operation, {'path': path, 'destination': destination, 'instruction_revision': revision})
        state.put(
            'assignment',
            key,
            {
                'id': key,
                'path': path,
                'sheet': sheet,
                'row': row,
                # Only sources that still back a cell count as included.
                'sources': sorted({source for cell in provenance.values() for source in cell['sources']}),
                'dataset': None,
                'provenance': provenance,
                'operation': operation,
                'values': values,
                'numeric_fields': sorted(numeric),
            },
        )


@dataclass
class JobDeps:
    store: Store
    job_id: str
    settings: Settings | None = None
    actor: str = 'main'
    scope: tuple[str, ...] = ()
    _state: JobState | None = field(default=None, init=False, repr=False)

    @property
    def state(self):
        if self._state is None:
            self._state = JobState(self.root)
            self._state.inventory(self.current()['files'])
        return self._state

    def check_stop(self):
        status = self.current()['status']
        if status in ('pausing', 'paused', 'cancelled'):
            raise InterruptedError('Job stopped; saved work is retained')
        if self.actor != 'main' and self.state.get('task', self.actor, {}).get('status') == 'cancelled':
            raise InterruptedError('Delegated task cancelled')

    @property
    def root(self):
        return self.store.path(self.job_id)

    def allow_artifact(self, path):
        if self.actor != 'main':
            task = self.state.get('task', self.actor)
            self.state.patch('task', self.actor, {'artifacts': sorted(set(task.get('artifacts', []) + [path]))})

    def current(self):
        return self.store.get(self.job_id)

    def resolve(self, path, output=False):
        p = within(self.root, path)
        if self.actor != 'main':
            artifacts = self.state.get('task', self.actor, {}).get('artifacts', [])
            if output or not any(p == within(self.root, item) for item in (*self.scope, *artifacts)):
                raise ValueError('Scout access is limited to its assigned source files')
        if output and not p.is_relative_to(self.root / 'outputs'):
            raise ValueError('Only output copies can be edited')
        return p

    async def action(self, name, args, fn):
        span = uuid.uuid4().hex
        self.store.event(
            self.job_id,
            name + '.started',
            name.replace('_', ' '),
            type='tool',
            arguments=args,
            actor=self.actor,
            span_id=span,
            status='running',
        )
        started = time.monotonic()
        try:
            # Hold the job lock in the worker thread, so a tool that outlived its timeout
            # finishes before a retry touches the same files.
            lock = job_lock(self.job_id)

            def locked():
                with lock:
                    # A pause requested during a model call takes effect at the next tool boundary.
                    if self.current()['status'] != 'pausing':
                        self.check_stop()
                    return fn()

            worker = asyncio.create_task(asyncio.to_thread(locked))
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Do not release the job worker while a timed-out thread can still mutate artifacts.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                raise
            from .progress import observe

            logged = result.return_value if isinstance(result, ToolReturn) else result
            progress_events = observe(self, name, args, logged)
            record_outcome(self.state, self.actor, name, args, logged, progress_events)
            if progress_events:
                self.store.event(
                    self.job_id,
                    'progress.changed',
                    name.replace('_', ' ') + ': saved useful change',
                    type='job',
                    actor=self.actor,
                    progress=progress_events[:20],
                )
            self.store.event(
                self.job_id,
                name,
                name.replace('_', ' '),
                type='tool',
                arguments=args,
                result=logged,
                duration_ms=round((time.monotonic() - started) * 1000),
                span_id=span,
                status='completed',
                actor=self.actor,
            )
            return self.prepare_result(name, args, result)
        except Exception as e:
            self.store.event(
                self.job_id,
                name,
                str(e),
                level='WARN',
                type='tool',
                arguments=args,
                error_type=type(e).__name__,
                error_class=failure(e)['error_class'],
                duration_ms=round((time.monotonic() - started) * 1000),
                span_id=span,
                status='failed',
            )
            record_outcome(self.state, self.actor, name, args, failure(e), [])
            return self.prepare_result(name, args, failure(e))

    async def async_action(self, name, args, work):
        """Run event-loop work (delegation) and report its errors to the model instead of failing the job."""
        try:
            outcome = await work
        except Exception as error:
            outcome = error

        def report():
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return await self.action(name, args, report)

    def prepare_result(self, name, args, result):
        if isinstance(result, ToolReturn):
            result.return_value = self.prepare_result(name, args, result.return_value)
            return result
        if not isinstance(result, ToolReturn):
            instruction_epoch = max((item['seq'] for item in self.state.instructions()), default=0)
            fingerprint = identity(name, normalized(args), normalized(result), instruction_epoch)
            recent, warning, stalled = repetition(
                self.state.get('activity', self.actor, {}).get('recent', []), fingerprint
            )
            actions = [
                *self.state.get('activity', self.actor, {}).get('actions', []),
                {'tool': name, 'arguments': normalized(args)},
            ][-10:]
            self.state.put('activity', self.actor, {'recent': recent, 'actions': actions})
            waiting = (
                name == 'get_task_status' and isinstance(result, dict) and result.get('status') in ('queued', 'running')
            )
            if warning and not waiting:
                self.store.event(
                    self.job_id,
                    'progress.warning',
                    'Repeated action produced no new information.',
                    type='job',
                    level='WARN',
                    actor=self.actor,
                )
            if stalled and not waiting:
                if self.actor == 'main':
                    self.store.update(self.job_id, status='pausing')
                else:
                    # A looping worker stops at its next step; its history and findings remain.
                    self.state.patch(
                        'task', self.actor, {'status': 'cancelled', 'error': 'Repeated actions without progress'}
                    )
                    self.store.event(
                        self.job_id,
                        'task.cancelled',
                        'Delegated task repeated actions without progress.',
                        type='task',
                        task_id=self.actor,
                        level='WARN',
                    )
            if warning and not waiting and isinstance(result, dict):
                result = result | {
                    'progress_warning': 'Repeated actions or an action cycle produced no new information. Change approach, save a result from what you have read, or identify a specific blocker.'
                }
            payload = encode(result).encode()
            limit = (self.settings or Settings()).tool_result_bytes
            if len(payload) > limit:
                # Keep the full result for audit, but hand the model a usable, shortened structure.
                # Paging through a saved result in raw slices was the observed re-reading trap.
                artifact = self.state.artifact('tool-results', result)
                self.allow_artifact(artifact)
                envelope = {
                    'artifact': artifact,
                    'truncated': True,
                    'bytes': len(payload),
                    'instruction': 'Result shortened to fit. Do not reread the saved result; repeat the tool with '
                    'a narrower request (fewer rows, offset/limit, or a specific key).',
                }
                return fit_result(result, limit, envelope)
        return result


def registrar(agent: Agent, readonly, worker_tools):
    """Register tools sequentially; read-only workers receive only the named subset."""

    def tool(fn):
        name = fn.__name__.removesuffix('_tool')
        if readonly and name not in worker_tools:
            return fn
        if name != 'ask_user' and os.getenv('DI_OPERATION_WORKER') != '1':
            from .execution import ISOLATED_TOOLS, execute_tool

            if name in ISOLATED_TOOLS:

                @functools.wraps(fn)
                async def isolated(ctx, **kwargs):
                    try:
                        return await execute_tool(ctx, name, kwargs)
                    except InterruptedError:
                        raise
                    except Exception as error:
                        result = failure(error)
                        ctx.deps.store.event(
                            ctx.deps.job_id,
                            name,
                            result['error'],
                            type='tool',
                            level='WARN',
                            status='failed',
                            actor=ctx.deps.actor,
                            error_class=result['error_class'],
                        )
                        return ctx.deps.prepare_result(name, kwargs, result)

                return agent.tool(name=name, sequential=True)(isolated)
        return agent.tool(name=name, sequential=True)(fn)

    return tool


def register_tools(agent: Agent, readonly=False):
    tool = registrar(agent, readonly, {'list_files', 'read_sheet', 'read_cell', 'read_document', 'view_page'})

    @tool
    async def list_files(ctx: RunContext[JobDeps], offset: int = 0, limit: int = 50) -> dict:
        """List submitted files and discovered sheets with their review status, plus output files. Paginated."""

        def listing():
            state = ctx.deps.state
            if ctx.deps.actor == 'main':
                return state.page('inventory', offset, limit) | {'outputs': ctx.deps.current()['artifacts']}
            allowed = (*ctx.deps.scope, *state.get('task', ctx.deps.actor, {}).get('artifacts', []))
            items = [{'id': key, **item} for key, item in state.all('inventory') if item['path'] in allowed]
            return paginate(items, offset, limit)

        return await ctx.deps.action('list_files', {'offset': offset, 'limit': limit}, listing)

    @tool
    async def read_sheet(
        ctx: RunContext[JobDeps],
        path: str,
        sheet: str,
        start_row: int = 1,
        end_row: int = 100,
        start_column: int = 1,
        end_column: int = 90,
        formulas: bool = False,
    ) -> Any:
        """Read native workbook cells with coordinates. Only non-empty cells are returned (absent coordinates are blank); data_type and number_format appear only when notable. Each read covers at most 10000 cells and stops early at the result size limit; continue from next_row. If one row cannot fit, narrow columns or use read_cell to page its value/note text before advancing. Formula values may be cached/stale."""

        def read():
            return sheet_reads.read_sheet(
                ctx.deps.resolve(path),
                sheet,
                start_row,
                end_row,
                start_column,
                end_column,
                formulas,
                (ctx.deps.settings or Settings()).tool_result_bytes,
            )

        return await ctx.deps.action(
            'read_sheet',
            {
                'path': path,
                'sheet': sheet,
                'range': [start_row, end_row, start_column, end_column],
                'formulas': formulas,
            },
            read,
        )

    @tool
    async def read_cell(
        ctx: RunContext[JobDeps],
        path: str,
        sheet: str,
        cell: str,
        field: Literal['value', 'note'] = 'value',
        offset: int = 0,
        formulas: bool = False,
    ) -> Any:
        """Read the complete text of one native cell value or note in bounded character pages. Follow next_offset until null; finish all columns of an oversized row before advancing read_sheet. Numeric/date values are returned as text here. Formula values may be cached/stale unless formulas is true."""

        return await ctx.deps.action(
            'read_cell',
            {'path': path, 'sheet': sheet, 'cell': cell, 'field': field, 'offset': offset, 'formulas': formulas},
            lambda: sheet_reads.read_cell(
                ctx.deps.resolve(path),
                sheet,
                cell,
                field,
                offset,
                formulas,
                (ctx.deps.settings or Settings()).tool_result_bytes,
            ),
        )

    @tool
    async def read_document(ctx: RunContext[JobDeps], path: str, page: int = 1, offset: int = 0) -> Any:
        """Read a PDF page, Word document, CSV, JSON, UTF-8 text or email. Email attachments are saved under working/attachments and listed for reading. Read scans with view_page. Large text pages through offset, 16000 characters per read."""

        def read():
            p = ctx.deps.resolve(path)
            if p.suffix.lower() == '.pdf':
                from pypdf import PdfReader

                doc = PdfReader(p)
                if page < 1 or page > len(doc.pages):
                    raise ValueError('Invalid page')
                text = doc.pages[page - 1].extract_text() or ''
                meta = {'pages': len(doc.pages), 'page': page, 'needs_vision': len(text.strip()) < 40}
            elif p.suffix.lower() == '.docx':
                from docx import Document

                d = Document(p)
                text = '\n'.join(
                    [x.text for x in d.paragraphs]
                    + [' | '.join(c.text for c in r.cells) for t in d.tables for r in t.rows]
                )
                meta = {}
            elif p.suffix.lower() == '.eml':
                settings = ctx.deps.settings or Settings()
                parent = path
                existing = [entry for entry in ctx.deps.current()['files'] if entry.get('parent') != parent]
                text, meta = read_email(
                    p,
                    ctx.deps.root,
                    settings.max_files - len(existing),
                    settings.max_upload_mb * 1024 * 1024 - sum(entry['size'] for entry in existing),
                )
                derived = [
                    {
                        'path': item['path'],
                        'size': item['size'],
                        'sha256': hashlib.sha256((ctx.deps.root / item['path']).read_bytes()).hexdigest(),
                        'role': 'inputs',
                        'parent': parent,
                        'supported': Path(item['path']).suffix.lower() in SUPPORTED,
                    }
                    for item in meta.get('attachments', [])
                ]

                def attach(job):
                    # Merge into the latest manifest, so a concurrent reader or a status change is never lost.
                    job['files'] = [entry for entry in job['files'] if entry.get('parent') != parent] + derived

                ctx.deps.state.inventory(ctx.deps.store.mutate(ctx.deps.job_id, attach)['files'])
                for entry in derived:
                    ctx.deps.allow_artifact(entry['path'])
            elif p.suffix.lower() in ('.txt', '.csv', '.json'):
                text = p.read_text(encoding='utf-8-sig', errors='replace')
                meta = {}
            else:
                raise ValueError('Use view_page for images; this file type is unsupported by the text reader')
            if offset < 0:
                raise ValueError('Offset must be nonnegative')
            limit = (ctx.deps.settings or Settings()).tool_result_bytes
            if len(encode(meta).encode()) > limit // 2:
                meta = {
                    'attachment_count': len(meta.get('attachments', [])),
                    'instruction': 'Use list_files for attachment paths.',
                }
            chunk = text[offset : offset + min(16000, max(1, limit // 2))]
            result = {
                'text': chunk,
                'offset': offset,
                'next_offset': offset + len(chunk) if offset + len(chunk) < len(text) else None,
                **meta,
            }
            while len(encode(result).encode()) > limit:
                chunk = chunk[: len(chunk) // 2]
                result.update(text=chunk, next_offset=offset + len(chunk) if offset + len(chunk) < len(text) else None)
            ctx.deps.state.record_read(
                path,
                page if p.suffix.lower() == '.pdf' else 1,
                meta.get('pages', 1),
                offset,
                len(chunk),
                len(text),
                needs_vision=meta.get('needs_vision', False),
                actor=ctx.deps.actor,
            )
            return result

        return await ctx.deps.action('read_document', {'path': path, 'page': page, 'offset': offset}, read)

    @tool
    async def view_page(ctx: RunContext[JobDeps], path: str, page: int = 1) -> Any:
        """Return actual pixels of a PDF page or image to this vision-capable model. PDF pages are 1-based. No external OCR service; image payload is not logged."""

        def render():
            from PIL import Image, ImageOps

            p = ctx.deps.resolve(path)
            pages = 1
            if p.suffix.lower() == '.pdf':
                import pypdfium2 as pdfium

                with pdfium.PdfDocument(p) as doc:
                    pages = len(doc)
                    if page < 1 or page > len(doc):
                        raise ValueError('Invalid page')
                    pg = doc[page - 1]
                    try:
                        bitmap = pg.render(scale=2)
                        try:
                            image = bitmap.to_pil().copy()
                        finally:
                            bitmap.close()
                    finally:
                        pg.close()
            else:
                with Image.open(p) as original:
                    image = ImageOps.exif_transpose(original).convert('RGB')
            # Bound transport size; callers may inspect source crops separately when needed.
            image.thumbnail((2048, 2048))
            buf = io.BytesIO()
            image.save(buf, format='PNG')
            payload = buf.getvalue()
            image_id = hashlib.sha256(payload).hexdigest() + '.png'
            atomic_bytes(ctx.deps.root / 'working' / image_id, payload)
            ctx.deps.state.record_read(path, page, pages, vision=True, actor=ctx.deps.actor)
            return ToolReturn(
                {'path': path, 'page': page, 'image_artifact': 'working/' + image_id},
                content=[BinaryContent(data=payload, media_type='image/png')],
            )

        return await ctx.deps.action('view_page', {'path': path, 'page': page}, render)

    @tool
    async def create_output(ctx: RunContext[JobDeps], filename: str = 'payroll_output.xlsx') -> Any:
        """Create an output copy of the uploaded template. Existing output is retained, so resume never resets previous edits. The application labels it draft until validation passes, so the filename must not claim a status."""

        def create():
            if len(relative_name(filename).parts) != 1 or not filename.endswith('.xlsx'):
                raise ValueError('Use a simple .xlsx filename')
            if STATUS_WORDS.search(filename):
                raise ValueError(
                    'Do not put a status such as completed, final or validated in the filename; '
                    'the application shows draft or validated itself'
                )
            dest = ctx.deps.root / 'outputs' / filename
            if not dest.exists():
                template = next(f for f in ctx.deps.current()['files'] if f['role'] == 'template')
                atomic_bytes(dest, ctx.deps.resolve(template['path']).read_bytes())
            return {'path': 'outputs/' + filename, 'size': dest.stat().st_size}

        return await ctx.deps.action('create_output', {'filename': filename}, create)

    @tool
    async def write_cells(ctx: RunContext[JobDeps], path: str, changes: list[CellChange]) -> Any:
        """Write explicit {sheet,cell,value} changes to an output workbook copy, preserving existing formulas. Dates should be Excel dates or ISO text with a suitable number_format. Optional number_format per change. Set record to the source observation ID a value accounts for, so the row counts as an output assignment. No row appending: explicit coordinates make retries idempotent."""
        changes = [change.model_dump() for change in changes]

        def write():
            if len(changes) > 2000:
                raise ValueError('Write at most 2000 cells per call')
            p = ctx.deps.resolve(path, output=True)
            unknown = {ch['record'] for ch in changes if ch.get('record')} - {
                key for key, _ in ctx.deps.state.all('observation')
            }
            if unknown:
                raise ValueError('Unknown source record: ' + sorted(unknown)[0])
            from .requirements import check_write_scope

            for change in changes:
                letters, row = coordinate_from_string(change['cell'])
                check_write_scope(ctx.deps.state, path, change['sheet'], row, row, [column_index_from_string(letters)])
            before_hash = hashlib.sha256(p.read_bytes()).hexdigest()
            operation_id = 'cells:' + identity(
                path, changes, before_hash, ctx.deps.state.revision('instruction_revision')
            )
            if any(j.get('status') == 'prepared' for _, j in ctx.deps.state.all('operation')):
                raise ValueError('Recover prepared output journals before writing')
            w = load_workbook(p)
            try:
                for ch in changes:
                    if not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}', str(ch.get('cell', ''))):
                        raise ValueError('Use a single Excel cell coordinate')
                    cell = w[ch['sheet']][ch['cell']]
                    if cell.data_type == 'f' and ch['value'] != cell.value:
                        raise ValueError(f'Preserve formula {ch["sheet"]}!{ch["cell"]}')
                    value = ch['value']
                    if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) and not cell.data_type == 'f':
                        cell.value = value
                        cell.data_type = 's'
                    else:
                        cell.value = value
                    if ch.get('number_format'):
                        cell.number_format = ch['number_format']
                out = io.BytesIO()
                w.save(out)
                payload = out.getvalue()
                journal = {
                    'kind': 'cells',
                    'status': 'prepared',
                    'before_sha256': before_hash,
                    'sha256': hashlib.sha256(payload).hexdigest(),
                    'changes': changes,
                    'instruction_revision': ctx.deps.state.revision('instruction_revision'),
                    'result': {'path': path},
                }
                ctx.deps.state.put('operation', operation_id, journal)
                atomic_bytes(p, payload)
            finally:
                w.close()
            assign_cells(ctx.deps.state, p.relative_to(ctx.deps.root).as_posix(), changes)
            ctx.deps.state.put('operation', operation_id, journal | {'status': 'completed'})
            return {'path': path, 'cells_written': len(changes)}

        return await ctx.deps.action('write_cells', {'path': path, 'changes': changes}, write)

    @tool
    async def validate_output(ctx: RunContext[JobDeps], path: str) -> Any:
        """Check the output workbook: template sheets, formulas and features kept, every assigned cell still matches its source record, destination requirements met, and all sources accounted for. Completion requires a current pass. Excel formulas are not recalculated."""

        def validate():
            from .harness_tools import validate_job

            workbook = validate_workbook(ctx.deps, path)
            job = validate_job(ctx.deps, path)
            return job | {
                'failures': [*workbook['failures'], *job['failures']][:100],
                'failure_count': len(workbook['failures']) + job['failure_count'],
                'workbook': {key: workbook[key] for key in ('passed', 'formulas_preserved', 'failures')},
            }

        return await ctx.deps.action('validate_output', {'path': path}, validate)

    @tool
    async def save_report(ctx: RunContext[JobDeps], filename: str, text: str) -> Any:
        """Save a concise exception/processing report as UTF-8 .txt or .md alongside the workbook."""

        def save():
            if len(relative_name(filename).parts) != 1 or Path(filename).suffix not in ('.txt', '.md'):
                raise ValueError('Use a .txt or .md filename')
            atomic_bytes(ctx.deps.root / 'outputs' / filename, text.encode('utf-8'))
            return {'path': 'outputs/' + filename}

        return await ctx.deps.action('save_report', {'filename': filename, 'text': text}, save)

    @tool
    async def ask_user(ctx: RunContext[JobDeps], question: str) -> str:
        """Ask a necessary material question that the sources cannot resolve. Save progress first; continue unrelated work before asking. Job releases its worker until answered."""
        ctx.deps.store.update(ctx.deps.job_id, question={'call_id': ctx.tool_call_id, 'text': question})
        ctx.deps.store.event(ctx.deps.job_id, 'user.question', question, type='question')
        raise CallDeferred
