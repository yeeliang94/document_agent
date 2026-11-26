"""What a workbook's formulas come to, worked out on this machine at every save.

openpyxl, which the agent writes with, saves no formula results, so neither the agent nor the checks could see what a
total adds up to. This uses the calculator the file preview already uses (app/formula_calc.py) to list each total
with its value, and to find two faults a person would see at once in Excel:
- a formula that shows an error (#REF!, #DIV/0!, ...), unless the template already showed it there
- a total whose range stops short: a plain number sits between the range and the total, in the same column (or
  row), and the formula reads it nowhere else, unless the blank template's own total already did the same
A formula the calculator cannot work out is counted, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from itertools import islice
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .. import formula_calc

TOTAL_FUNCTIONS = {'SUM', 'SUBTOTAL', 'SUMIF', 'SUMIFS', 'AGGREGATE'}
MAX_FORMULAS = 20_000  # formula cells looked at per workbook; a bigger one is checked as far as this
MAX_LISTED = 30  # totals listed per workbook in the save reply


@dataclass
class Totals:
    totals: list[dict] = field(default_factory=list)  # {'sheet', 'cell', 'formula', 'value'}, in sheet order
    problems: list[str] = field(default_factory=list)
    errors: set = field(default_factory=set)  # (sheet, formula) of every cell showing an error
    short: set = field(default_factory=set)  # (sheet, formula) of every total that leaves a number out
    unsupported: int = 0  # formulas the calculator could not work out
    unsupported_at: list = field(default_factory=list)  # where they are, as Sheet!B4
    formulas: int = 0


def _ranges(node, found: list) -> None:
    """Every range read by a total function anywhere in a parsed formula."""
    if not isinstance(node, tuple):
        return
    if node[0] == 'call':
        for arg in node[2]:
            if node[1] in TOTAL_FUNCTIONS and arg[0] == 'range' and arg[4] is not None:
                found.append(arg)
            _ranges(arg, found)
    elif node[0] in ('op', 'neg', 'pct'):
        for part in node[1:]:
            _ranges(part, found)


def _reads(node, sheet: str, found: list) -> None:
    """Every cell or block a parsed formula reads, as (sheet, r1, c1, r2, c2)."""
    if not isinstance(node, tuple):
        return
    if node[0] == 'ref':
        found.append((node[1], node[2], node[3], node[2], node[3]))
    elif node[0] == 'range' and node[4] is not None:
        found.append(node[1:])
    elif node[0] == 'call':
        for arg in node[2]:
            _reads(arg, sheet, found)
    elif node[0] in ('op', 'neg', 'pct'):
        for part in node[1:]:
            _reads(part, sheet, found)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _shown(value) -> str:
    if _number(value):
        return f'{value:,.2f}' if isinstance(value, float) and not value.is_integer() else f'{value:,.0f}'
    return str(value)


class _Book:
    def __init__(self, path: Path):
        self.path = path
        self.formulas = load_workbook(path, read_only=True, data_only=False)
        self.values = load_workbook(path, read_only=True, data_only=True)
        self.calc = formula_calc.Calculator(self.formulas, self.values)
        self.texts: dict[tuple[str, int, int], str] = {}

    def close(self) -> None:
        self.formulas.close()
        self.values.close()

    def formula_cells(self):
        # Read from a copy of its own, with the sheets' stated size dropped, so only the rows the file holds are
        # read: one far-away cell carrying only formatting would otherwise mean millions of empty cells. The
        # calculator keeps the stated size, which whole-column ranges need.
        scan = load_workbook(self.path, read_only=True, data_only=False)
        try:
            for ws in scan.worksheets:
                ws.reset_dimensions()
                for row in ws.iter_rows():
                    for cell in row:
                        value = getattr(cell, 'value', None)
                        if isinstance(value, str) and value.startswith('=') and len(value) > 1:
                            self.texts[(ws.title, cell.row, cell.column)] = value
                            yield ws.title, cell.row, cell.column, value
        finally:
            scan.close()

    def is_formula(self, sheet: str, row: int, col: int) -> bool:
        return (sheet, row, col) in self.texts

    def value(self, sheet: str, row: int, col: int):
        try:
            return self.calc.cell(sheet, row, col)
        except (formula_calc.Unsupported, formula_calc.XlError, RecursionError):
            return None


def _left_out(book: _Book, sheet: str, row: int, col: int, rng: tuple, reads: list) -> str | None:
    """The first plain number sitting between a one-column (or one-row) total range and its total, that the formula
    reads nowhere else, or None. A row added below a table and left outside its total shows up this way; a heading or
    an opening balance beyond the range's far end does not, nor a subtotal (a formula) in between."""
    _, where, r1, c1, r2, c2 = rng
    if where != sheet:
        return None
    if c1 == c2 == col and not r1 <= row <= r2:  # a column total
        gap = [(r, col) for r in (range(r2 + 1, row) if row > r2 else range(row + 1, r1))]
    elif r1 == r2 == row and not c1 <= col <= c2:  # a row total
        gap = [(row, c) for c in (range(c2 + 1, col) if col > c2 else range(col + 1, c1))]
    else:
        return None
    for r, c in gap:
        read = any(s == sheet and a <= r <= b and x <= c <= y for s, a, x, b, y in reads)
        if not read and not book.is_formula(sheet, r, c) and _number(book.value(sheet, r, c)):
            return f'{get_column_letter(c)}{r}'
    return None


