"""Cells and files named in what the agent writes, found so the screen can turn them into links to that spot.

The agent is asked to name cells as Sheet!B4 (or 'Sheet name'!B4, or a block such as Sheet!B4:D9) and files by their
name. Nothing it writes is trusted: a name becomes a link only when that sheet is in exactly one of the run's
workbooks and the cell is inside the sheet, or when the file is one of the run's own. Anything else stays plain text.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path, PurePosixPath

from openpyxl import load_workbook
from openpyxl.utils.cell import column_index_from_string, coordinate_from_string

from . import preview

CELL = re.compile(
    r"(?:'(?P<quoted>[^'\n]{1,100})'|(?P<plain>[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}))!"
    r'(?P<cell>\$?[A-Z]{1,3}\$?[1-9]\d{0,6}(?::\$?[A-Z]{1,3}\$?[1-9]\d{0,6})?)\b'
)
WORKBOOKS = ('.xlsx', '.xlsm')
MAX_TEXT = 20_000  # characters looked at in one text


@functools.lru_cache(maxsize=32)
def _sheets(path: str, modified: int, size: int) -> dict[str, tuple[int, int]]:
    """Each sheet of a workbook with its used size (rows, columns). Kept while the file is unchanged."""
    book = load_workbook(path, read_only=True)
    try:
        return {ws.title: (ws.max_row or 0, ws.max_column or 0) for ws in book.worksheets}
    finally:
        book.close()


def _sheets_of(root: Path, relative: str) -> dict[str, tuple[int, int]]:
    try:
        path = preview.job_file(root, relative)
        info = path.stat()
        return _sheets(str(path), info.st_mtime_ns, info.st_size)
    except Exception:  # noqa: BLE001 - a file that cannot be read gives no links, never an error
        return {}


def _inside(cell: str, size: tuple[int, int]) -> bool:
    rows, columns = size
    for part in cell.replace('$', '').split(':'):
        try:
            letters, row = coordinate_from_string(part)
            column = column_index_from_string(letters)
        except ValueError:  # openpyxl's address errors are ValueErrors: no such cell, so no link
            return False
        if not 1 <= row <= max(rows, 1) or column > max(columns, 1):
            return False
    return True


def find(root: Path, paths: list[str], text: str) -> list[dict]:
    """The links in one text, in order: {"start", "end", "path"} for a file, plus "sheet" and "cell" for a cell.
    paths are the run's files as the app lists them ("inputs/...", "outputs/...")."""
    text = (text or '')[:MAX_TEXT]
    found: list[dict] = []
    books = {p: _sheets_of(root, p) for p in paths if p.startswith('outputs/') and p.lower().endswith(WORKBOOKS)}
    for match in CELL.finditer(text):
        sheet, cell = match['quoted'] or match['plain'], match['cell']
        holders = [p for p, sheets in books.items() if sheet in sheets]
        if len(holders) == 1 and _inside(cell, books[holders[0]][sheet]):
            found.append({'start': match.start(), 'end': match.end(), 'path': holders[0], 'sheet': sheet,
                          'cell': cell.replace('$', '')})  # fmt: skip
    # Files by their path in the packet or their name alone; a name two files share links to neither.
    names: dict[str, list[str]] = {}
    for path in paths:
        inner = path.split('/', 1)[1] if '/' in path else path
        for name in {inner, PurePosixPath(inner).name}:
            names.setdefault(name, []).append(path)
    taken = [(f['start'], f['end']) for f in found]
    for name in sorted(names, key=len, reverse=True):  # the longest first, so a path wins over its own name
        if len(names[name]) != 1 or len(name) < 3:
            continue
        for match in re.finditer(r'(?<![\w/.-])' + re.escape(name) + r'(?![\w/-]|\.\w)', text):
            if any(match.start() < end and start < match.end() for start, end in taken):
                continue
            taken.append((match.start(), match.end()))
            found.append({'start': match.start(), 'end': match.end(), 'path': names[name][0]})
    return sorted(found, key=lambda f: f['start'])
