"""Read-only previews of a job's files for the browser: sheets as rows, PDFs as page images, the rest as text.

Previews are capped, so a huge workbook or document never ties up the server or the page.
"""

import csv
import io
import json
import threading
import zipfile
from datetime import date, datetime, time
from itertools import chain
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .engine.checks import PROVENANCE, parse_rows
from .files import within

FOLDERS = ('inputs', 'template', 'outputs')
MAX_ROWS = 500
MAX_COLUMNS = 40
MAX_CHARS = 200_000
MAX_DOCX_BYTES = 100 * 1024**2  # unpacked
SHEETS = {'.xlsx', '.xlsm'}
IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
TEXT = {'.txt', '.md', '.json', '.eml', '.log', '.xml', '.html', '.htm'}
PAGE_PIXELS = 1800  # longest side of a rendered PDF page
PDF_LOCK = threading.Lock()  # PDFium is not safe to call from two threads at once


def job_file(root: Path, relative: str) -> Path:
    """The job file a browser asked for. Only uploads, the template and outputs can be read."""
    path = within(root, relative)
    if (
        path.relative_to(root).parts[:1] not in [(f,) for f in FOLDERS]
        or path.name.endswith('.pending')
        or not path.is_file()
    ):
        raise FileNotFoundError(relative)
    return path


def _cell(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=' ', timespec='seconds')
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value)


def _trim(rows: list[list]) -> list[list]:
    """Drop empty trailing rows and columns so the grid shows only what is there."""
    while rows and all(v in (None, '') for v in rows[-1]):
        rows.pop()
    width = max((i + 1 for row in rows for i, v in enumerate(row) if v not in (None, '')), default=0)
    return [row[:width] for row in rows]


def _sheet(path: Path, wanted: str | None) -> dict:
    # Formulas a program wrote have no stored result, so fall back to the formula text for those cells.
    values = load_workbook(path, read_only=True, data_only=True)
    formulas = load_workbook(path, read_only=True, data_only=False)
    try:
        names = [{'name': ws.title, 'hidden': ws.sheet_state != 'visible'} for ws in values.worksheets]
        if not names:
            return {'kind': 'sheet', 'sheets': [], 'sheet': None, 'columns': [], 'rows': [], 'more': False}
        title = (
            wanted
            if wanted in values.sheetnames
            else next((s['name'] for s in names if not s['hidden']), names[0]['name'])
        )
        box = {'max_row': MAX_ROWS + 1, 'max_col': MAX_COLUMNS, 'values_only': True}
        rows = [
            [_cell(shown if shown is not None else typed) for shown, typed in zip(a, b, strict=False)]
            for a, b in zip(values[title].iter_rows(**box), formulas[title].iter_rows(**box), strict=False)
        ]
    finally:
        values.close()
        formulas.close()
    more = len(rows) > MAX_ROWS
    rows = _trim(rows[:MAX_ROWS])
    return {'kind': 'sheet', 'sheets': names, 'sheet': title, 'rows': rows, 'more': more, **_columns(rows)}


def _columns(rows: list[list]) -> dict:
    return {'columns': [get_column_letter(i + 1) for i in range(max((len(r) for r in rows), default=0))]}


def _text(path: Path) -> str:
    with open(path, encoding='utf-8-sig', errors='replace') as handle:
        return handle.read(MAX_CHARS + 1)


def _csv(path: Path) -> dict:
    text = _text(path)
    rows = [row[:MAX_COLUMNS] for row in csv.reader(io.StringIO(text[:MAX_CHARS]))]
    more = len(rows) > MAX_ROWS or len(text) > MAX_CHARS
    rows = _trim(rows[:MAX_ROWS])
    return {'kind': 'sheet', 'sheets': [], 'sheet': None, 'rows': rows, 'more': more, **_columns(rows)}


