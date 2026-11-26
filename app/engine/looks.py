"""Save warnings about how a workbook will look in Excel, found by code with no model ("Looks right").

- a number or date too wide for its column, which Excel shows as ####
- numbers or dates stored as text, which Excel cannot add up, sort or filter as numbers or dates
- a column of numbers with decimals and no number format (4200.5 rather than 4,200.50)
- rows holding data that are hidden

They name only cells the agent wrote: a filled template keeps its own look, so only the rows the agent filled count.
They are warnings, not problems: a save can pass with them, and the agent fixes them or says why in notes.md.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .. import cell_format, formula_calc
from ..sheet_picture import too_narrow, width_px
from .house_style import as_date, as_number

MAX_WARNINGS = 12
MAX_ROWS = 5000  # rows looked at per sheet


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _runs(numbers: list[int]) -> str:
    out, start = [], None
    for i, n in enumerate(numbers):
        start = n if start is None else start
        if i == len(numbers) - 1 or numbers[i + 1] != n + 1:
            out.append(str(start) if start == n else f'{start}-{n}')
            start = None
    return ', '.join(out)


def _types(ws, spec_sheet: dict | None) -> dict[int, str]:
    """Column number -> the type the format gives it, found by its heading."""
    if not spec_sheet:
        return {}
    by_name = {c['name']: c.get('type', 'text') for c in spec_sheet.get('columns', [])}
    header = int(spec_sheet.get('header_row', 1))
    return {c.column: by_name[str(c.value).strip()] for c in ws[header] if str(c.value or '').strip() in by_name}


def warnings(path: Path, rows: dict[str, set[int]], format_spec: dict | None = None) -> list[str]:
    """The look warnings for one workbook. rows: per sheet, the rows to look at (the rows the agent wrote)."""
    if not any(rows.values()):
        return []
    formulas = load_workbook(path)
    values = load_workbook(path, data_only=True)
    calc = formula_calc.Calculator(formulas, values)
    sheets = {s['name']: s for s in (format_spec or {}).get('sheets', [])}
    found = []
    try:
        for title, wanted in rows.items():
            if title not in formulas.sheetnames or not wanted:
                continue
            types = _types(formulas[title], sheets.get(title))
            found += _sheet(formulas[title], values[title], calc, sorted(wanted)[:MAX_ROWS], types)
    finally:
        formulas.close()
        values.close()
    more = len(found) - MAX_WARNINGS
    return found[:MAX_WARNINGS] + ([f'... and {more} more.'] if more > 0 else [])


def _sheet(ws, wv, calc, wanted: list[int], types: dict[int, str]) -> list[str]:  # noqa: C901 - one pass, four findings
    title = ws.title
    hashes, as_text, unformatted, hidden = [], defaultdict(list), defaultdict(list), []
    real_numbers = defaultdict(int)
    widths = {}
    merged = {(r, c) for block in ws.merged_cells.ranges for r, c in block.cells}  # once per sheet, not per cell
    held = defaultdict(list)  # the stored cells of each wanted row, read in one pass
    wanted_set = set(wanted)
    for cell in ws._cells.values():  # noqa: SLF001 - openpyxl has no public list of stored cells
        if cell.row in wanted_set and cell.value not in (None, ''):
            held[cell.row].append(cell)
    for r in wanted:
        dim = ws.row_dimensions.get(r)
        cells = sorted(held.get(r, ()), key=lambda cell: cell.column)
        if dim is not None and dim.hidden and cells:
            hidden.append(r)
        for cell in cells:
            c = cell.column
            value, _ = formula_calc.shown_value(wv.cell(r, c), cell.value, calc, title, r, c)
            kind = types.get(c)
            if _is_number(value):
                real_numbers[c] += 1
                if cell.number_format == 'General' and not float(value).is_integer():
                    unformatted[c].append(cell.coordinate)
            written_as_text = isinstance(value, str) and not str(cell.value).startswith('=')
            if written_as_text and kind != 'text' and (as_date(value) is not None or as_number(value) is not None):
                as_text[c].append((cell.coordinate, value))
            if _is_number(value) or isinstance(value, (datetime, date)):
                shown = cell_format.shown_text(value, cell.number_format)
                text = shown[0] if shown else (f'{value:.10g}' if isinstance(value, float) else str(value))
                if c not in widths:
                    widths[c] = width_px(ws, c)[0]
                size, bold = (cell.font.sz or 11, bool(cell.font.b)) if cell.font else (11, False)
                if (r, c) not in merged and too_narrow(text, size, bold, widths[c]):
                    hashes.append((cell.coordinate, text))
    out = []
    if hashes:
        columns = sorted({''.join(ch for ch in coord if ch.isalpha()) for coord, _ in hashes})
        coord, text = hashes[0]
        out.append(
            f'{title}: {len(hashes)} value(s) are too wide for column(s) {", ".join(columns)}, so Excel shows #### '
            f'(e.g. {coord} "{text}"). Widen the column.'
        )
    for c, items in sorted(as_text.items()):
        letter = get_column_letter(c)
        dates = [i for i in items if as_date(i[1]) is not None]
        numbers = [i for i in items if as_date(i[1]) is None]
        if numbers and not (types.get(c) in ('number', 'integer') or real_numbers[c]):
            numbers = []  # a code such as 2100 in a text column stays text
        for found, what, kind in ((numbers, 'numbers', 'numbers'), (dates, 'dates', 'real dates with a date format')):
            if found:
                out.append(
                    f'{title} column {letter}: {len(found)} {what} stored as text (e.g. {found[0][0]} '
                    f'"{found[0][1]}"). Write them as {kind}.'
                )
    for c, coords in sorted(unformatted.items()):
        out.append(
            f'{title} column {get_column_letter(c)}: numbers with decimals have no number format (e.g. {coords[0]}). '
            'Give the column one, such as #,##0.00.'
        )
    if hidden:
        out.append(f'{title}: rows {_runs(hidden)} hold data and are hidden. Show them, or say why in notes.md.')
    return out
