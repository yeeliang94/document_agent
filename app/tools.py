import asyncio
import hashlib
import io
import json
import re
import threading
import time
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from pydantic import BaseModel
from pydantic_ai import Agent, BinaryContent, CallDeferred, RunContext, ToolReturn

from .files import atomic_bytes, relative_name, within
from .store import Store

# Package parts openpyxl cannot carry through a load/save round trip.
DROPPED_PARTS = {
    'xl/drawings/': 'drawings or shapes',
    'xl/charts/': 'charts',
    'xl/media/': 'images',
    'xl/pivotTables/': 'pivot tables',
    'xl/slicers/': 'slicers',
    'xl/vbaProject.bin': 'macros',
}
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


def unsized(sh):
    # Some generators omit the sheet dimension; read-only mode then reports no size.
    if sh.max_row is None or sh.max_column is None:
        sh.calculate_dimension(force=True)
    return sh


def read_email(p, root):
    msg = BytesParser(policy=policy.default).parsebytes(p.read_bytes())
    headers = '\n'.join(f'{k}: {msg[k]}' for k in ('From', 'To', 'Cc', 'Date', 'Subject') if msg[k])
    body = msg.get_body(preferencelist=('plain', 'html'))
    text = body.get_content() if body else ''
    if body is not None and body.get_content_type() == 'text/html':
        text = re.sub(r'<[^>]+>', ' ', text)
    source_id = hashlib.sha256(p.relative_to(root).as_posix().encode()).hexdigest()
    folder = Path('working/attachments') / source_id
    attachments = []
    for i, part in enumerate(msg.iter_attachments(), 1):
        data = part.get_payload(decode=True) or b''
        try:
            name = relative_name(Path(part.get_filename() or '').name).as_posix()
        except ValueError:
            name = f'attachment-{i}'
        # MIME part order gives even identical attachment names stable distinct paths.
        rel = (folder / str(i) / name).as_posix()
        atomic_bytes(within(root, rel), data)
        attachments.append({'path': rel, 'size': len(data), 'media_type': part.get_content_type()})
    return headers + '\n\n' + text, {'attachments': attachments} if attachments else {}


class CellChange(BaseModel):
    sheet: str
    cell: str
    value: str | int | float | bool | None
    number_format: str | None = None


@dataclass
class JobDeps:
    store: Store
    job_id: str

    @property
    def root(self):
        return self.store.path(self.job_id)

    def current(self):
        return self.store.get(self.job_id)

    def resolve(self, path, output=False):
        p = within(self.root, path)
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
                    return fn()

            result = await asyncio.to_thread(locked)
            logged = result.return_value if isinstance(result, ToolReturn) else result
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
            )
            return result
        except Exception as e:
            self.store.event(
                self.job_id,
                name,
                str(e),
                level='WARN',
                type='tool',
                arguments=args,
                error_type=type(e).__name__,
                duration_ms=round((time.monotonic() - started) * 1000),
                span_id=span,
                status='failed',
            )
            return {
                'error': str(e),
                'error_type': type(e).__name__,
                'instruction': 'Investigate or use another tool; continue independent work.',
            }


