"""Checks that run on this machine against what the agent saved.

They know only the job (zip, templates, formats, rules), never an answer key, and they apply to any job:
- a template deliverable keeps everything the template already held (sheets, visibility, every non-empty cell and
  formula), follows the template's own data-validation rules (dropdowns, number-only cells) and gains no duplicate rows
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
import zipfile
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path, PurePosixPath

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries

from .jobspec import Deliverable, JobSpec
from .rules import _key, blank, check_format, check_rules, filled_rows, table

JUNK_NAMES = {'.DS_Store', 'Thumbs.db', 'desktop.ini'}
MAX_UNCOMPRESSED = 2 * 1024**3
DOCUMENT_MAP = 'document_map.json'
PROVENANCE = 'provenance.json'
DECISIONS = ('used', 'reference', 'not_relevant', 'unclear')
MAX_ISSUES = 40
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


def _validation_issues(template, out) -> list[str]:
    issues = []
    for sheet in template.worksheets:
        if sheet.title not in out.sheetnames:
            continue
        target = out[sheet.title]
        issues += _lost_rules(sheet, target)
        # Only rows and columns that hold something are checked, so a rule that covers a whole column stays cheap.
        filled, before = filled_rows(target), filled_rows(sheet)
        width = max([c for cells in (*filled.values(), *before.values()) for c in cells], default=0)
        written = _written_rows(sheet, target)
        numbers = sorted(filled)
        for rule in sheet.data_validations.dataValidation:
            allowed = _list_values(rule.formula1, template) if rule.type == 'list' else None
            allowed_keys = {_key(a) for a in allowed or []}
            for cell_range in rule.sqref.ranges:
                min_col, min_row, max_col, max_row = cell_range.bounds
                # A rule that spans a whole row is checked only as far as the sheets hold values.
                last = max_col if max_col - min_col < WIDE_RULE else min(max_col, max(width, min_col))
                for number in numbers[bisect_left(numbers, min_row) : bisect_right(numbers, max_row)]:
                    for column in range(min_col, last + 1):
                        value = filled[number].get(column)
                        where = f'{sheet.title}!{get_column_letter(column)}{number}'
                        if value is None:
                            if not rule.allow_blank and number in written:
                                issues.append(f'{where} is blank; the template requires a value.')
                        elif isinstance(value, str) and value.startswith('='):
                            # A formula's result is not known here. One the template put there stands; a new one
                            # could hide any value, so the agent is asked for the value itself.
                            if before.get(number, {}).get(column) != value:
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


def _lost_rules(sheet, target) -> list[str]:
    """Template entry rules (dropdowns, number-only cells) the output no longer carries on the same cells. One line
    per sheet, so a workbook rebuilt without its rules does not crowd out every other problem."""
    kept = defaultdict(list)
    for rule in target.data_validations.dataValidation:
        kept[_rule_key(rule)] += rule.sqref.ranges
    lost = [
        f'{cell_range.coord} ({"dropdown" if rule.type == "list" else f"{rule.type} rule"})'
        for rule in sheet.data_validations.dataValidation
        for cell_range in rule.sqref.ranges
        if not _covered(cell_range, kept[_rule_key(rule)])
    ]
    if not lost:
        return []
    return [
        f'Sheet "{sheet.title}": {len(lost)} of the template\'s entry rules are missing or changed, e.g. '
        f'{", ".join(lost[:3])}. Open the template and write into it, so its rules stay as they are.'
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


def _written_rows(template_sheet, ws) -> set[int]:
    """Rows the agent wrote into: some cell holds a value where the template's cell was blank. A row the template
    already filled (labels, formulas) is not the agent's, so its blank validated cells are not required."""
    before = filled_rows(template_sheet)
    return {n for n, cells in filled_rows(ws).items() if any(c not in before.get(n, {}) for c in cells)}


def _duplicate_rows(template, out) -> list[str]:
    issues = []
    for sheet in template.worksheets:
        if sheet.title not in out.sheetnames:
            continue
        seen, last = {}, max(filled_rows(sheet), default=0)  # the template's last row that holds anything
        for number, cells in sorted(filled_rows(out[sheet.title]).items()):
            if number <= last:
                continue
            key = tuple(sorted(cells.items()))
            if key in seen:
                issues.append(f'{sheet.title} row {number} duplicates row {seen[key]}.')
            seen.setdefault(key, number)
    return issues


