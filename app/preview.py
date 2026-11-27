"""Read-only previews of a job's files for the browser: sheets as rows, PDFs as page images, the rest as text.

Previews are capped, so a huge workbook or document never ties up the server or the page.
"""

import csv
import io
import json
import re
import threading
import zipfile
from datetime import date, datetime, time
from itertools import chain
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from . import cell_format, formula_calc
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


def version_file(root: Path, number: int, relative: str) -> Path:
    """An output as a kept version holds it (app/engine/versions.py): <job>/versions/v0001/files/, or the version's
    own folder for versions kept before the files had a folder of their own. Only outputs are kept."""
    folder, _, name = relative.partition('/')
    base = root / 'versions' / f'v{number:04d}'
    holder = base / 'files' if (base / 'files').is_dir() else base
    if folder != 'outputs' or not name or number < 1:
        raise FileNotFoundError(relative)
    path = within(holder, name)
    record = holder == base and path == base / 'version.json'  # the old layout keeps the record among the files
    if record or not path.is_file() or path.name.endswith('.pending'):
        raise FileNotFoundError(relative)
    return path


def any_file(root: Path, relative: str, version: int | None = None) -> Path:
    """A job file as it is now, or an output as a version kept it."""
    return job_file(root, relative) if version is None else version_file(root, version, relative)


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
    # Formulas a program wrote have no stored result: work those out, and show the formula text only when that fails.
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
        box = {'max_row': MAX_ROWS + 1, 'max_col': MAX_COLUMNS}
        theme = cell_format.theme_colours(values.loaded_theme)
        rows, looks, styles = [], [], cell_format.Styles(theme)
        calc = formula_calc.Calculator(formulas, values)
        for r, (a, b) in enumerate(
            zip(values[title].iter_rows(**box), formulas[title].iter_rows(**box, values_only=True), strict=False), 1
        ):
            shown = [
                formula_calc.shown_value(cell, typed, calc, title, r, c) for c, (cell, typed) in enumerate(zip(a, b, strict=False), 1)
            ]
            rows.append([_cell(value) for value, _ in shown])
            looks.append([_look(cell, styles, *pair) for cell, pair in zip(a, shown, strict=False)])
    finally:
        values.close()
        formulas.close()
    more = len(rows) > MAX_ROWS
    rows = _trim(rows[:MAX_ROWS])
    formats = {
        f'{get_column_letter(c + 1)}{r + 1}': look
        for r, row in enumerate(rows)
        for c, look in enumerate(looks[r][: len(row)])
        if look
    }
    return {
        'kind': 'sheet',
        'sheets': names,
        'sheet': title,
        'rows': rows,
        'styles': styles.found,
        'formats': formats,
        'more': more,
        **_columns(rows),
    }


def _look(cell, styles: 'cell_format.Styles', value, formula: str | None) -> dict:
    """How one cell looks in Excel: `style` (a number into the sheet's style list), the `text` Excel shows, and the
    `formula` behind a value the preview worked out.

    Empty for a plain cell. A sheet reuses a handful of styles, so each is sent once rather than per cell.
    """
    look = {'formula': formula} if formula else {}
    if not getattr(cell, 'has_style', False):  # the blank cells that pad a short row have no style at all
        return look
    number = styles.number(cell)
    if number is not None:
        look['style'] = number
    shown = cell_format.shown_text(value, cell.number_format)
    if shown:
        look['text'] = shown[0]
        if shown[1]:
            look['color'] = shown[1]
    return look


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


def text_of(path: Path) -> str:
    """A text or Word file as the preview shows it."""
    return _docx(path) if path.suffix.lower() == '.docx' else _text(path)


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


def preview(root: Path, relative: str, sheet: str | None = None, paths: list[str] | None = None, version: int | None = None) -> dict:
    path = any_file(root, relative, version)
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
            text = text_of(path)
            body = {'kind': 'text', 'text': text[:MAX_CHARS], 'more': len(text) > MAX_CHARS}
        else:
            body = {'kind': 'none'}
    except Exception as error:  # noqa: BLE001 - a damaged file must not break the page; say why it cannot be shown
        body = {'kind': 'none', 'error': f'{type(error).__name__}: {error}'[:300]}
    return {'path': relative, 'name': path.name, 'size': path.stat().st_size, **body}


def page_png(root: Path, relative: str, page: int, version: int | None = None) -> bytes:
    """One PDF page as a PNG, sized for reading on screen."""
    import pypdfium2 as pdfium

    path = any_file(root, relative, version)
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


# --- Finding text in a PDF -------------------------------------------------

MAX_FIND_PAGES = 300  # pages of a PDF that a search reads
MAX_HITS = 500
NUMBER = re.compile(r'^\(?-?\s*(?:RM|MYR|USD|SGD|EUR|GBP|AUD|[A-Z]{0,2}\$|€|£)?\s*(\d[\d,]*(?:\.\d+)?)\s*\)?%?$')