def _docx(path: Path) -> str:
    from docx import Document

    # A Word file is a zip; a small one can unpack to something huge, so look before opening it.
    with zipfile.ZipFile(path) as archive:
        if sum(member.file_size for member in archive.infolist()) > MAX_DOCX_BYTES:
            raise ValueError('The document is too large to preview')
    document = Document(str(path))
    rows = (row for table in document.tables for row in table.rows)
    lines, size = [], 0
    for line in chain((p.text for p in document.paragraphs), ('\t'.join(c.text for c in row.cells) for row in rows)):
        lines.append(line)
        size += len(line) + 1
        if size > MAX_CHARS + 1:  # the preview is full; the rest is never read
            break
    return '\n'.join(lines)


def row_sources(outputs: Path, name: str, sheet: str | None, paths: list[str]) -> dict[str, list[str]]:
    """For one output sheet, the uploaded files each row came from, as the agent recorded them."""
    try:
        records = json.loads((outputs / PROVENANCE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    # The agent cites paths inside the packet, sometimes with the sandbox folder in front. Like the engine's own
    # check, take the longest ending of the cited path that is a stored file.
    endings = {}
    for stored in paths:
        parts = stored.split('/')
        for i in range(len(parts)):
            endings.setdefault('/'.join(parts[i:]), stored)

    def stored_file(cited) -> str:
        parts = str(cited or '').strip().replace('\\', '/').removeprefix('./').split('/')
        return next((endings[t] for t in ('/'.join(parts[i:]) for i in range(len(parts))) if t in endings), str(cited))

    found: dict[str, list[str]] = {}
    for record in records if isinstance(records, list) else []:
        try:
            if record['file'] != name or record['sheet'] != sheet:
                continue
            rows = parse_rows(record['rows'], MAX_ROWS)  # only rows the preview shows
            files = [stored_file(s) for s in record['sources']]
        except (KeyError, TypeError, ValueError):
            continue
        for row in rows:
            known = found.setdefault(str(row), [])
            known += [f for f in files if f not in known]
    return found


def preview(root: Path, relative: str, sheet: str | None = None, paths: list[str] | None = None) -> dict:
    path = job_file(root, relative)
    kind = path.suffix.lower()
    try:
        if kind in SHEETS:
            body = _sheet(path, sheet)
            if path.parent == root / 'outputs':
                body['row_sources'] = row_sources(path.parent, path.name, body['sheet'], paths or [])
        elif kind == '.csv':
            body = _csv(path)
        elif kind == '.pdf':
            import pypdfium2 as pdfium

            with PDF_LOCK, pdfium.PdfDocument(path) as document:
                body = {'kind': 'pages', 'pages': len(document)}
        elif kind in IMAGES:
            body = {'kind': 'image'}
        elif kind in TEXT or kind == '.docx':
            text = _docx(path) if kind == '.docx' else _text(path)
            body = {'kind': 'text', 'text': text[:MAX_CHARS], 'more': len(text) > MAX_CHARS}
        else:
            body = {'kind': 'none'}
    except Exception as error:  # noqa: BLE001 - a damaged file must not break the page; say why it cannot be shown
        body = {'kind': 'none', 'error': f'{type(error).__name__}: {error}'[:300]}
    return {'path': relative, 'name': path.name, 'size': path.stat().st_size, **body}


def page_png(root: Path, relative: str, page: int) -> bytes:
    """One PDF page as a PNG, sized for reading on screen."""
    import pypdfium2 as pdfium

    path = job_file(root, relative)
    with PDF_LOCK, pdfium.PdfDocument(path) as document:
        if page < 1 or page > len(document):
            raise ValueError('Invalid page')
        sheet = document[page - 1]
        try:
            # Size the render from the page, so an enormous page never asks for gigabytes of memory.
            longest = max(*sheet.get_size(), 1)
            bitmap = sheet.render(scale=min(2, PAGE_PIXELS / longest))
            try:
                image = bitmap.to_pil().copy()
            finally:
                bitmap.close()
        finally:
            sheet.close()
    out = io.BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()
