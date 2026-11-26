"""The agent looks at its own output: a picture of a sheet drawn as Excel shows it (app/sheet_picture.py).

- look_at_output: the agent asks for a picture of one of its workbooks, as it is now in the sandbox
- before finishing: the first save that passes every check also shows the top of each deliverable and the rows
  around its totals, so the agent sees what the person receiving it will see before it hands it over (a Settings
  page switch)

Each picture costs the model roughly a thousand tokens or two to read; the run record notes the cost.
"""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path, PurePosixPath

from agents import ToolOutputImage, ToolOutputText
from openpyxl import load_workbook

from ..sheet_picture import draw, tokens
from .checks import RECORD_FILES, WORKBOOKS
from .totals import work_out

OUTPUT_DIR = '/mnt/data/outputs'
MAX_PICTURES = 4  # per call: each one travels in every later request of the run
BEFORE_FINISH_BOOKS = 2  # workbooks shown before finishing
AROUND_TOTAL = (8, 3)  # rows shown above and below the last total


def _url(png: bytes) -> str:
    return 'data:image/png;base64,' + base64.b64encode(png).decode()


def _outputs_name(file: str) -> str:
    """The file's path under the outputs folder, however the agent wrote it."""
    name = file.strip().removeprefix(OUTPUT_DIR).lstrip('/')
    if not name or '..' in PurePosixPath(name).parts:
        raise ValueError(f'"{file}" is not a file under {OUTPUT_DIR}/.')
    return name


def _items(path: Path, label: str, sheet: str | None, cells: str | None, room: int) -> tuple[list, int, list[str]]:
    """The pictures of one sheet as tool output items, with the text that goes with them."""
    picture = draw(path, sheet or None, cells or None, max_tiles=room)
    head = f'{label}, sheet "{picture.sheet}"'
    if picture.tiles:
        head += ' (' + ', '.join(picture.tiles) + ')'
    lines = [head + '.']
    if picture.notes:
        lines.append('Not shown: ' + ' '.join(picture.notes))
    items = [ToolOutputText(text=' '.join(lines))]
    items += [ToolOutputImage(image_url=_url(png), detail='high') for png in picture.images]
    return items, sum(tokens(png) for png in picture.images), picture.sheet


async def look(job, file: str, sheet: str = '', cells: str = '') -> list:
    """A picture of one output workbook as it is now in the sandbox, for the look_at_output tool."""
    name = _outputs_name(file)
    if PurePosixPath(name).suffix.lower() not in WORKBOOKS:
        return [ToolOutputText(text=f'{name} is not a workbook; only .xlsx and .xlsm files can be drawn.')]
    data = await job.main.download(f'{OUTPUT_DIR}/{name}')
    folder = job.store.dir / 'looks'
    local = folder / re.sub(r'[^A-Za-z0-9._-]+', '_', name)
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(local.write_bytes, data)
    try:
        items, cost, _ = await asyncio.to_thread(_items, local, name, sheet, cells, MAX_PICTURES)
        sheets = await asyncio.to_thread(_sheet_names, local)
    except ValueError as error:
        return [ToolOutputText(text=str(error))]
    pictures = len(items) - 1
    job.store.event('output.looked', file=name, sheet=sheet or None, cells=cells or None, pictures=pictures,
                    tokens=cost)  # fmt: skip
    items[0] = ToolOutputText(text=items[0].text + f' Sheets in this workbook: {", ".join(sheets)}.')
    return items


def _sheet_names(path: Path) -> list[str]:
    book = load_workbook(path, read_only=True)
    try:
        return [ws.title + ('' if ws.sheet_state == 'visible' else ' (hidden)') for ws in book.worksheets]
    finally:
        book.close()


def _last_total_row(path: Path, sheet: str) -> int | None:
    try:
        found = work_out(path)
    except Exception:  # noqa: BLE001 - no totals to show is not a reason to show nothing
        return None
    rows = [int(re.sub(r'\D', '', t['cell'])) for t in found.totals if t['sheet'] == sheet]
    return max(rows, default=None)


def _before_finish(outputs: Path, names: list[str]) -> tuple[list, int, list[str]]:
    """The top of each workbook's first visible sheet, and the rows around its last total when they are further
    down. Also returns the workbooks not shown."""
    items, cost, pictures, shown_books = [], 0, 0, set()
    for name in names[:BEFORE_FINISH_BOOKS]:
        path = outputs / name
        try:
            first, spent, sheet = _items(path, f'{name}, top', None, '1:30', 1)
            parts = [(first, spent)]
            last = _last_total_row(path, sheet)
            if last and last > 30:
                above, below = AROUND_TOTAL
                parts.append(_items(path, f'{name}, around its totals', sheet, f'{last - above}:{last + below}', 1)[:2])
        except Exception:  # noqa: BLE001 - a picture that cannot be drawn never fails the save
            continue
        for shown, spent in parts:
            if pictures + len(shown) - 1 > MAX_PICTURES:
                break
            items += shown
            cost += spent
            pictures += len(shown) - 1
            shown_books.add(name)
    return items, cost, [name for name in names if name not in shown_books]


async def before_finish(job, check: dict) -> list:
    """Pictures of the deliverables for the first save that passes every check, when the setting is on."""
    if not (job.look_before_finish and check.get('passed')) or job.looked_before_finish:
        return []
    job.looked_before_finish = True
    outputs = job.store.outputs
    if job.spec.deliverables:
        names = [d.name for d in job.spec.deliverables if (outputs / d.name).exists()]
    else:
        names = sorted(p.relative_to(outputs).as_posix() for p in outputs.rglob('*')
                       if p.is_file() and p.suffix.lower() in WORKBOOKS and p.name not in RECORD_FILES)  # fmt: skip
    items, cost, left = await asyncio.to_thread(_before_finish, outputs, names)
    pictures = sum(isinstance(i, ToolOutputImage) for i in items)
    if not pictures:
        return []
    job.store.event('outputs.pictured', pictures=pictures, tokens=cost)
    intro = ToolOutputText(
        text='The checks passed. Before you finish, look at these pictures of your deliverables as the person '
        'receiving them will see them in Excel: headers, widths (#### means a number does not fit its column), '
        'number and date formats, totals, borders. Fix what looks wrong, save again, and look at anything else '
        'with look_at_output.'
        + (f' Not shown here, so look at them with look_at_output: {", ".join(left)}.' if left else '')
    )
    return [intro, *items]