def register_tools(agent: Agent):
    @agent.tool(sequential=True)
    async def list_files(ctx: RunContext[JobDeps]) -> dict:
        """List all submitted files with relative paths, hashes and duplicate links, plus outputs and saved working memory."""
        j = ctx.deps.current()
        return {
            'files': j['files'],
            'artifacts': j['artifacts'],
            'memory': json.loads((ctx.deps.root / 'working/memory.json').read_text())
            if (ctx.deps.root / 'working/memory.json').exists()
            else {},
        }

    @agent.tool(sequential=True)
    async def inspect_workbook(ctx: RunContext[JobDeps], path: str) -> Any:
        """Inspect every sheet, visibility, dimensions, first populated rows, comments, formulas and validation lists. Previews do not establish completeness."""

        def read():
            w = load_workbook(ctx.deps.resolve(path), data_only=False)
            try:
                sheets = []
                for sh in w:
                    rows = []
                    for row in sh.iter_rows(min_row=1, max_row=min(sh.max_row, 20), max_col=min(sh.max_column, 100)):
                        cells = [
                            {
                                'cell': c.coordinate,
                                'value': str(c.value) if c.value is not None else None,
                                'note': c.comment.text if c.comment else None,
                            }
                            for c in row
                            if c.value is not None or c.comment
                        ]
                        if cells:
                            rows.append(cells)
                    sheets.append(
                        {
                            'name': sh.title,
                            'visibility': sh.sheet_state,
                            'rows': sh.max_row,
                            'columns': sh.max_column,
                            'preview': rows,
                            'merged_ranges': [str(r) for r in sh.merged_cells.ranges],
                            'validation_lists': [
                                {'range': str(v.sqref), 'formula': v.formula1}
                                for v in sh.data_validations.dataValidation
                            ],
                        }
                    )
                return {
                    'sheets': sheets,
                    'preview_limits': {'rows': 20, 'columns': 100},
                    'instruction': 'Read applicable rows beyond this preview with read_sheet.',
                }
            finally:
                w.close()

        return await ctx.deps.action('inspect_workbook', {'path': path}, read)

    @agent.tool(sequential=True)
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
        """Read native workbook cells with coordinates. Iterate across all applicable rows; each read is limited to 10000 cells. Formula values may be cached/stale."""

        def read():
            if (
                start_row < 1
                or start_column < 1
                or end_row < start_row
                or end_column < start_column
                or (end_row - start_row + 1) * (end_column - start_column + 1) > 10000
            ):
                raise ValueError('Use a valid range of at most 10000 cells')
            w = load_workbook(ctx.deps.resolve(path), read_only=True, data_only=not formulas)
            try:
                sh = unsized(w[sheet])
                return {
                    'sheet': sheet,
                    'rows': [
                        [
                            {
                                'cell': f'{get_column_letter(start_column + column_index)}{start_row + row_index}',
                                'value': c.value.isoformat() if hasattr(c.value, 'isoformat') else c.value,
                            }
                            for column_index, c in enumerate(row)
                        ]
                        for row_index, row in enumerate(
                            sh.iter_rows(
                                min_row=start_row,
                                max_row=min(end_row, sh.max_row),
                                min_col=start_column,
                                max_col=end_column,
                            )
                        )
                    ],
                    'last_row': min(end_row, sh.max_row),
                    'sheet_rows': sh.max_row,
                    'more_rows': end_row < sh.max_row,
                    'formulas': formulas,
                }
            finally:
                w.close()

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

    @agent.tool(sequential=True)
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
                text, meta = read_email(p, ctx.deps.root)
            elif p.suffix.lower() in ('.txt', '.csv', '.json'):
                text = p.read_text(encoding='utf-8-sig', errors='replace')
                meta = {}
            else:
                raise ValueError('Use view_page for images; this file type is unsupported by the text reader')
            if offset < 0:
                raise ValueError('Offset must be nonnegative')
            return {
                'text': text[offset : offset + 16000],
                'offset': offset,
                'next_offset': offset + 16000 if len(text) > offset + 16000 else None,
                **meta,
            }

        return await ctx.deps.action('read_document', {'path': path, 'page': page, 'offset': offset}, read)

    @agent.tool(sequential=True)
    async def view_page(ctx: RunContext[JobDeps], path: str, page: int = 1) -> Any:
        """Return actual pixels of a PDF page or image to this vision-capable model. PDF pages are 1-based. No external OCR service; image payload is not logged."""

        def render():
            from PIL import Image, ImageOps

            p = ctx.deps.resolve(path)
            if p.suffix.lower() == '.pdf':
                import pypdfium2 as pdfium

                with pdfium.PdfDocument(p) as doc:
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
            # The model downsizes anything larger than 2048px, so bigger images only cost upload time.
            image.thumbnail((2048, 2048))
            buf = io.BytesIO()
            image.save(buf, format='PNG')
            payload = buf.getvalue()
            image_id = uuid.uuid4().hex + '.png'
            atomic_bytes(ctx.deps.root / 'working' / image_id, payload)
            return ToolReturn(
                {'path': path, 'page': page, 'image_artifact': 'working/' + image_id},
                content=[BinaryContent(data=payload, media_type='image/png')],
            )

        return await ctx.deps.action('view_page', {'path': path, 'page': page}, render)

    @agent.tool(sequential=True)
    async def create_output(ctx: RunContext[JobDeps], filename: str = 'payroll_completed.xlsx') -> Any:
        """Create an output copy of the uploaded template. Existing output is retained, so resume never resets previous edits."""

        def create():
            if len(relative_name(filename).parts) != 1 or not filename.endswith('.xlsx'):
                raise ValueError('Use a simple .xlsx filename')
            dest = ctx.deps.root / 'outputs' / filename
            if not dest.exists():
                template = next(f for f in ctx.deps.current()['files'] if f['role'] == 'template')
                atomic_bytes(dest, ctx.deps.resolve(template['path']).read_bytes())
            return {'path': 'outputs/' + filename, 'size': dest.stat().st_size}

        return await ctx.deps.action('create_output', {'filename': filename}, create)

    @agent.tool(sequential=True)
    async def write_cells(ctx: RunContext[JobDeps], path: str, changes: list[CellChange]) -> Any:
        """Write explicit {sheet,cell,value} changes to an output workbook copy, preserving existing formulas. Dates should be Excel dates or ISO text with a suitable number_format. Optional number_format per change. No row appending: explicit coordinates make retries idempotent."""
        changes = [change.model_dump() for change in changes]

        def write():
            if len(changes) > 2000:
                raise ValueError('Write at most 2000 cells per call')
            p = ctx.deps.resolve(path, output=True)
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
                atomic_bytes(p, out.getvalue())
            finally:
                w.close()
            return {'path': path, 'cells_written': len(changes)}

        return await ctx.deps.action('write_cells', {'path': path, 'changes': changes}, write)

    @agent.tool(sequential=True)
    async def validate_output(ctx: RunContext[JobDeps], path: str) -> Any:
        """Reopen output and check template sheets and existing formulas. This does not prove record completeness or recalculate Excel formulas; reconcile the actual source rows separately."""

        def validate():
            output = ctx.deps.resolve(path, output=True)
            template = ctx.deps.resolve(next(f['path'] for f in ctx.deps.current()['files'] if f['role'] == 'template'))
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
                    'path': output.relative_to(ctx.deps.root).as_posix(),
                    'reopened': True,
                    'formulas_preserved': formula_count,
                    'failures': failures,
                    'passed': not failures,
                    'limitation': 'No formula recalculation; source accounting and business rules need separate checks.',
                }
                atomic_bytes(ctx.deps.root / 'working/validation.json', json.dumps(result).encode())
                return result
            finally:
                original.close()
                w.close()

        return await ctx.deps.action('validate_output', {'path': path}, validate)

    @agent.tool(sequential=True)
    async def save_memory(ctx: RunContext[JobDeps], key: str, value: str) -> Any:
        """Save findings, source accounting, mappings and progress outside context. Pass value as plain text or a JSON-encoded string (not a raw object). Read back via list_files. Save before large operations or compaction."""

        def save():
            p = ctx.deps.root / 'working/memory.json'
            memory = json.loads(p.read_text()) if p.exists() else {}
            try:
                memory[key] = json.loads(value)
            except (ValueError, TypeError):
                memory[key] = value
            atomic_bytes(p, json.dumps(memory, default=str, ensure_ascii=False).encode())
            return {'saved': key}

        return await ctx.deps.action('save_memory', {'key': key, 'value': value}, save)

    @agent.tool(sequential=True)
    async def save_report(ctx: RunContext[JobDeps], filename: str, text: str) -> Any:
        """Save a concise exception/processing report as UTF-8 .txt or .md alongside the workbook."""

        def save():
            if len(relative_name(filename).parts) != 1 or Path(filename).suffix not in ('.txt', '.md'):
                raise ValueError('Use a .txt or .md filename')
            atomic_bytes(ctx.deps.root / 'outputs' / filename, text.encode('utf-8'))
            return {'path': 'outputs/' + filename}

        return await ctx.deps.action('save_report', {'filename': filename, 'text': text}, save)

    @agent.tool(sequential=True)
    async def ask_user(ctx: RunContext[JobDeps], question: str) -> str:
        """Ask a necessary material question that the sources cannot resolve. Save progress first; continue unrelated work before asking. Job releases its worker until answered."""
        ctx.deps.store.update(ctx.deps.job_id, question={'call_id': ctx.tool_call_id, 'text': question})
        ctx.deps.store.event(ctx.deps.job_id, 'user.question', question, type='question')
        raise CallDeferred
