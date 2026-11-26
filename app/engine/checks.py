"""Checks that run on this machine against what the agent saved.

They know only the job (zip, templates, formats, rules), never an answer key, and they apply to any job:
- a template deliverable keeps the template's sheets, their visibility, its formulas and its data-validation rules
  (dropdowns, number-only cells), follows those rules and gains no duplicate rows. Template content may move down
  when the agent inserts rows; content that moved or is no longer there as it was is listed as a template change,
  which the agent explains rather than fixes
- a format deliverable has the specified sheets, headers, types and allowed values (rules.py)
- the job's rules hold (for example, a journal balances)
- document_map.json gives every file in the zip a decision (used, reference, not_relevant, unclear) and a reason
- with row-level proof on, provenance.json names the source files of every row the agent wrote, and those files are
  marked used or reference in the map

They are the feedback the agent gets when it saves, and the gate for a complete run.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries
from openpyxl.worksheet.cell_range import CellRange

from .jobspec import Deliverable, JobSpec
from .looks import warnings as look_warnings
from .rules import _key, blank, check_format, check_rules, filled_rows, table
from .totals import listed, template_faults, work_out

JUNK_NAMES = {'.DS_Store', 'Thumbs.db', 'desktop.ini'}
MAX_UNCOMPRESSED = 2 * 1024**3
WORKBOOKS = {'.xlsx', '.xlsm'}
DOCUMENT_MAP = 'document_map.json'
PROVENANCE = 'provenance.json'
# The agent's records of its work and the app's own summary: never the output the user asked for.
RECORD_FILES = frozenset({DOCUMENT_MAP, PROVENANCE, 'notes.md', 'processing_summary.txt'})
DECISIONS = ('used', 'reference', 'not_relevant', 'unclear')
MAX_ISSUES = 40
MAX_GAPS = 10  # gap lines per sheet
WIDE_RULE = 100  # columns


def top_folder(zip_path: Path) -> str | None:
    """The zip's single top folder, when every file sits inside one."""
    with zipfile.ZipFile(zip_path) as archive:
        names = [PurePosixPath(m.filename) for m in _members(archive)]
    tops = {n.parts[0] for n in names}
    return tops.pop() if len(tops) == 1 and all(len(n.parts) > 1 for n in names) else None


def _members(archive: zipfile.ZipFile):
    return [m for m in archive.infolist() if not m.is_dir() and not m.filename.startswith('__MACOSX/')]


def inventory(zip_path: Path) -> list[dict]:
    """Every file in the zip. "path" is relative to the zip's single top folder when there is one; "member" is
    the name inside the zip."""
    top = top_folder(zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        members = _members(archive)
        if sum(m.file_size for m in members) > MAX_UNCOMPRESSED:
            raise ValueError('The zip expands to more than 2 GB; split the packet.')
        files = []
        for member in members:
            name = PurePosixPath(member.filename)
            relative = PurePosixPath(*name.parts[1:]) if top else name
            digest = hashlib.sha256()
            with archive.open(member) as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b''):
                    digest.update(chunk)
            files.append(
                {
                    'path': relative.as_posix(),
                    'member': member.filename,
                    'bytes': member.file_size,
                    'sha256': digest.hexdigest(),
                    'type': relative.suffix.lower().lstrip('.') or 'none',
                    'junk': relative.name in JUNK_NAMES,
                }
            )
    return sorted(files, key=lambda f: f['path'])


def _clean(path) -> str:
    return str(path or '').strip().replace('\\', '/').removeprefix('./')


def _list_values(formula: str, workbook) -> list | None:
    formula = (formula or '').lstrip('=')
    if formula.startswith('"') and formula.endswith('"'):
        return [v.strip() for v in formula[1:-1].split(',')]
    if '!' in formula:
        sheet, ref = formula.rsplit('!', 1)
        sheet = sheet.strip("'")
        if sheet in workbook.sheetnames:
            min_col, min_row, max_col, max_row = range_boundaries(ref.replace('$', ''))
            return [  # only cells that hold something, so a list that names a whole column stays cheap
                value
                for number, cells in sorted(filled_rows(workbook[sheet]).items())
                if (min_row or 1) <= number <= (max_row or number)  # "A:A" has no row limits
                for column, value in sorted(cells.items())
                if (min_col or 1) <= column <= (max_col or column)
            ]
    return None


MAX_ROW = 1_048_576  # Excel's last row
FORMULA = '='  # index key for "a formula sits in this column"
MAX_CHANGES = 20  # per sheet


def _formula(value) -> bool:
    return isinstance(value, str) and value.startswith('=')


