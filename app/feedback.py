"""Comments a user pins to exact spots on a job's files, sent to the agent together as one round.

The browser says only where each comment points (a cell, a place on a page, a line) and what it says. Everything
the agent then sees is built here from the files themselves: what is at that spot now, and for pages and images a
picture with numbered pins. Each round keeps a folder under <job>/feedback/round-NN/:

  round.json   the comments, where each points, and the exact message the agent received
  pins/        the pictures sent with the message
  before/      the outputs as they were when the comments were made
  report.json  what changed where the comments asked, and what else changed since

The copy in before/ is what makes the report possible: after the agent saves, its outputs are compared with it.
A round is "saving" while it is being written, "sent" once queued for the agent, "delivered" when a run has read it,
and "done" when that run finished its work and the final report was kept.
"""

from __future__ import annotations

import csv
import difflib
import functools
import hashlib
import io
import json
import re
import shutil
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import column_index_from_string, coordinate_from_string

from . import preview
from .cleaning import now
from .engine.checks import DOCUMENT_MAP, PROVENANCE
from .files import within

MAX_COMMENTS = 50
MAX_TEXT = 2000
MAX_PICTURES = 8  # pages or images with pins in one round; each is sent to the model on every later turn
MAX_IMAGE_PIXELS = 40_000_000  # a picture larger than this is refused before it is opened in full
SHOWN = 200  # characters of a cell or line quoted back to the agent
LISTED = 30  # changes outside the commented spots that are listed one by one
CELL = re.compile(r'[A-Z]{1,3}[1-9][0-9]{0,6}')
CELLS = re.compile(rf'({CELL.pattern})(?::({CELL.pattern}))?')  # one cell, or a block such as C3:E5
MAX_BLOCK = 1000  # cells in one commented block
PIN_COLOUR = (47, 111, 219)
CAPTION = 44  # pixels of white above each picture, holding its name
NOT_COMPARED = ('processing_summary.txt',)  # written by the app when a run ends, not by the agent
# The agent's own records of its work. It is meant to update them whenever it changes anything, so a change to one
# is reported only when a comment pointed at it, never as something that changed elsewhere.
RECORDS = (DOCUMENT_MAP, PROVENANCE, 'notes.md')


def _short(value) -> str:
    text = '' if value is None else str(preview._cell(value))
    return text if len(text) <= SHOWN else text[:SHOWN] + '…'


def _quoted(text) -> str:
    """Text from a file or a person as one quoted line, so nothing in it can pass for another comment."""
    return json.dumps('' if text is None else str(text), ensure_ascii=False)


def _row_column(cell: str) -> tuple[int, int]:
    letters, row = coordinate_from_string(cell)
    return row, column_index_from_string(letters)


def _block(cell: str) -> list[tuple[int, int]]:
    """Every (row, column) of a commented cell or block, row by row."""
    first, _, last = cell.partition(':')
    (r1, c1), (r2, c2) = _row_column(first), _row_column(last or first)
    return [(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]


def _cells_spot(spot: dict) -> str:
    """The commented cell (D4) or block (C3:E5), checked and written top-left to bottom-right."""
    m = CELLS.fullmatch(str(spot.get('cell') or '').upper())
    if not m:
        raise ValueError('a comment on a sheet needs a cell such as D4, or a block such as C3:E5')
    (r1, c1), (r2, c2) = _row_column(m.group(1)), _row_column(m.group(2) or m.group(1))
    top, bottom, left, right = min(r1, r2), max(r1, r2), min(c1, c2), max(c1, c2)
    if (bottom - top + 1) * (right - left + 1) > MAX_BLOCK:
        raise ValueError(f'a comment can cover at most {MAX_BLOCK} cells')
    first = f'{get_column_letter(left)}{top}'
    return first if (top, left) == (bottom, right) else f'{first}:{get_column_letter(right)}{bottom}'


def _block_text(cell: str, value_at) -> str:
    """What a cell holds, or for a block its filled cells as "C3: 8450.5; D3: 676.04", cut short."""
    if ':' not in cell:
        return _short(value_at(_row_column(cell)))
    filled = [(f'{get_column_letter(c)}{r}', value_at((r, c))) for r, c in _block(cell)]
    text = '; '.join(f'{at}: {preview._cell(v)}' for at, v in filled if v not in (None, ''))
    return text if len(text) <= SHOWN * 3 else text[: SHOWN * 3] + '…'


def _spot_on_page(spot: dict) -> tuple[float, float, float, float]:
    """Where on a page or picture a comment points, as shares of its width and height: a point (no size), or the
    box the person drew from x, y across w and down h."""
    x, y, w, h = spot.get('x'), spot.get('y'), spot.get('w') or 0, spot.get('h') or 0
    if not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in (x, y, w, h)) or x + w > 1.0001 or y + h > 1.0001:
        raise ValueError('a comment on a page or picture needs a position inside it')
    return float(x), float(y), float(w), float(h)


