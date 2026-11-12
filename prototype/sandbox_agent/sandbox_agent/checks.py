"""Checks that run on this machine against what the agent saved.

They know only the uploaded zip and template, never an answer key, and they apply to any template:
- everything the template already contains is unchanged: sheets, visibility, every non-empty cell and formula
- the template's own data-validation rules hold: dropdown lists and number-only cells
- no table gains duplicate rows
- document_hierarchy.json lists every file in the zip

They are the feedback the agent gets when it saves, and the gate for a complete run.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath

from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries

JUNK_NAMES = {'.DS_Store', 'Thumbs.db', 'desktop.ini'}
MAX_UNCOMPRESSED = 2 * 1024**3
HIERARCHY = 'document_hierarchy.json'
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


def covered(paths: set[str], wanted: str) -> bool:
    """A packet file counts as listed when a written path is that path or ends with it, so absolute sandbox paths
    and paths that keep the zip's top folder both match."""
    return any(p == wanted or p.endswith('/' + wanted) for p in paths)


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
                            if not rule.allow_blank and _row_used(target, cell.row):
                                issues.append(
                                    f'{sheet.title}!{cell.coordinate} is blank; the template requires a value.'
                                )
                        elif allowed is not None and value not in allowed:
                            issues.append(
                                f'{sheet.title}!{cell.coordinate} = {value!r} is not in its dropdown {allowed}.'
                            )
                        elif rule.type in ('decimal', 'whole') and (
                            isinstance(value, bool) or not isinstance(value, (int, float))
                        ):
                            issues.append(f'{sheet.title}!{cell.coordinate} = {value!r} must be a number.')
    return issues


def _row_used(ws, row: int) -> bool:
    return any(c.value not in (None, '') for c in ws[row])


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


def check_workbook(path: Path, template: Path) -> list[str]:
    try:
        out = load_workbook(path)
    except Exception as error:  # noqa: BLE001 - any failure to open is the finding
        return [f'{path.name} does not open as an Excel workbook: {error}']
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


def check_hierarchy(path: Path, files: list[dict]) -> list[str]:
    try:
        tree = json.loads(path.read_text(encoding='utf-8'))
    except Exception as error:  # noqa: BLE001
        return [f'{path.name} is not valid JSON: {error}']
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for key in ('path', 'relative_path'):
                if isinstance(node.get(key), str):
                    found.add(_clean(node[key]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(tree)
    missing = [f['path'] for f in files if not covered(found, f['path'])]
    return [f'{path.name} does not include {len(missing)} file(s) as a "path" entry: {missing[:10]}'] if missing else []


def run_checks(outputs: Path, template: Path, files: list[dict], workbook_name: str) -> dict:
    issues = []
    workbook = outputs / workbook_name
    hierarchy = outputs / HIERARCHY
    issues += check_workbook(workbook, template) if workbook.exists() else [f'{workbook_name} has not been saved yet.']
    issues += check_hierarchy(hierarchy, files) if hierarchy.exists() else [f'{HIERARCHY} has not been saved yet.']
    shown = issues[:MAX_ISSUES] + ([f'... and {len(issues) - MAX_ISSUES} more.'] if len(issues) > MAX_ISSUES else [])
    return {'passed': not issues, 'issues': shown}