@dataclass
class Alignment:
    """Where the template's rows ended up in the output. The agent may insert rows, so template content can move
    down; it counts as kept when every row it held is still there, in the same order, with the same values (a formula
    may change, for example to cover inserted rows)."""

    rows: dict[int, int]  # template row -> output row, for template rows that hold something
    written: set[int]  # output rows the agent wrote into: new rows, or template rows where it filled a blank cell
    missing: list[int]  # template rows that are no longer in the output as they were
    before: dict  # filled_rows of the template sheet
    after: dict  # filled_rows of the output sheet

    def __post_init__(self):
        self._anchors = sorted(self.rows.items())
        self._tops = [t for t, _ in self._anchors]
        self._source = {o: t for t, o in self.rows.items()}
        self._filled = sorted(self.before)

    def start(self, row: int) -> int:
        """The output row where template row `row` sits now: it moves with the nearest kept template row above it."""
        i = bisect_right(self._tops, row) - 1
        return row + (self._anchors[i][1] - self._anchors[i][0] if i >= 0 else 0)

    def end(self, row: int) -> int:
        """Like start, but measured from the next filled template row at or below `row` when that row was kept, so
        rows inserted above it (even across blank spacer rows) count as before it."""
        below = next((t for t in self._filled[bisect_left(self._filled, row) :]), None)
        if below in self.rows:
            return self.rows[below] - (below - row)
        return self.start(row)

    def window(self, row: int) -> tuple[int, int]:
        """The output rows where template row `row` could be now: between the kept template rows around it."""
        i = bisect_left(self._tops, row)
        low = self._anchors[i - 1][1] + 1 if i > 0 else 1
        high = self._anchors[i][1] - 1 if i < len(self._anchors) else MAX_ROW
        return low, high

    def span(self, top: int, bottom: int) -> tuple[int, int]:
        """Template rows top..bottom in the output, including any rows inserted among them or below them before the
        next kept template row. When the agent removed all of them, bottom is top - 1: an empty span."""
        first = min(self.start(top), MAX_ROW)
        return first, max(min(self.end(bottom + 1) - 1, MAX_ROW), first - 1)

    def template_cells(self, row: int) -> dict:
        """What the template held on the row that is now this output row; empty for a row the agent inserted."""
        source = self._source.get(row)
        return self.before.get(source, {}) if source else {}

    def template_value(self, row: int, column: int):
        """What the template held in the cell that is now at this output row and column."""
        source = self._source.get(row)
        return self.before.get(source, {}).get(column) if source else None


def _matches(template_cells: dict, output_cells: dict) -> bool:
    return all(
        _formula(output_cells.get(column)) if _formula(value) else output_cells.get(column) == value
        for column, value in template_cells.items()
    )


def align(template_sheet, output_sheet) -> Alignment:
    """Find each template row in the output, in order. Rows the agent inserted are allowed anywhere."""
    before, after = filled_rows(template_sheet), filled_rows(output_sheet)
    by_cell = defaultdict(list)
    for number in sorted(after):
        for column, value in after[number].items():
            by_cell[(column, FORMULA if _formula(value) else value)].append(number)
    rows, pointer, failed = {}, 0, {}
    for number in sorted(before):
        cells = before[number]
        signature = tuple(sorted((c, FORMULA if _formula(v) else v) for c, v in cells.items()))
        if failed.get(signature, MAX_ROW + 1) <= pointer:
            continue  # an identical row already found no match from an earlier point, so this one cannot either
        # Look the row up by its rarest cell: a plain value, or a formula in that column.
        candidates = min((by_cell.get(k, []) for k in signature), key=len)
        for found in candidates[bisect_right(candidates, pointer) :]:
            if _matches(cells, after[found]):
                rows[number] = pointer = found
                break
        else:
            failed[signature] = pointer
    kept = set(rows.values())
    written = {n for n in after if n not in kept}
    written |= {o for t, o in rows.items() if any(c not in before[t] for c in after[o])}
    missing = [n for n in sorted(before) if n not in rows]
    return Alignment(rows, written, missing, before, after)


def _shown(cells: dict) -> str:
    values = [repr(v)[:40] for _, v in sorted(cells.items())]
    return ', '.join(values[:4]) + (', ...' if len(values) > 4 else '')


def _template_changes(sheet, alignment: Alignment) -> list[str]:
    """Template content that moved or is no longer there as it was. Allowed when deliberate; the agent says why."""
    changes = [
        f'Sheet "{sheet.title}": template row {n} ({_shown(alignment.before[n])}) is no longer in the output as it was.'
        for n in alignment.missing
    ]
    run = []  # kept template rows that moved by the same number of rows
    for t, o in [*sorted(alignment.rows.items()), (None, None)]:
        if run and (t is None or o - t != run[0][1] - run[0][0]):
            shift = run[0][1] - run[0][0]
            if shift:
                (first, now_first), (last, now_last) = run[0], run[-1]
                rows = f'rows {first}-{last}' if last != first else f'row {first}'
                now = f'{now_first}-{now_last}' if last != first else str(now_first)
                way = f'{abs(shift)} row{"s" if abs(shift) > 1 else ""} {"down" if shift > 0 else "up"}'
                changes.append(f'Sheet "{sheet.title}": template {rows} moved {way} (now {now}).')
            run = []
        if t is not None:
            run.append((t, o))
    if len(changes) > MAX_CHANGES:
        changes = changes[:MAX_CHANGES] + [f'Sheet "{sheet.title}": ... and {len(changes) - MAX_CHANGES} more.']
    return changes