def _page_words(x: float, y: float, w: float, h: float) -> str:
    if w or h:
        return f'the area from about {x:.0%} to {x + w:.0%} across and {y:.0%} to {y + h:.0%} down'
    return f'about {x:.0%} across and {y:.0%} down'


def locate(root: Path, spots: list[dict]) -> list[dict]:
    """Check where each comment points and read what is there now. Returns one entry per comment: path, kind
    ('cell', 'page', 'image' or 'line'), the spot's own fields, "where" in words and "now", the current content.
    Raises ValueError naming the first comment whose spot does not exist."""
    found = []
    for number, spot in enumerate(spots, 1):
        try:
            found.append(_locate_one(root, spot))
        except ValueError as error:
            raise ValueError(f'Comment {number}: {error}') from None
        except Exception:  # noqa: BLE001 - a damaged file is the user's to know about, not a crash
            raise ValueError(f'Comment {number}: the file could not be read at that spot') from None
    # Workbook cells are read in one pass per sheet: a sheet opened for reading is walked from the top each time.
    wanted: dict[tuple[str, str], list[dict]] = {}
    for entry in found:
        if entry['kind'] == 'cell' and 'sheet' in entry:
            wanted.setdefault((entry['path'], entry['sheet']), []).append(entry)
    for (relative, sheet), entries in wanted.items():
        values = _read_cells(preview.job_file(root, relative), sheet, {at for e in entries for at in _block(e['cell'])})
        for entry in entries:
            entry['now'] = _block_text(entry['cell'], values.get)
    return found


def _read_cells(path: Path, sheet: str, cells: set[tuple[int, int]]) -> dict[tuple[int, int], object]:
    """The values of some cells of one sheet. A formula a program wrote has no stored result, so its text stands
    in, as in the preview."""
    rows = {r for r, _ in cells}
    box = {'min_row': min(rows), 'max_row': max(rows), 'values_only': True}
    found: dict[tuple[int, int], object] = {}
    for data_only in (False, True):  # the stored results, read second, win where there are any
        book = load_workbook(path, read_only=True, data_only=data_only)
        try:
            for r, row in enumerate(book[sheet].iter_rows(**box), box['min_row']):
                for c, value in enumerate(row, 1):
                    if (r, c) in cells and value is not None:
                        found[(r, c)] = value
        finally:
            book.close()
    return found