def find_in_pdf(
    root: Path, relative: str, needles: list[str], whole_words: bool = False, version: int | None = None
) -> tuple[list[dict], bool, bool]:
    """Where each needle appears in a PDF, page by page: {page, x, y, w, h, needle} with the box as shares of the
    page's width and height. Also whether the search stopped before the end, and whether any page read has text: a
    scanned page has none, so nothing can be found on it. Case is ignored.

    Each page's text is read once and only the needles on it are searched for their boxes. PDFium is shared by the
    whole app, so it is held one page at a time: page images for other people are not kept waiting for a search."""
    import pypdfium2 as pdfium

    path = any_file(root, relative, version)
    if path.suffix.lower() != '.pdf':
        raise ValueError('Only a PDF can be searched this way')
    needles = [n.strip() for n in needles]
    hits: list[dict] = []
    text = False
    with PDF_LOCK:
        document = pdfium.PdfDocument(path)
    try:
        with PDF_LOCK:
            pages = len(document)
        for number in range(min(pages, MAX_FIND_PAGES)):
            with PDF_LOCK:
                if _search_page(document, number, needles, whole_words, hits):
                    text = True
            if len(hits) >= MAX_HITS:
                return hits[:MAX_HITS], True, True
        return hits, pages > MAX_FIND_PAGES, text
    finally:
        with PDF_LOCK:
            document.close()


def _search_page(document, number: int, needles: list[str], whole_words: bool, hits: list[dict]) -> bool:
    """Add the matches on one page to `hits`. Whether the page has any text."""
    sheet = document[number]
    textpage = sheet.get_textpage()
    try:
        words = textpage.get_text_range().lower()
        if not words.strip():
            return False
        width, height = sheet.get_size()
        for n, needle in enumerate(needles):
            if not needle or needle.lower() not in words:
                continue
            searcher = textpage.search(needle, match_case=False, match_whole_word=whole_words)
            try:
                while (found := searcher.get_next()) is not None and len(hits) < MAX_HITS:
                    box = _box(textpage, *found, width, height)
                    if box:
                        hits.append({'page': number + 1, **box, 'needle': n})
            finally:
                searcher.close()
        return True
    finally:
        textpage.close()
        sheet.close()


def _box(textpage, index: int, count: int, width: float, height: float) -> dict | None:
    """The box around one match, as shares of the page. A match that runs onto a second line gets one box around both."""
    rects = [textpage.get_rect(i) for i in range(textpage.count_rects(index, count))]
    if not rects or width <= 0 or height <= 0:
        return None
    left, right = min(r[0] for r in rects), max(r[2] for r in rects)
    bottom, top = min(r[1] for r in rects), max(r[3] for r in rects)

    def share(value: float, whole: float) -> float:
        return min(max(value / whole, 0), 1)

    x, y = share(left, width), share(height - top, height)
    return {'x': x, 'y': y, 'w': share(right, width) - x, 'h': share(height - bottom, height) - y}


def _forms(value: str) -> list[str]:
    """The ways a sheet's value may be written in a source: an amount like 2500.85 also as 2,500.85, a whole
    number also with .00. Signs, brackets and currency are dropped, since sources write them in many ways. Values
    too short to tell apart, and zero, are left out: they would be found everywhere."""
    text = value.strip()
    match = NUMBER.match(text)
    if not match:
        return [text] if len(text) >= 3 else []
    digits = match.group(1).replace(',', '')
    amount = float(digits)
    core = digits.rstrip('0').rstrip('.') if '.' in digits else digits
    if amount == 0 or sum(ch.isdigit() for ch in core.lstrip('0.')) < 3:  # 7 or 40.00 would be found everywhere
        return []
    forms = [f'{amount:,.2f}', f'{amount:.2f}']
    if amount == int(amount):
        forms += [f'{int(amount):,}', str(int(amount))]
    if '.' in match.group(1) and len(match.group(1).split('.')[1]) > 2:
        forms.append(match.group(1))
    return [f for f in dict.fromkeys(forms) if len(f) >= 3]


MAX_FORMS = 120  # ways of writing a row's values looked for in one source
MAX_SEARCH_ROWS = 5000  # rows of each sheet, or lines of a text file, searched for a row's values


def _spot(row: int, column: int, sheet: str | None = None) -> dict:
    """Where a link opens for a value found at this cell: the cell itself while the preview shows it, else the sheet."""
    spot = {'sheet': sheet} if sheet else {}
    if row <= MAX_ROWS and column <= MAX_COLUMNS:
        spot['cell'] = f'{get_column_letter(column)}{row}'
    return spot


