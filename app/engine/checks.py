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
from pathlib import Path, PurePosixPath

from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries

from .jobspec import Deliverable, JobSpec
from .rules import _key, blank, check_format, check_rules, table

JUNK_NAMES = {'.DS_Store', 'Thumbs.db', 'desktop.ini'}
MAX_UNCOMPRESSED = 2 * 1024**3
DOCUMENT_MAP = 'document_map.json'
PROVENANCE = 'provenance.json'
DECISIONS = ('used', 'reference', 'not_relevant', 'unclear')
MAX_ISSUES = 40


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
            ws = workbook[sheet]
            return [
                c.value
                for row in ws.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col)
                for c in row
                if c.value not in (None, '')
            ]
    return None


def _validation_issues(template, out) -> list[str]:
    issues = []
    for sheet in template.worksheets:
        if sheet.title not in out.sheetnames:
            continue
        target = out[sheet.title]
        for rule in sheet.data_validations.dataValidation:
            allowed = _list_values(rule.formula1, template) if rule.type == 'list' else None
            for cell_range in rule.sqref.ranges:
                min_col, min_row, max_col, max_row = cell_range.bounds
                for row in target.iter_rows(
                    min_row=min_row, max_row=min(max_row, target.max_row), min_col=min_col, max_col=max_col
                ):
                    for cell in row:
                        value = cell.value
                        if value in (None, ''):
                            if not rule.allow_blank and _row_written(sheet, target, cell.row):
                                issues.append(
                                    f'{sheet.title}!{cell.coordinate} is blank; the template requires a value.'
                                )
                        elif allowed is not None and _key(value) not in {_key(a) for a in allowed}:
                            issues.append(
                                f'{sheet.title}!{cell.coordinate} = {value!r} is not in its dropdown {allowed}.'
                            )
                        elif rule.type in ('decimal', 'whole') and (
                            isinstance(value, bool) or not isinstance(value, (int, float))
                        ):
                            issues.append(f'{sheet.title}!{cell.coordinate} = {value!r} must be a number.')
    return issues


def _row_written(template_sheet, ws, row: int) -> bool:
    """The agent wrote into this row: some cell holds a value where the template's cell was blank. A row the
    template already filled (labels, formulas) is not the agent's, so its blank validated cells are not required."""
    return any(not blank(c.value) and blank(template_sheet.cell(row, c.column).value) for c in ws[row])


def _duplicate_rows(template, out) -> list[str]:
    issues = []
    for sheet in template.worksheets:
        if sheet.title not in out.sheetnames:
            continue
        seen = {}
        for number, row in enumerate(out[sheet.title].iter_rows(min_row=sheet.max_row + 1, values_only=True)):
            if any(v not in (None, '') for v in row):
                key = tuple(row)
                if key in seen:
                    issues.append(f'{sheet.title} row {sheet.max_row + 1 + number} duplicates row {seen[key]}.')
                seen.setdefault(key, sheet.max_row + 1 + number)
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
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value not in (None, '') and target[cell.coordinate].value != cell.value:
                    issues.append(
                        f'{sheet.title}!{cell.coordinate} was {cell.value!r} in the template and is now '
                        f'{target[cell.coordinate].value!r}. Keep template content as it is.'
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


def parse_rows(rows) -> set[int]:
    """Rows as 7, [7, 8], "7-12" or "7-12, 15"."""
    if isinstance(rows, int):
        return {rows}
    if isinstance(rows, list):
        return {n for item in rows for n in parse_rows(item)}
    found = set()
    for part in str(rows).split(','):
        part = part.strip()
        if '-' in part:
            low, high = part.split('-', 1)
            found |= set(range(int(low), int(high) + 1))
        elif part:
            found.add(int(part))
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
            rows = set()
            for row in wb[sheet.title].iter_rows():
                for cell in row:
                    if not blank(cell.value) and blank(sheet[cell.coordinate].value):
                        rows.add(cell.row)
                        break
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
    for number, record in enumerate(records):
        try:
            rows = parse_rows(record['rows'])
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
        headers = [str(c.value) for c in ws[1] if not blank(c.value)] if ws.max_row else []
        formulas = sum(1 for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith('='))
        state = '' if ws.sheet_state == 'visible' else f' ({ws.sheet_state})'
        lines.append(
            f'Sheet "{ws.title}"{state}: row 1 {headers[:20]}' + (f', {formulas} formula cells' if formulas else '')
        )
        for rule in ws.data_validations.dataValidation:
            what = f'dropdown from {rule.formula1}' if rule.type == 'list' else f'{rule.type} only'
            lines.append(f'  - {rule.sqref}: {what}' + ('' if rule.allow_blank else ', required on used rows'))
    return '\n'.join(lines)