def _validation_issues(template, sheet, alignment: Alignment) -> list[str]:
    """The template's dropdowns and number rules, checked on the output cells they now cover: their own cells, moved
    with the template, plus any rows inserted among them."""
    issues = []
    filled, before = alignment.after, alignment.before
    width = max([c for cells in (*filled.values(), *before.values()) for c in cells], default=0)
    numbers = sorted(filled)
    for rule in sheet.data_validations.dataValidation:
        allowed = _list_values(rule.formula1, template) if rule.type == 'list' else None
        allowed_keys = {_key(a) for a in allowed or []}
        for cell_range in rule.sqref.ranges:
            min_col, min_row, max_col, max_row = cell_range.bounds
            top, bottom = alignment.span(min_row, max_row)
            # A rule that spans a whole row is checked only as far as the sheets hold values.
            last = max_col if max_col - min_col < WIDE_RULE else min(max_col, max(width, min_col))
            for number in numbers[bisect_left(numbers, top) : bisect_right(numbers, bottom)]:
                for column in range(min_col, last + 1):
                    value = filled[number].get(column)
                    where = f'{sheet.title}!{get_column_letter(column)}{number}'
                    if value is None:
                        if not rule.allow_blank and number in alignment.written:
                            issues.append(f'{where} is blank; the template requires a value.')
                    elif _formula(value):
                        # A formula's result is not known here. One the template put there stands; a new one
                        # could hide any value, so the agent is asked for the value itself.
                        if not _formula(alignment.template_value(number, column)):
                            issues.append(
                                f"{where} = {value!r} is a formula, so the template's rule for this cell cannot "
                                'be checked. Write the value itself.'
                            )
                    elif allowed is not None and _key(value) not in allowed_keys:
                        issues.append(f'{where} = {value!r} is not in its dropdown {allowed}.')
                    elif rule.type in ('decimal', 'whole'):
                        problem = _number_problem(rule, value)
                        if problem:
                            issues.append(f'{where} = {value!r} {problem}.')
    return issues


def _rule_key(rule) -> tuple:
    return rule.type, rule.operator, rule.formula1, rule.formula2, bool(rule.allow_blank)


def _covered(cell_range, others) -> bool:
    """Every cell of the range lies in one of the others, however they are split up."""
    left, top, right, bottom = cell_range.bounds
    columns = {left, *(v for o in others for v in (o.min_col, o.max_col + 1) if left < v <= right)}
    rows = {top, *(v for o in others for v in (o.min_row, o.max_row + 1) if top < v <= bottom)}
    return all(
        any(o.min_col <= column <= o.max_col and o.min_row <= row <= o.max_row for o in others)
        for column in columns
        for row in rows
    )


def _lost_rules(sheet, target, alignment: Alignment) -> list[str]:
    """Template entry rules (dropdowns, number-only cells) that no longer cover their cells in the output: where the
    template had them, moved down with inserted rows and stretched over rows inserted among them. One line per
    sheet, so a workbook rebuilt without its rules does not crowd out every other problem."""
    kept = defaultdict(list)
    for rule in target.data_validations.dataValidation:
        kept[_rule_key(rule)] += rule.sqref.ranges
    lost = []
    for rule in sheet.data_validations.dataValidation:
        for cell_range in rule.sqref.ranges:
            top, bottom = alignment.span(cell_range.min_row, cell_range.max_row)
            if bottom < top:
                continue  # the agent removed every row the rule covered, such as an unused example row
            wanted = CellRange(min_col=cell_range.min_col, min_row=top, max_col=cell_range.max_col, max_row=bottom)
            if not _covered(wanted, kept[_rule_key(rule)]):
                lost.append(f'{wanted.coord} ({"dropdown" if rule.type == "list" else f"{rule.type} rule"})')
    if not lost:
        return []
    return [
        f'Sheet "{sheet.title}": {len(lost)} of the template\'s entry rules do not cover their cells, e.g. '
        f"{', '.join(lost[:3])}. Keep the template's dropdowns and number rules, and stretch them over any rows you "
        "insert among them (openpyxl's insert_rows does not move or stretch them)."
    ]