def _locate_one(root: Path, spot: dict) -> dict:
    relative = str(spot.get('path') or '')
    try:
        path = preview.job_file(root, relative)
    except (ValueError, FileNotFoundError):
        raise ValueError("the file is not one of this run's files") from None
    kind = path.suffix.lower()
    if kind in preview.SHEETS or kind == '.csv':
        cell = _cells_spot(spot)
        named = f'cells {cell}' if ':' in cell else f'cell {cell}'
        if kind == '.csv':
            rows = list(csv.reader(io.StringIO(preview._text(path))))
            now = _block_text(cell, lambda at: _csv_value(rows, at))
            return {'path': relative, 'kind': 'cell', 'cell': cell, 'where': named, 'now': now}
        book = load_workbook(path, read_only=True)
        try:
            names = book.sheetnames
        finally:
            book.close()
        sheet = spot.get('sheet') or names[0]
        if sheet not in names:
            raise ValueError(f'there is no sheet named "{sheet}"')
        return {'path': relative, 'kind': 'cell', 'sheet': sheet, 'cell': cell, 'where': f'sheet "{sheet}", {named}', 'now': ''}  # fmt: skip
    if kind == '.pdf':
        import pypdfium2 as pdfium

        x, y, w, h = _spot_on_page(spot)
        page = spot.get('page')
        with preview.PDF_LOCK, pdfium.PdfDocument(path) as document:
            pages = len(document)
        if not isinstance(page, int) or not 1 <= page <= pages:
            raise ValueError(f'the file has {pages} page(s)')
        where = f'page {page}, {_page_words(x, y, w, h)}'
        return {'path': relative, 'kind': 'page', 'page': page, 'x': x, 'y': y, 'w': w, 'h': h, 'where': where, 'now': None}  # fmt: skip
    if kind in preview.IMAGES:
        from PIL import Image

        x, y, w, h = _spot_on_page(spot)
        with Image.open(path) as image:  # reads the size only; nothing is unpacked yet
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError('the picture is too large to comment on')
        where = f'{_page_words(x, y, w, h)} the picture'
        return {'path': relative, 'kind': 'image', 'x': x, 'y': y, 'w': w, 'h': h, 'where': where, 'now': None}
    if kind in preview.TEXT or kind == '.docx':
        lines = (preview._docx(path) if kind == '.docx' else preview._text(path)).split('\n')
        line = spot.get('line')
        if not isinstance(line, int) or not 1 <= line <= len(lines):
            raise ValueError(f'the file has {len(lines)} line(s)')
        return {'path': relative, 'kind': 'line', 'line': line, 'where': f'line {line}', 'now': _short(lines[line - 1])}
    raise ValueError('this kind of file cannot take a comment')


