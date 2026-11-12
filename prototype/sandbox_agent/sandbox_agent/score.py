"""Score a finished run against the answer key of a generated test packet. Never shown to the agent."""

from __future__ import annotations

import json
from pathlib import Path

from openpyxl import load_workbook

from .checks import _clean

FIELDS = ['name', 'department', 'currency', 'base_local', 'allowances_local', 'fx_rate', 'base_usd']


def _same(expected, actual) -> bool:
    if isinstance(expected, float) or isinstance(expected, int):
        try:
            return abs(float(actual) - float(expected)) <= 0.011
        except (TypeError, ValueError):
            return False
    return str(actual or '').strip().casefold() == str(expected).strip().casefold()


def score(run_dir: Path, key_path: Path, workbook_name: str = 'filled_output.xlsx') -> dict:
    key = json.loads(key_path.read_text(encoding='utf-8'))
    workbook = run_dir / 'outputs' / workbook_name
    if not workbook.exists():
        return {'workbook_found': False}
    wb = load_workbook(workbook)
    got = {}
    for row in wb['Employees'].iter_rows(min_row=2, values_only=True):
        if row and row[0]:
            padded = (list(row) + [None] * 9)[:9]
            got[(str(row[0]), str(row[1]))] = dict(zip(['entity', 'employee_id'] + FIELDS, padded, strict=True))
    expected = {(r['entity'], str(r['employee_id'])): r for r in key['employees']}
    matched = set(expected) & set(got)
    cells = {field: sum(_same(expected[k][field], got[k][field]) for k in matched) for field in FIELDS}
    by_entity = {}
    for entity in sorted({k[0] for k in expected}):
        want = {k for k in expected if k[0] == entity}
        have = {k for k in got if k[0] == entity}
        by_entity[entity] = {'expected': len(want), 'found': len(have & want), 'extra': len(have - want)}
    written = {}
    if 'File Map' in wb.sheetnames:
        for row in wb['File Map'].iter_rows(min_row=2, values_only=True):
            if row and row[0]:
                written[_clean(row[0])] = row[3] if len(row) > 3 else None

    def role_of(path):
        return next((r for p, r in written.items() if p == path or p.endswith('/' + path)), None)

    role_hits = {path: role_of(path) == role for path, role in key['roles'].items()}
    leavers_wrongly_included = [k for k in got if k[1] in key['excluded_leavers']]
    total_cells = len(matched) * len(FIELDS)
    return {
        'workbook_found': True,
        'rows_expected': len(expected),
        'rows_found': len(matched),
        'rows_missing': sorted('/'.join(k) for k in set(expected) - set(got))[:30],
        'rows_extra': sorted('/'.join(k) for k in set(got) - set(expected))[:30],
        'row_recall': round(len(matched) / len(expected), 4) if expected else None,
        'row_precision': round(len(matched) / len(got), 4) if got else None,
        'cell_accuracy': round(sum(cells.values()) / total_cells, 4) if total_cells else None,
        'cell_accuracy_by_field': {f: round(c / len(matched), 4) if matched else None for f, c in cells.items()},
        'by_entity': by_entity,
        'file_map_rows': len(written),
        'file_map_expected': len(key['files']),
        'key_roles_correct': role_hits,
        'leavers_wrongly_included': ['/'.join(k) for k in leavers_wrongly_included],
    }