def _bound(formula):
    """A validation limit as a number, or None when it is a cell reference or formula the check cannot evaluate."""
    try:
        return float(str(formula).lstrip('='))
    except (TypeError, ValueError):
        return None


NUMBER_RULES = {
    'between': (lambda v, a, b: a <= v <= b, 'must be between {a} and {b}'),
    'notBetween': (lambda v, a, b: not a <= v <= b, 'must not be between {a} and {b}'),
    'equal': (lambda v, a, b: v == a, 'must equal {a}'),
    'notEqual': (lambda v, a, b: v != a, 'must not equal {a}'),
    'greaterThan': (lambda v, a, b: v > a, 'must be greater than {a}'),
    'lessThan': (lambda v, a, b: v < a, 'must be less than {a}'),
    'greaterThanOrEqual': (lambda v, a, b: v >= a, 'must be {a} or more'),
    'lessThanOrEqual': (lambda v, a, b: v <= a, 'must be {a} or less'),
}


def _number_problem(rule, value) -> str | None:
    """What is wrong with a value under a template's number rule, as Excel would judge it, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 'must be a number'
    if rule.type == 'whole' and not float(value).is_integer():
        return 'must be a whole number'
    test, words = NUMBER_RULES.get(rule.operator or 'between', (None, None))
    low, high = _bound(rule.formula1), _bound(rule.formula2)
    if test is None or low is None or ('between' in (rule.operator or 'between').lower() and high is None):
        return None  # a limit given by a cell or formula is not evaluated here
    if not test(value, low, high):
        show = lambda n: f'{n:g}'  # noqa: E731
        return words.format(a=show(low), b=show(high) if high is not None else '')
    return None


def _duplicate_rows(title: str, alignment: Alignment) -> list[str]:
    issues, seen = [], {}
    for number in sorted(alignment.written):
        key = tuple(sorted(alignment.after[number].items()))
        if key in seen:
            issues.append(f'{title} row {number} duplicates row {seen[key]}.')
        seen.setdefault(key, number)
    return issues


_CELL_ROW = re.compile(r'(\$?[A-Z]{1,3}\$?)\d+')


def _shape(formula: str) -> str:
    """A formula with its row numbers taken out: =SUM(B5:B7) and =SUM(B5:B9) have the same shape."""
    return _CELL_ROW.sub(r'\1', formula.upper())


def _lost_formulas(title: str, alignment: Alignment) -> list[str]:
    """Template formulas that are gone: on a kept row, or on a row that changed. A changed row may have moved
    anywhere between the kept rows around it, so its formula counts as kept when a formula of the same shape sits
    in that column there."""
    issues = []
    shapes = defaultdict(list)  # (column, shape) -> output rows, in order
    for number in sorted(alignment.after):
        for column, value in alignment.after[number].items():
            if _formula(value):
                shapes[(column, _shape(value))].append(number)
    places = [(t, o, None) for t, o in sorted(alignment.rows.items())]
    places += [(t, alignment.start(t), alignment.window(t)) for t in alignment.missing]
    for t, o, window in places:
        for column, value in alignment.before[t].items():
            if not _formula(value):
                continue
            now = alignment.after.get(o, {}).get(column)
            if window is None and _formula(now):
                continue  # a kept row: its formula may change, for example to cover inserted rows
            found = shapes.get((column, _shape(value)), [])
            if window and bisect_left(found, window[0]) < bisect_right(found, window[1]):
                continue
            issues.append(
                f'{title}!{get_column_letter(column)}{o} held the template formula {value!r} and is now {now!r}. '
                "Keep the template's formulas; adjust their ranges if you insert rows."
            )
    return issues


def _spans(columns) -> str:
    """Column numbers as letters in runs: {1, 2, 3, 10} reads "A-C, J"."""
    runs = []
    for c in sorted(columns):
        if runs and c == runs[-1][1] + 1:
            runs[-1][1] = c
        else:
            runs.append([c, c])
    return ', '.join(
        get_column_letter(a) if a == b else f'{get_column_letter(a)}-{get_column_letter(b)}' for a, b in runs
    )


def _gaps(title: str, alignment: Alignment) -> list[str]:
    """Rows left empty among the agent's records, as facts the agent fills or explains, not faults.

    A record is a row where the agent filled at least two cells: a new row, or a template row it filled in. A single
    filled cell (a "Company:" field, a sign-off line) is not a record. The table's columns are the ones most records
    fill, so a remark the agent adds beside one record does not count. A gap is a row with nothing in those columns
    that sits between a record and the row above it that anchors it: the previous record, or the nearest row with
    two or more values in those columns (a heading, a total, the next table's headings). Spacers between tables and
    unused rows above a total are therefore not gaps; a row skipped because of a note beside the table is."""
    after = alignment.after
    new = {o: after[o].keys() - alignment.template_cells(o).keys() for o in alignment.written}
    records = sorted(o for o, cells in new.items() if len(cells) >= 2)
    if not records:
        return []
    counts = defaultdict(int)
    for o in records:
        for c in new[o]:
            counts[c] += 1
    columns = {c for c, n in counts.items() if n * 2 >= len(records)}
    taken = set(records)
    anchors = sorted(r for r in after if r not in taken and len(columns & after[r].keys()) >= 2)
    lines, previous = [], None
    for record in records:
        i = bisect_left(anchors, record)
        above = [a for a in (previous, anchors[i - 1] if i else None) if a is not None]
        anchor = max(above) if above else None
        previous = record
        if anchor is None:
            continue
        empty = [r for r in range(anchor + 1, record) if not columns & after.get(r, {}).keys()]
        if not empty:
            continue
        held = {r: ', '.join(f'{get_column_letter(c)}: "{str(v)[:40]}"' for c, v in sorted(after.get(r, {}).items()))
                for r in empty}  # fmt: skip
        for group in _runs(empty, held):
            one = len(group) == 1
            rows = f'Row {group[0]} sits' if one else f'Rows {group[0]}-{group[-1]} sit'
            beside = held[group[0]]
            what = (f'it holds only {beside}' if one else f'each holds only {beside}') if beside else 'otherwise blank'
            lines.append(
                f'Sheet "{title}": {rows} between row {anchor} and your record at row {record}, with nothing in the '
                f'columns your records fill ({_spans(columns)}); {what}.'
            )
    more = len(lines) - MAX_GAPS
    return lines[:MAX_GAPS] + ([f'Sheet "{title}": ... and {more} more.'] if more > 0 else [])


def _runs(rows: list[int], held: dict) -> list[list[int]]:
    """Consecutive rows that hold the same thing, together."""
    groups = []
    for r in rows:
        if groups and r == groups[-1][-1] + 1 and held[r] == held[groups[-1][-1]]:
            groups[-1].append(r)
        else:
            groups.append([r])
    return groups


@dataclass
class TemplateCheck:
    issues: list[str]  # must be fixed
    changes: list[str]  # allowed when deliberate; the agent explains them in notes.md
    alignments: dict[str, Alignment]  # per sheet
    gaps: list[str]  # rows left empty among the agent's records: fill them or say why they stay empty


def check_template(out, template: Path) -> TemplateCheck:
    tpl = load_workbook(template)
    issues, changes, alignments, gaps = [], [], {}, []
    for sheet in tpl.worksheets:
        if sheet.title not in out.sheetnames:
            issues.append(f'Template sheet "{sheet.title}" is missing.')
            continue
        target = out[sheet.title]
        if target.sheet_state != sheet.sheet_state:
            issues.append(f'Sheet "{sheet.title}" visibility changed from {sheet.sheet_state} to {target.sheet_state}.')
        alignment = alignments[sheet.title] = align(sheet, target)
        changes += _template_changes(sheet, alignment)
        issues += _lost_formulas(sheet.title, alignment)
        issues += _lost_rules(sheet, target, alignment)
        issues += _validation_issues(tpl, sheet, alignment)
        issues += _duplicate_rows(sheet.title, alignment)
        gaps += _gaps(sheet.title, alignment)
    return TemplateCheck(issues, changes, alignments, gaps)


def _open(path: Path):
    try:
        return load_workbook(path), None
    except Exception as error:  # noqa: BLE001 - any failure to open is the finding
        return None, f'{path.name} does not open as an Excel workbook: {error}'


@dataclass
class DeliverableCheck:
    issues: list[str]  # must be fixed
    changes: list[str]  # template changes the agent explains
    written: dict[str, set[int]]  # per sheet, the rows the agent wrote, which need sources
    gaps: list[str] = field(default_factory=list)  # rows left empty among the agent's records


def check_deliverable(outputs: Path, deliverable: Deliverable, job_rules) -> DeliverableCheck:
    path = outputs / deliverable.name
    if not path.exists():
        return DeliverableCheck([f'{deliverable.name} has not been saved yet.'], [], {})
    wb, error = _open(path)
    if error:
        return DeliverableCheck([error], [], {})
    rules = list((deliverable.format or {}).get('rules', [])) + [
        r for r in job_rules if r.get('file') in (None, deliverable.name)
    ]
    if deliverable.template:
        template = check_template(wb, deliverable.template)
        issues, changes, alignments = template.issues, template.changes, template.alignments
        written = {title: a.written for title, a in alignments.items() if a.written}
        # The template's own rows (instructions, labels, totals) are not records, so the job's rules skip them, and a
        # rule's header row is wherever the template's header row now sits.
        skip = {title: set(a.rows.values()) - a.written for title, a in alignments.items()}
        rules = [
            {**r, 'header_row': alignments[r['sheet']].start(r.get('header_row', 1))} if r.get('sheet') in alignments
            else r
            for r in rules
        ]  # fmt: skip
        return DeliverableCheck(
            issues + check_rules(wb, rules, deliverable.name, skip), changes, written, template.gaps
        )
    issues = check_format(wb, deliverable.format, deliverable.name)
    written = {}
    for sheet in deliverable.format.get('sheets', []):
        if sheet['name'] in wb.sheetnames:
            _, rows = table(wb[sheet['name']], sheet.get('header_row', 1))
            if rows:
                written[sheet['name']] = {n for n, _ in rows}
    return DeliverableCheck(issues + check_rules(wb, rules, deliverable.name), [], written)


def normal_decision(value) -> str:
    return str(value or '').strip().lower().replace(' ', '_').replace('-', '_')


class DocumentMap(dict):
    """Map entries by cleaned path, also found by any trailing part of the path (so "/mnt/data/packet/a/b.xlsx"
    answers for "a/b.xlsx"). The lookup is built once, so checking thousands of files stays quick."""

    def find(self, wanted: str) -> dict | None:
        if not hasattr(self, '_tails'):
            self._tails = {}
            for path, entry in self.items():
                parts = path.split('/')
                for start in range(len(parts)):
                    self._tails.setdefault('/'.join(parts[start:]), entry)
            self._tails.update(self)  # an exact path always wins
        return self._tails.get(wanted)


def packet_path(source: str, paths: set[str]) -> str | None:
    """The packet file a cited path refers to: the path itself or the longest packet path it ends with."""
    parts = source.split('/')
    return next(('/'.join(parts[i:]) for i in range(len(parts)) if '/'.join(parts[i:]) in paths), None)


def map_entries(tree) -> DocumentMap:
    """Every object in the map that carries a "path", by its cleaned path. Flat lists and nested trees both work."""
    found = DocumentMap()

    def walk(node):
        if isinstance(node, dict):
            for key in ('path', 'relative_path'):
                if isinstance(node.get(key), str):
                    found[_clean(node[key])] = node
                    break
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(tree)
    return found


def entry_for(entries: dict[str, dict], wanted: str) -> dict | None:
    if isinstance(entries, DocumentMap):
        return entries.find(wanted)
    return next((e for p, e in entries.items() if p == wanted or p.endswith('/' + wanted)), None)


def read_map(outputs: Path) -> tuple[dict[str, dict] | None, str | None]:
    path = outputs / DOCUMENT_MAP
    if not path.exists():
        return None, f'{DOCUMENT_MAP} has not been saved yet.'
    try:
        return map_entries(json.loads(path.read_text(encoding='utf-8'))), None
    except Exception as error:  # noqa: BLE001
        return None, f'{DOCUMENT_MAP} is not valid JSON: {error}'


def check_map(entries: dict[str, dict], files: list[dict]) -> tuple[list[str], int]:
    """Problems with the map, and how many files that need a decision have a valid one."""
    missing, bad, unexplained, decided = [], [], [], 0
    for f in files:
        entry = entry_for(entries, f['path'])
        if entry is None:
            if not f.get('junk'):  # system clutter such as Thumbs.db needs no decision
                missing.append(f['path'])
        elif normal_decision(entry.get('decision')) not in DECISIONS:
            bad.append(f'{f["path"]} ({entry.get("decision")!r})')
        else:
            decided += not f.get('junk')
            if blank(entry.get('reason')):
                unexplained.append(f['path'])
    issues = []
    if missing:
        issues.append(f'{DOCUMENT_MAP} has no entry with "path" for {len(missing)} file(s): {missing[:10]}')
    if bad:
        issues.append(f'{DOCUMENT_MAP}: "decision" must be one of {list(DECISIONS)} for {bad[:10]}')
    if unexplained:
        issues.append(f'{DOCUMENT_MAP}: give a one-line "reason" for {unexplained[:10]}')
    return issues, decided


def parse_rows(rows, top: int) -> set[int]:
    """Rows as 7, [7, 8], "7-12" or "7-12, 15", keeping rows 1 to top. Ranges are clipped before they are expanded,
    so a record that names billions of rows costs nothing."""
    if isinstance(rows, int):
        return {rows} if 1 <= rows <= top else set()
    if isinstance(rows, list):
        return {n for item in rows for n in parse_rows(item, top)}
    found = set()
    for part in str(rows).split(','):
        low, dash, high = part.strip().partition('-')
        if dash and not (low and high):
            raise ValueError(f'Not a row range: {part!r}')
        if low:
            found |= set(range(max(int(low), 1), min(int(high or low), top) + 1))
    return found


def _ranges(numbers: set[int]) -> str:
    ordered, parts = sorted(numbers), []
    start = prev = ordered[0]
    for n in ordered[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f'{start}-{prev}' if prev != start else str(start))
        if n is not None:
            start = prev = n
    return ', '.join(parts[:8]) + (' ...' if len(parts) > 8 else '')


def check_provenance(
    outputs: Path, written: dict[tuple[str, str], set[int]], files: list[dict], entries: dict[str, dict] | None
) -> tuple[list[str], dict]:
    """Problems with the row sources, and how many written rows have valid sources: every cited file is in the
    packet and marked used or reference."""
    counts = {'written': sum(len(rows) for rows in written.values()), 'sourced': 0}
    path = outputs / PROVENANCE
    if not path.exists():
        return [f'{PROVENANCE} has not been saved yet.'], counts
    try:
        records = json.loads(path.read_text(encoding='utf-8'))
        assert isinstance(records, list)
    except Exception:  # noqa: BLE001
        return [f'{PROVENANCE} must be a JSON list of {{"file", "sheet", "rows", "sources"}} objects.'], counts
    issues, covered_rows = [], {}
    paths = {f['path'] for f in files}
    last = max((max(rows) for rows in written.values()), default=0)  # rows past the last written one prove nothing
    for number, record in enumerate(records):
        try:
            rows = parse_rows(record['rows'], last)
            sources = [_clean(s) for s in record['sources']]
            key = (record['file'], record['sheet'])
        except Exception:  # noqa: BLE001
            issues.append(f'{PROVENANCE} item {number} needs "file", "sheet", "rows" (e.g. "2-40") and "sources".')
            continue
        valid = bool(sources)
        if not sources:
            issues.append(f'{PROVENANCE} item {number} ({key[0]} {key[1]} rows {record["rows"]}) names no sources.')
        for source in sources:
            match = packet_path(source, paths)
            if match is None:
                valid = False
                issues.append(f'{PROVENANCE} item {number} cites {source!r}, which is not a file in the packet.')
                continue
            entry = entry_for(entries or {}, match)
            if entry is not None and normal_decision(entry.get('decision')) not in ('used', 'reference'):
                valid = False
                issues.append(
                    f'{PROVENANCE} item {number} cites {match}, but {DOCUMENT_MAP} marks it '
                    f'{entry.get("decision")!r}. Mark it used or reference, or cite the right file.'
                )
        if valid:
            covered_rows.setdefault(key, set()).update(rows)
    for (name, sheet), rows in written.items():
        left = rows - covered_rows.get((name, sheet), set())
        counts['sourced'] += len(rows) - len(left)
        if left:
            issues.append(f'{PROVENANCE} gives no sources for {name} "{sheet}" rows {_ranges(left)}.')
    return issues, counts


def free_form_file(path: Path) -> tuple[str | None, dict[str, set[int]]]:
    """One output of a run that had no template and no format: a problem with the file if it cannot be used (empty,
    or it does not open as what its name says), and for a workbook the rows the agent wrote: on each visible sheet,
    every filled row below the first, which holds the headings."""
    kind = path.suffix.lower()
    if path.stat().st_size == 0:
        return 'the file is empty.', {}
    try:
        if kind == '.xlsx':
            book = load_workbook(path)
            rows = {}
            for sheet in book.worksheets:
                filled = sorted(filled_rows(sheet))
                if sheet.sheet_state == 'visible' and len(filled) > 1:
                    rows[sheet.title] = set(filled[1:])
            return None, rows
        if kind == '.docx':
            with zipfile.ZipFile(path) as archive:
                archive.getinfo('word/document.xml')
        elif kind == '.pdf':
            with path.open('rb') as handle:
                if handle.read(5) != b'%PDF-':
                    raise ValueError
        elif kind == '.json':
            json.loads(path.read_text(encoding='utf-8-sig'))
        elif kind in ('.csv', '.txt', '.md'):
            path.read_text(encoding='utf-8-sig')
    except Exception:  # noqa: BLE001 - whatever stops it opening, the user could not use it either
        return f'the file does not open as a {kind.lstrip(".")} file. Write it again.', {}
    return None, {}


def run_checks(outputs: Path, job: JobSpec, files: list[dict], proof: str = 'row') -> dict:
    """Every check, plus a summary per area for people: deliverable problems, files decided, rows with sources."""
    deliverable_issues, changes, written, gaps = [], [], {}, []
    # The workbooks whose totals and look are checked: (name, path, template, format)
    books = [(d.name, outputs / d.name, d.template, d.format) for d in job.deliverables]
    for deliverable in job.deliverables:
        result = check_deliverable(outputs, deliverable, job.rules)
        deliverable_issues += result.issues
        changes += [f'{deliverable.name}: {line}' for line in result.changes]
        gaps += [f'{deliverable.name}: {line}' for line in result.gaps]
        written |= {(deliverable.name, sheet): rows for sheet, rows in result.written.items()}
    if not job.deliverables:
        # No template and no format: the form of the output is the agent's choice. What can be checked is that
        # there is an output, that each file opens, and (below) that a workbook's rows name their sources.
        made = sorted(
            p
            for p in outputs.rglob('*')
            if p.is_file() and p.name not in RECORD_FILES and not p.name.endswith('.pending')
        )
        if not made:
            deliverable_issues.append(
                'No output file has been saved yet. Save what the goal calls for under /mnt/data/outputs/.'
            )
        for path in made:
            name = path.relative_to(outputs).as_posix()
            problem, rows = free_form_file(path)
            if problem:
                deliverable_issues.append(f'{name}: {problem}')
            written |= {(name, sheet): numbers for sheet, numbers in rows.items()}
            if path.suffix.lower() in WORKBOOKS and not problem:
                books.append((name, path, None, None))
    totals_issues, totals_lines, unsupported, looks = [], [], 0, []
    for name, path, template, format_spec in books:
        if not path.exists():
            continue
        try:
            found = work_out(path, template_faults(template) if template else frozenset())
        except Exception:  # noqa: BLE001 - a workbook that does not open is already a deliverable problem
            continue
        totals_issues += [f'{name}: {line}' for line in found.problems]
        totals_lines += listed(name, found)
        unsupported += found.unsupported
        rows = {sheet: numbers for (book, sheet), numbers in written.items() if book == name}
        try:
            looks += [f'{name}: {line}' for line in look_warnings(path, rows, format_spec)]
        except Exception:  # noqa: BLE001 - a look that cannot be checked is not a problem with the work
            pass
    entries, error = read_map(outputs)
    map_issues, decided = ([error], 0) if error else check_map(entries, files)
    summary = {
        'deliverable_problems': len(deliverable_issues),
        'files_decided': decided,
        'files': sum(not f.get('junk') for f in files),
        'totals': len(totals_lines),
        'totals_problems': len(totals_issues),
        'looks_right': not looks,
    }
    provenance_issues = []
    if proof == 'row':
        provenance_issues, counts = check_provenance(outputs, written, files, entries)
        summary |= {'rows_sourced': counts['sourced'], 'rows_written': counts['written']}
    issues = deliverable_issues + totals_issues + map_issues + provenance_issues
    shown = issues[:MAX_ISSUES] + ([f'... and {len(issues) - MAX_ISSUES} more.'] if len(issues) > MAX_ISSUES else [])
    result = {'passed': not issues, 'issues': shown, 'template_changes': changes, 'empty_rows': gaps,
              'totals': totals_lines, 'looks': looks, 'summary': summary}  # fmt: skip
    if unsupported:
        result['totals_not_worked_out'] = (
            f'{unsupported} formula(s) use something this machine cannot work out; check those in the sandbox.'
        )
    return result


MAX_LAYOUT = 12  # row groups per sheet in the template outline


def _layout(filled: dict) -> list[str]:
    """Which columns each run of rows uses, so content beside a table (a note, a source remark) stands out from the
    table itself: "rows 4-40: A-I" reads differently from "row 5: A-I, J" or "row 41: J"."""
    groups = []  # [first row, last row, column spans]
    for row in sorted(filled):
        spans = _spans(filled[row])
        if groups and groups[-1][1] == row - 1 and groups[-1][2] == spans:
            groups[-1][1] = row
        else:
            groups.append([row, row, spans])
    lines = [f'  {f"rows {a}-{b}" if a != b else f"row {a}"}: {spans}' for a, b, spans in groups]
    more = len(lines) - MAX_LAYOUT
    return lines[:MAX_LAYOUT] + ([f'  ... and {more} more row groups'] if more > 0 else [])


def describe_template(path: Path) -> str:
    """The template in outline, for the agent's goal card: sheets, where content sits and in which columns,
    formulas and entry rules. It names no header row or table: the agent reads the template and decides its layout
    itself."""
    wb = load_workbook(path)
    lines = []
    for ws in wb.worksheets:
        filled = filled_rows(ws)
        formulas = sum(_formula(v) for cells in filled.values() for v in cells.values())
        state = '' if ws.sheet_state == 'visible' else f' ({ws.sheet_state})'
        where = f'content in rows {_ranges(set(filled))}' if filled else 'empty'
        lines.append(f'Sheet "{ws.title}"{state}: {where}' + (f', {formulas} formula cells' if formulas else ''))
        if filled and ws.sheet_state == 'visible':  # lookup tabs are read, not filled
            lines += _layout(filled)
        for rule in ws.data_validations.dataValidation:
            what = f'dropdown from {rule.formula1}' if rule.type == 'list' else f'{rule.type} only'
            lines.append(f'  - {rule.sqref}: {what}' + ('' if rule.allow_blank else ', required on used rows'))
    return '\n'.join(lines)