def check_template(out, template: Path) -> list[str]:
    tpl = load_workbook(template)
    issues = []
    for sheet in tpl.worksheets:
        if sheet.title not in out.sheetnames:
            issues.append(f'Template sheet "{sheet.title}" is missing.')
            continue
        target = out[sheet.title]
        if target.sheet_state != sheet.sheet_state:
            issues.append(f'Sheet "{sheet.title}" visibility changed from {sheet.sheet_state} to {target.sheet_state}.')
        after = filled_rows(target)
        for number, cells in filled_rows(sheet).items():
            for column, value in cells.items():
                now = after.get(number, {}).get(column)
                if now != value:
                    issues.append(
                        f'{sheet.title}!{get_column_letter(column)}{number} was {value!r} in the template and is now '
                        f'{now!r}. Keep template content as it is.'
                    )
    issues += _validation_issues(tpl, out) + _duplicate_rows(tpl, out)
    return issues


def _open(path: Path):
    try:
        return load_workbook(path), None
    except Exception as error:  # noqa: BLE001 - any failure to open is the finding
        return None, f'{path.name} does not open as an Excel workbook: {error}'


def check_deliverable(outputs: Path, deliverable: Deliverable, job_rules) -> list[str]:
    path = outputs / deliverable.name
    if not path.exists():
        return [f'{deliverable.name} has not been saved yet.']
    wb, error = _open(path)
    if error:
        return [error]
    issues = check_template(wb, deliverable.template) if deliverable.template else []
    if deliverable.format:
        issues += check_format(wb, deliverable.format, deliverable.name)
    rules = list((deliverable.format or {}).get('rules', [])) + [
        r for r in job_rules if r.get('file') in (None, deliverable.name)
    ]
    return issues + check_rules(wb, rules, deliverable.name)


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


def written_rows(outputs: Path, deliverable: Deliverable) -> dict[str, set[int]]:
    """Per sheet, the rows the agent wrote: in a template, rows where a blank template cell now holds a value; in a
    format deliverable, every data row."""
    wb, error = _open(outputs / deliverable.name)
    if error:
        return {}
    found = {}
    if deliverable.template:
        tpl = load_workbook(deliverable.template)
        for sheet in tpl.worksheets:
            if sheet.title not in wb.sheetnames:
                continue
            rows = _written_rows(sheet, wb[sheet.title])
            if rows:
                found[sheet.title] = rows
    else:
        for sheet in deliverable.format.get('sheets', []):
            if sheet['name'] in wb.sheetnames:
                _, rows = table(wb[sheet['name']], sheet.get('header_row', 1))
                if rows:
                    found[sheet['name']] = {n for n, _ in rows}
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
    outputs: Path, job: JobSpec, files: list[dict], entries: dict[str, dict] | None
) -> tuple[list[str], dict]:
    """Problems with the row sources, and how many written rows have valid sources: every cited file is in the
    packet and marked used or reference."""
    written = {(d.name, sheet): rows for d in job.deliverables for sheet, rows in written_rows(outputs, d).items()}
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


def run_checks(outputs: Path, job: JobSpec, files: list[dict], proof: str = 'row') -> dict:
    """Every check, plus a summary per area for people: deliverable problems, files decided, rows with sources."""
    deliverable_issues = []
    for deliverable in job.deliverables:
        deliverable_issues += check_deliverable(outputs, deliverable, job.rules)
    entries, error = read_map(outputs)
    map_issues, decided = ([error], 0) if error else check_map(entries, files)
    summary = {
        'deliverable_problems': len(deliverable_issues),
        'files_decided': decided,
        'files': sum(not f.get('junk') for f in files),
    }
    provenance_issues = []
    if proof == 'row':
        provenance_issues, counts = check_provenance(outputs, job, files, entries)
        summary |= {'rows_sourced': counts['sourced'], 'rows_written': counts['written']}
    issues = deliverable_issues + map_issues + provenance_issues
    shown = issues[:MAX_ISSUES] + ([f'... and {len(issues) - MAX_ISSUES} more.'] if len(issues) > MAX_ISSUES else [])
    return {'passed': not issues, 'issues': shown, 'summary': summary}


def describe_template(path: Path) -> str:
    """The template in words, for the agent's goal card: sheets, headers, hidden sheets, formulas and rules."""
    wb = load_workbook(path)
    lines = []
    for ws in wb.worksheets:
        filled = filled_rows(ws)
        headers = [str(value) for _, value in sorted(filled.get(1, {}).items())]
        formulas = sum(isinstance(v, str) and v.startswith('=') for cells in filled.values() for v in cells.values())
        state = '' if ws.sheet_state == 'visible' else f' ({ws.sheet_state})'
        lines.append(
            f'Sheet "{ws.title}"{state}: row 1 {headers[:20]}' + (f', {formulas} formula cells' if formulas else '')
        )
        for rule in ws.data_validations.dataValidation:
            what = f'dropdown from {rule.formula1}' if rule.type == 'list' else f'{rule.type} only'
            lines.append(f'  - {rule.sqref}: {what}' + ('' if rule.allow_blank else ', required on used rows'))
    return '\n'.join(lines)