def _where_values(path: Path, groups: list[tuple[str, ...]]) -> tuple[dict[int, str], dict[int, dict]]:
    """Where each group of forms is first found in a workbook, CSV or text file, as the page names the place. A
    number is matched as a number, so 4500 in a cell is found for 4,500.00; text is matched as a whole word or
    number, as the page outlines it (2,500.85 is in "Total 2,500.85" but not in "12,500.85"). Also the exact spot of
    each, for a link that opens the file there: {'sheet', 'cell'} in a workbook, {'cell'} in a CSV, {'line'} in text."""
    kind = path.suffix.lower()
    if kind not in SHEETS | TEXT | {'.csv', '.docx'}:
        return {}, {}
    wanted = {g: set(group) for g, group in enumerate(groups) if group}
    patterns = {
        g: re.compile(
            r'(?<![\w.,])(?:' + '|'.join(re.escape(f) for f in group) + r')(?![\w]|[.,]\d)', re.IGNORECASE
        )
        for g, group in wanted.items()
    }
    places: dict[int, str] = {}
    spots: dict[int, dict] = {}

    def look(value, place: str, spot: dict) -> None:
        if value is None or value == '' or isinstance(value, bool):
            return
        numbers = set(_forms(str(value))) if isinstance(value, (int, float)) else set()
        text = str(_cell(value))
        for g in [g for g in wanted if g not in places]:
            if (numbers and numbers & wanted[g]) or (not numbers and patterns[g].search(text)):
                places[g] = place
                spots[g] = spot

    if kind in SHEETS:
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            for ws in book.worksheets:
                for r, cells in enumerate(ws.iter_rows(max_row=MAX_SEARCH_ROWS, values_only=True), 1):
                    for c, value in enumerate(cells, 1):
                        look(value, f'{ws.title}, row {r}', _spot(r, c, ws.title))
                    if len(places) == len(wanted):
                        return places, spots
        finally:
            book.close()
    elif kind == '.csv':
        for r, cells in enumerate(csv.reader(io.StringIO(_text(path)[:MAX_CHARS])), 1):
            if r > MAX_SEARCH_ROWS:
                break
            for c, value in enumerate(cells, 1):
                look(value, f'row {r}', _spot(r, c))
    else:
        for n, line in enumerate(text_of(path)[:MAX_CHARS].split('\n')[:MAX_SEARCH_ROWS], 1):
            look(line, f'line {n}', {'line': n})
    return places, spots


def locate_values(root: Path, relative: str, values: list[str]) -> dict:
    """Where a row's values show up in one of its sources. `wanted` is every way of writing them that is looked
    for, so the page can outline them in a sheet or text file too. `values` says where in the file each value given
    was first found ("page 2", "Employees, row 4", "line 7"), so the page can say which value came from which file.
    For a PDF, also the page where most of them appear and the boxes around them there; `page` is None when none is
    found, or for another kind of file. A picture is not searched."""
    forms: dict[str, set[int]] = {}  # each way of writing a value, and which values it stands for
    groups = list(dict.fromkeys(tuple(_forms(v)) for v in dict.fromkeys(values)))
    for g, group in enumerate(groups):
        for form in group:
            forms.setdefault(form, set()).add(g)
    needles = list(forms)[:MAX_FORMS]
    found = {'page': None, 'boxes': [], 'wanted': needles, 'values': {}, 'spots': {}}
    if not needles:
        return found
    path = job_file(root, relative)
    kind = path.suffix.lower()

    def named(places: dict[int, str]) -> dict[str, str]:
        return {v: places[g] for v in dict.fromkeys(values) if (g := groups.index(tuple(_forms(v)))) in places}

    if kind != '.pdf':
        places, spots = _where_values(path, groups)
        return {**found, 'values': named(places), 'spots': named(spots)}
    hits, _, _ = find_in_pdf(root, relative, needles, whole_words=True)
    pages: dict[int, set[int]] = {}
    first: dict[int, int] = {}  # the first page each value is on
    for hit in sorted(hits, key=lambda h: h['page']):
        for g in forms[needles[hit['needle']]]:
            pages.setdefault(hit['page'], set()).add(g)
            first.setdefault(g, hit['page'])
    if not pages:
        return found
    found['values'] = named({g: f'page {p}' for g, p in first.items()})
    found['spots'] = named({g: {'page': p} for g, p in first.items()})
    page = max(pages, key=lambda p: (len(pages[p]), -p))
    boxes = list(
        {tuple(round(h[k], 5) for k in 'xywh'): {k: h[k] for k in 'xywh'} for h in hits if h['page'] == page}.values()
    )

    # A short form found inside a longer one (12,960 inside 12,960.00) is the same place: keep the outer box.
    def inside(a: dict, b: dict) -> bool:
        edge = 1e-4
        return (
            a is not b
            and b['x'] <= a['x'] + edge
            and b['y'] <= a['y'] + edge
            and a['x'] + a['w'] <= b['x'] + b['w'] + edge
            and a['y'] + a['h'] <= b['y'] + b['h'] + edge
        )

    boxes = [a for a in boxes if not any(inside(a, b) for b in boxes)]
    return {**found, 'page': page, 'boxes': boxes[:50], 'found': len(pages[page]), 'of': sum(1 for g in groups if g)}