def work_out(path: Path, known: frozenset = frozenset()) -> Totals:
    """The totals and formula faults of one workbook. known: (sheet, formula) pairs that already showed the fault in
    the blank template the workbook was filled from; those are the template's own, not the agent's."""
    out = Totals()
    book = _Book(path)
    try:
        cells = list(islice(book.formula_cells(), MAX_FORMULAS + 1))
        # Past the cap the scan stops, so which cells hold formulas is known only that far: a total's gap could
        # then hold an unseen formula and be blamed for leaving it out. Totals are still listed; gaps are not judged.
        whole = len(cells) <= MAX_FORMULAS
        cells = cells[:MAX_FORMULAS]
        out.formulas = len(cells)
        for sheet, r, c, text in cells:
            place = f'{sheet}!{get_column_letter(c)}{r}'
            try:
                value = book.calc.result(sheet, r, c)
                parsed = formula_calc._parse(text[1:], sheet)  # noqa: SLF001 - the calculator's own parse
            except Exception:  # noqa: BLE001 - a formula the calculator trips on is counted, never a reason to fail
                out.unsupported += 1
                out.unsupported_at.append(place)
                continue
            if isinstance(value, str) and value in formula_calc.ERRORS:
                out.errors.add((sheet, text))
                if (sheet, text) not in known:
                    out.problems.append(f'{place} shows {value} ({text}). Fix the formula or the cells it reads.')
                continue
            ranges: list = []
            _ranges(parsed, ranges)
            if not ranges:
                continue
            out.totals.append({'sheet': sheet, 'cell': place.split('!', 1)[1], 'formula': text, 'value': value})
            reads: list = []
            _reads(parsed, sheet, reads)
            for rng in ranges if whole else ():
                missed = _left_out(book, sheet, r, c, rng, reads)
                if missed:
                    out.short.add((sheet, text))
                    if (sheet, text) not in known:
                        out.problems.append(
                            f'{place} ({text}) leaves out {missed}, a number between the range and the total. '
                            'Stretch the range over every row it should total.'
                        )
                    break
    finally:
        book.close()
    return out


def template_faults(template: Path) -> frozenset:
    """The formulas that already show an error, or already leave a number out, in a blank template, so a filled copy
    is not blamed for the template's own layout."""
    try:
        return _template_faults(template, template.stat().st_mtime_ns)
    except OSError:
        return frozenset()


@lru_cache(maxsize=16)
def _template_faults(template: Path, _changed: int) -> frozenset:
    """Worked out once per template file: every save of a run reads the same blank template."""
    try:
        found = work_out(template)
    except Exception:  # noqa: BLE001 - a template the calculator cannot read blames nothing
        return frozenset()
    return frozenset(found.errors | found.short)


def listed(name: str, found: Totals) -> list[str]:
    """The totals of one workbook as lines for the save reply."""
    lines = [f'{name} {t["sheet"]}!{t["cell"]} = {_shown(t["value"])} ({t["formula"]})' for t in found.totals]
    more = len(lines) - MAX_LISTED
    return lines[:MAX_LISTED] + ([f'{name}: ... and {more} more totals'] if more > 0 else [])