def pin_pictures(root: Path, number: int, entries: list[dict]) -> dict[str, bytes]:
    """One picture per page or image that has comments in round `number`: the page under a line that names the
    picture and what it shows, with each comment's number drawn where it points. Sets each entry's "picture"."""
    from PIL import Image, ImageDraw, ImageFont

    grouped: dict[tuple, list[dict]] = {}
    for entry in entries:
        if entry['kind'] in ('page', 'image'):
            grouped.setdefault((entry['path'], entry.get('page')), []).append(entry)
    if len(grouped) > MAX_PICTURES:
        raise ValueError(f'These comments are on {len(grouped)} different pages or pictures. Send at most {MAX_PICTURES} at a time.')  # fmt: skip
    made = {}
    for index, ((relative, page), pins) in enumerate(grouped.items(), 1):
        name = f'round-{number:02d}-picture-{index:02d}.png'
        if page is not None:
            image = Image.open(io.BytesIO(preview.page_png(root, relative, page)))
        else:
            image = Image.open(preview.job_file(root, relative))
        image.thumbnail((preview.PAGE_PIXELS, preview.PAGE_PIXELS))
        sheet = Image.new('RGB', (image.width, image.height + CAPTION), 'white')
        sheet.paste(image.convert('RGB'), (0, CAPTION))
        draw = ImageDraw.Draw(sheet)
        caption = f'{name}: {relative}' + (f', page {page}' if page is not None else '')
        draw.text((8, CAPTION // 2), caption, fill='black', font=ImageFont.load_default(size=18), anchor='lm')
        radius = max(14, image.width // 60)
        font = ImageFont.load_default(size=radius * 1.3)
        for pin in pins:
            x, y = pin['x'] * image.width, pin['y'] * image.height + CAPTION
            if pin.get('w') or pin.get('h'):  # a drawn box: its outline, with the number on its top-left corner
                box = (x, y, x + pin['w'] * image.width, y + pin['h'] * image.height)
                draw.rectangle(box, outline=PIN_COLOUR, width=max(3, radius // 4))
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=PIN_COLOUR, outline='white', width=3)
            draw.text((x, y), str(pin['n']), fill='white', font=font, anchor='mm')
            pin['picture'] = name
        out = io.BytesIO()
        sheet.save(out, format='PNG')
        made[name] = out.getvalue()
    return made


def round_message(number: int, entries: list[dict], lessons: bool = False) -> str:
    """The comments as the agent reads them: one line each, with its file, its spot, what is there now and what the
    user wrote. What comes from a file or from the user is quoted, so it can only ever be read as content."""
    lines = [
        f'Round {number} of comments from the user, each at an exact spot in a file. Make the change each comment '
        'calls for, and whatever follows from it, such as a total; leave everything else as it is. A comment that '
        'speaks of every row, or states a rule, reaches every place the same thing occurs, not only its pin. After you save, '
        'save_outputs also returns comment_changes: which of these spots changed, and what else changed since these '
        'comments were made.'
    ]
    if lessons:
        lines[0] += (
            ' A comment can also show a lasting rule about how this user wants this kind of work done, one that '
            'would apply to later jobs as well. When it does, call remember with the comment and the rule, worded '
            'so that someone doing a different job of this kind could follow it, with the exact form wanted and an '
            'example where the comment gives one. A fix that is only about this '
            'file is not a lesson, and most comments are just fixes.'
        )
    if any(entry.get('picture') for entry in entries):
        lines[0] += (
            ' The pictures that come with this message show where the pins are. They are for you to look at; they '
            'are not files of this job, so leave them out of the outputs.'
        )
    for entry in entries:
        parts = [f'{number}.{entry["n"]} file: {entry["path"]}', f'spot: {entry["where"]}']
        if entry.get('picture'):
            parts.append(f'pin {entry["n"]} on picture {entry["picture"]}')
        if entry['now'] is not None:
            parts.append(f'there now: {_quoted(entry["now"])}')
        parts.append(f'comment: {_quoted(entry["text"])}')
        lines.append(' | '.join(parts))
    return '\n'.join(lines)


def _folder(root: Path, number: int) -> Path:
    return root / 'feedback' / f'round-{number:02d}'


def _compared(folder: Path) -> dict[str, Path]:
    """The files of an outputs folder (or a copy of one) that the agent wrote, by their path inside it."""
    if not folder.is_dir():
        return {}
    files = {p.relative_to(folder).as_posix(): p for p in folder.rglob('*') if p.is_file()}
    return {name: p for name, p in sorted(files.items()) if name not in NOT_COMPARED and not name.endswith('.pending')}


def create_round(store, job_id: str, round_id: str, comments: list[dict]) -> dict:
    """Save one round of comments and queue it for the agent. The same round_id sent twice is one round: the
    second call returns the first with "duplicate" set. Raises ValueError, with words for the screen, when a
    comment's spot does not exist, the round cannot be stored, or a remembered comment cannot be added to the
    playbook; nothing is queued then and nothing is left behind."""
    root = store.path(job_id)
    entries = [
        {'n': n, **spot, 'text': comment['text'].strip(), 'remember': bool(comment.get('remember'))}
        for n, (spot, comment) in enumerate(zip(locate(root, comments), comments, strict=True), 1)
    ]
    claim = {}

    def reserve(job):
        rounds = job.setdefault('feedback', [])
        claim['duplicate'] = next((r for r in rounds if r['id'] == round_id), None)
        if not claim['duplicate']:
            # Numbers are never reused, so a round that failed can never hand its folder to the next one.
            claim['number'] = job['feedback_rounds'] = job.get('feedback_rounds', 0) + 1
            rounds.append({'id': round_id, 'number': claim['number'], 'status': 'saving', 'comments': len(entries), 'created_at': now()})  # fmt: skip

    job = store.mutate(job_id, reserve)
    if claim['duplicate']:
        return {**claim['duplicate'], 'duplicate': True}
    number, folder = claim['number'], _folder(root, claim['number'])
    try:
        (folder / 'before').mkdir(parents=True)
        for name, source in _compared(root / 'outputs').items():
            (folder / 'before' / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, folder / 'before' / name)
        pictures = pin_pictures(root, number, entries)
        for name, data in pictures.items():
            (folder / 'pins').mkdir(exist_ok=True)
            (folder / 'pins' / name).write_bytes(data)
        # A job with a playbook can keep a lesson (app/lessons.py); the message says so only then.
        message = round_message(number, entries, lessons=bool((job.get('playbook') or {}).get('id')))
        record = {'id': round_id, 'number': number, 'created_at': now(), 'comments': entries, 'message': message,
                  'pictures': sorted(pictures), 'remembered_version': None}  # fmt: skip
        (folder / 'round.json').write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')
        # Last, because it is the one step that reaches outside this job.
        record['remembered_version'] = version = _remember(store, job, round_id, number, entries)
        (folder / 'round.json').write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')
    except Exception as error:
        _drop(store, job_id, round_id, folder)
        if isinstance(error, ValueError):
            raise
        raise ValueError(f'The comments could not be saved ({type(error).__name__}). Try again in a moment.') from error
    images = [{'name': name, 'path': f'feedback/round-{number:02d}/pins/{name}'} for name in sorted(pictures)]
    sent = {}

    def send(job):
        entry = next(r for r in job['feedback'] if r['id'] == round_id)
        entry.update(status='sent', remembered_version=version)
        job['pending_messages'] = [*(job.get('pending_messages') or []), {'text': message, 'round': number, 'images': images}]  # fmt: skip
        sent.update(entry)

    store.mutate(job_id, send)
    store.event(
        job_id, 'feedback.sent', f'{len(entries)} comment{"" if len(entries) == 1 else "s"} sent to the agent',
        type='user', round=number, round_id=round_id, remembered_version=version,
        comments=[{k: e.get(k) for k in ('n', 'path', 'where', 'text', 'remember')} for e in entries],
    )  # fmt: skip
    return sent


def _drop(store, job_id: str, round_id: str, folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)
    store.mutate(job_id, lambda j: j.update(feedback=[r for r in j.get('feedback') or [] if r['id'] != round_id]))


def recover(store, job: dict) -> None:
    """Called when the app starts: a round still "saving" was cut off by the last shutdown and never reached the
    agent. Remove it, so the browser's retry of the same round is taken as new."""
    for entry in job.get('feedback') or []:
        if entry['status'] == 'saving':
            _drop(store, job['id'], entry['id'], _folder(store.path(job['id']), entry['number']))
            store.event(job['id'], 'feedback.dropped', 'Comments that were being saved when the app stopped were not sent. Send them again.', type='job', level='WARN', round=entry['number'])  # fmt: skip


def _remember(store, job: dict, round_id: str, number: int, entries: list[dict]) -> int | None:
    """Add the comments marked "remember" to the job's playbook as one new version; returns its number. A round
    remembered once is never added twice."""
    lines = [e['text'] for e in entries if e['remember']]
    if not lines:
        return None
    playbook_id = (job.get('playbook') or {}).get('id') or job.get('module') or ''
    detail = {'job_id': job['id'], 'round': number, 'round_id': round_id}
    # Found again by the job and the round's own id, not its number: a round cut off after this saved and sent
    # again is given a new number, and must still not add the same instructions twice.
    same = lambda d: (d.get('job_id'), d.get('round_id')) == (job['id'], round_id)  # noqa: E731
    try:
        earlier = next((v for v in store.playbooks.versions(playbook_id) if same(v['detail'])), None)
        if earlier:
            return earlier['version']
        saved = store.playbooks.save(
            playbook_id, '\n\n'.join(lines), 'remembered', ' '.join(lines), append=True, **detail
        )
    except KeyError:
        raise ValueError("This run's playbook no longer exists, so a comment cannot be remembered") from None
    return saved['version']


def rounds(store, job_id: str) -> list[dict]:
    """Every round of a job, oldest first, with its comments, its status and its change report once there is one."""
    root, found = store.path(job_id), []
    for entry in store.get(job_id).get('feedback') or []:
        folder = _folder(root, entry['number'])
        try:
            record = json.loads((folder / 'round.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue  # still being saved
        report = folder / 'report.json'
        learned = {k: entry[k] for k in ('learned_version', 'learned') if k in entry}  # see app/lessons.py
        found.append({**record, **learned, 'status': entry['status'], 'report': json.loads(report.read_text(encoding='utf-8')) if report.exists() else None})  # fmt: skip
    return found


def open_pictures(store, job_id: str) -> list[dict]:
    """The pictures of every round a run has read but not finished acting on, for a run that continues in a fresh
    sandbox: the round's text is in its resume card, and the pictures it names must come with it."""
    root, found = store.path(job_id), []
    for entry in rounds(store, job_id):
        if entry['status'] == 'delivered':
            pins = _folder(root, entry['number']) / 'pins'
            found += [{'name': name, 'data': (pins / name).read_bytes()} for name in entry['pictures']]
    return found


@functools.lru_cache(maxsize=4)
def _cells_of(path: str, modified: int, size: int) -> dict[tuple[str, str], object]:
    """Every filled cell of a workbook as typed (formulas as text), keyed by sheet and cell. Kept for the files it
    was last asked about: a round's before-copy never changes, and is compared again after every save."""
    book, cells = load_workbook(path, read_only=True, data_only=False), {}
    try:
        for sheet in book.worksheets:
            for r, row in enumerate(sheet.iter_rows(values_only=True), 1):
                for c, value in enumerate(row, 1):
                    if value not in (None, ''):
                        cells[(sheet.title, f'{get_column_letter(c)}{r}')] = preview._cell(value)
    finally:
        book.close()
    return cells


def _cells(path: Path) -> dict[tuple[str, str], object]:
    stat = path.stat()
    return _cells_of(str(path), stat.st_mtime_ns, stat.st_size)


def _digest(path: Path | None) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path else None


def _differences(before: Path, outputs: Path) -> dict[str, dict | None]:
    """What differs between a before-copy and the outputs now, by file. A workbook gives its changed cells,
    {(sheet, cell): (was, is now)}; any other file, or one added or removed, gives None."""
    old, new, found = _compared(before), _compared(outputs), {}
    for name in sorted(old.keys() | new.keys()):
        if _digest(old.get(name)) == _digest(new.get(name)):
            continue
        if Path(name).suffix.lower() in preview.SHEETS and name in old and name in new:
            was, is_now = _cells(old[name]), _cells(new[name])
            found[name] = {k: (was.get(k), is_now.get(k)) for k in was.keys() | is_now.keys() if was.get(k) != is_now.get(k)}  # fmt: skip
        else:
            found[name] = None
    return found


def _at_spot(path: Path, comment: dict) -> str | None:
    """What a text file holds on a commented line, or a CSV in a commented cell. None when the file is of another
    kind or is not there."""
    kind = path.suffix.lower()
    if not path.is_file():
        return None
    if comment['kind'] == 'line' and kind in preview.TEXT:
        lines = preview._text(path).split('\n')
        return lines[comment['line'] - 1] if comment['line'] <= len(lines) else ''
    if comment['kind'] == 'cell' and kind == '.csv':
        rows = list(csv.reader(io.StringIO(preview._text(path))))
        return '\t'.join(_csv_value(rows, at) or '' for at in _block(comment['cell']))
    return None


def _csv_value(rows: list[list[str]], at: tuple[int, int]) -> str | None:
    row, column = at
    return rows[row - 1][column - 1] if row <= len(rows) and column <= len(rows[row - 1]) else None


def _spot_text(before: Path, after: Path, comment: dict) -> tuple[str, str] | None:
    was, is_now = _at_spot(before, comment), _at_spot(after, comment)
    return None if was is None or is_now is None else (was, is_now)


def _change(pair) -> str:
    return f'{_quoted(_short(pair[0]))} -> {_quoted(_short(pair[1]))}'


def changes_since(store, job_id: str, relative: str, sheet: str | None = None) -> dict:
    """What the agent changed in one output since the last round of comments, as the preview shows it: for a sheet,
    each changed cell with what it showed before ({'value', 'text'}); for a text file, each changed line with the line
    it replaced (None for a new line), and under 'removed' the lines taken out, each after the line it followed (0 for
    the top). 'round' is that round's number, None when no round has been sent. 'new' says the file, or the sheet,
    was not there then. Files the preview shows as pictures or pages are not compared: 'cells' and 'lines' are
    both missing."""
    root = store.path(job_id)
    current = preview.job_file(root, relative)
    folder, _, name = relative.partition('/')
    entries = store.get(job_id).get('feedback') or [] if folder == 'outputs' else []
    before = next((b for e in reversed(entries) if (b := _folder(root, e['number']) / 'before').is_dir()), None)
    if before is None:
        return {'round': None}
    number = int(before.parent.name.removeprefix('round-'))
    try:
        old = within(before, name)
    except ValueError:
        old = None
    if old is None or not old.is_file():
        return {'round': number, 'new': True}
    kind = current.suffix.lower()
    if kind in preview.SHEETS or kind == '.csv':
        now = preview._sheet(current, sheet) if kind != '.csv' else preview._csv(current)
        was = preview._sheet(old, now['sheet']) if kind != '.csv' else preview._csv(old)
        if was['sheet'] != now['sheet']:  # a sheet added since; the preview fell back to another one
            return {'round': number, 'new': True}
        return {'round': number, 'cells': _changed_cells(was, now)}
    if kind in preview.TEXT or kind == '.docx':
        return {'round': number, **_changed_lines(preview.text_of(old), preview.text_of(current))}
    return {'round': number}


def _shown_cells(body: dict) -> dict[str, tuple]:
    """Each filled cell of a sheet preview as (value, the text Excel shows), keyed by its address."""
    formats, found = body.get('formats') or {}, {}
    for r, row in enumerate(body['rows'], 1):
        for c, value in enumerate(row, 1):
            at = f'{get_column_letter(c)}{r}'
            text = formats.get(at, {}).get('text')
            if value not in (None, '') or text:
                found[at] = (value, text)
    return found


def _changed_cells(was: dict, now: dict) -> dict[str, dict]:
    old, new = _shown_cells(was), _shown_cells(now)
    return {
        at: {'value': old.get(at, (None, None))[0], 'text': old.get(at, (None, None))[1]}
        for at in sorted(old.keys() | new.keys(), key=_row_column)
        if old.get(at) != new.get(at)
    }


MAX_MATCHED_LINES = 1000  # beyond this, lines are compared in place: matching them up is slow for long files


def _changed_lines(was: str, now: str) -> dict:
    """The lines of `now` that differ from `was`, with what they replaced, and the lines of `was` that are gone.
    Lines both files start and end with are set aside first, so a long file with a few edits is quick to compare."""
    old, new = was[: preview.MAX_CHARS].split('\n'), now[: preview.MAX_CHARS].split('\n')
    top = 0
    while top < min(len(old), len(new)) and old[top] == new[top]:
        top += 1
    tail = 0
    while tail < min(len(old), len(new)) - top and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    a, b = old[top : len(old) - tail], new[top : len(new) - tail]
    if max(len(a), len(b)) <= MAX_MATCHED_LINES:
        steps = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    else:  # line by line in place, then what is left over at the end
        shared = min(len(a), len(b))
        steps = [('replace' if a[i] != b[i] else 'equal', i, i + 1, i, i + 1) for i in range(shared)]
        steps.append(('replace', shared, len(a), shared, len(b)))
    lines: dict[str, str | None] = {}
    removed: list[dict] = []
    for tag, i1, i2, j1, j2 in steps:
        if tag == 'equal':
            continue
        for k, j in enumerate(range(j1, j2)):
            lines[str(top + j + 1)] = a[i1 + k] if i1 + k < i2 else None
        removed += [{'after': top + j2, 'text': line} for line in a[i1 + (j2 - j1) : i2]]
    return {'lines': lines, 'removed': removed}


def change_report(store, job_id: str) -> dict | None:
    """Compare the outputs now with the copies taken when comments were made, for the rounds a run has read and
    not yet finished with. Each comment is judged against its own round's copy: did its spot change since the
    comment was made? Everything else that changed is listed against the oldest of those copies. None when no
    round is open. The comparison is cell by cell, so rows inserted or removed show as changes below them."""
    root = store.path(job_id)
    open_rounds = [r for r in rounds(store, job_id) if r['status'] == 'delivered']
    if not open_rounds:
        return None
    outputs = root / 'outputs'
    changed, unchanged, touched, spots = [], [], set(), []
    for entry in open_rounds:
        before_folder = _folder(root, entry['number']) / 'before'
        differs = _differences(before_folder, outputs)
        for comment in entry['comments']:
            label = f'{entry["number"]}.{comment["n"]} {comment["path"]}, {comment["where"]}'
            before = len(changed)
            folder, _, name = comment['path'].partition('/')
            if folder != 'outputs':
                unchanged.append(f'{label}: a comment on a source file; there is nothing to compare')
            elif name not in differs:
                unchanged.append(f'{label}: the file is as it was')
            elif (pair := _spot_text(before_folder / name, outputs / name, comment)) is not None:
                # A line of text or a cell of a CSV: the file changed, and what is at the spot says whether it did.
                touched.add((name, None))
                if pair[0] != pair[1]:
                    changed.append(f'{label}: {_change(pair)}')
                else:
                    unchanged.append(f'{label}: the file changed, but what is at this spot is as it was')
            elif differs[name] is None or comment['kind'] != 'cell' or 'sheet' not in comment:
                touched.add((name, None))
                changed.append(f'{label}: the file changed')
            elif hits := [
                (comment['sheet'], f'{get_column_letter(c)}{r}')
                for r, c in _block(comment['cell'])
                if (comment['sheet'], f'{get_column_letter(c)}{r}') in differs[name]
            ]:
                touched.update((name, key) for key in hits)
                if ':' in comment['cell']:
                    shown = '; '.join(f'{key[1]} {_change(differs[name][key])}' for key in hits[:5])
                    changed.append(
                        f'{label}: {len(hits)} of its cells changed: {shown}' + ('; …' if len(hits) > 5 else '')
                    )
                else:
                    changed.append(f'{label}: {_change(differs[name][hits[0]])}')
            else:
                unchanged.append(
                    f'{label}: '
                    + (
                        "these cells' own content is as it was"
                        if ':' in comment['cell']
                        else "this cell's own content is as it was"
                    )
                )
            spots.append({'round': entry['number'], 'n': comment['n'], 'changed': len(changed) > before})
    elsewhere, total = [], 0
    for name, cells in _differences(_folder(root, open_rounds[0]['number']) / 'before', outputs).items():
        if cells is None:
            if (name, None) not in touched and name not in RECORDS:
                total += 1
                elsewhere.append(f'{name}: the file was added, removed or rewritten')
            continue
        for key in sorted(cells, key=lambda k: (k[0], *_row_column(k[1]))):
            if (name, key) not in touched:
                total += 1
                if len(elsewhere) < LISTED:
                    elsewhere.append(f'{name}, sheet "{key[0]}", cell {key[1]}: {_change(cells[key])}')
    return {
        'rounds': [r['number'] for r in open_rounds],
        'changed_where_asked': changed,
        'not_changed_where_asked': unchanged,
        'spots': spots,  # the same verdict per comment, for the screen to mark the spots that changed
        'changed_elsewhere': elsewhere,
        'changed_elsewhere_count': total,
        'note': f'Compared cell by cell with the files as they were saved when round {open_rounds[0]["number"]} was '
        f'sent ({open_rounds[0]["created_at"]}). Everything saved since then counts, whoever asked for it, except your own notes and records, which are meant to change. Rows '
        'inserted or removed shift every cell below them, which shows here as many changes.',
    }


def close_rounds(store, job_id: str) -> dict | None:
    """When a run has finished its work, keep the final change report beside each round it read and mark those
    rounds done; returns that report. A run that only stopped (paused, waiting, out of time) leaves them open for
    the run that continues."""
    report = change_report(store, job_id)
    if report is None:
        return None
    root = store.path(job_id)
    for number in report['rounds']:
        (_folder(root, number) / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')  # fmt: skip

    def done(job):
        for entry in job.get('feedback') or []:
            if entry['number'] in report['rounds']:
                entry['status'] = 'done'

    store.mutate(job_id, done)
    store.event(
        job_id, 'feedback.done', 'Comments checked against the saved files', type='job', rounds=report['rounds'],
        changed=len(report['changed_where_asked']), unchanged=len(report['not_changed_where_asked']),
        elsewhere=report['changed_elsewhere_count'],
    )  # fmt: skip
    return report
