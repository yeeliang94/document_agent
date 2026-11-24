"""Prove a test packet is consistent: build the "perfect" outputs from its answer key, then check and score them.

A packet passes when its perfect outputs pass every local check (so the key obeys the template's dropdowns, the
format and the rules) and score 100% (so the scorer reads what the key describes).
"""

from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook, load_workbook

from app.engine.checks import DOCUMENT_MAP, PROVENANCE, inventory, run_checks
from app.engine.jobspec import JobSpec
from app.engine.rules import blank, table

from .score import score


def _write_rows(ws, header_row: int, rows: list[dict]):
    headers, existing = table(ws, header_row)
    start = max([header_row] + [n for n, _ in existing]) + 1
    for offset, values in enumerate(rows):
        for name, value in values.items():
            if name in headers:
                ws.cell(start + offset, headers[name], value)


def perfect_outputs(job: JobSpec, key: dict, dest: Path) -> list[dict]:
    """Write the answer key's deliverables, a document map and provenance into dest. Returns the inventory."""
    dest.mkdir(parents=True, exist_ok=True)
    expected = key.get('deliverables', {})
    for deliverable in job.deliverables:
        sheets = expected.get(deliverable.name, {}).get('sheets', {})
        if deliverable.template:
            wb = load_workbook(deliverable.template)
        else:
            wb = Workbook()
            wb.remove(wb.active)
            for spec in deliverable.format['sheets']:
                ws = wb.create_sheet(spec['name'])
                for col, column in enumerate(spec['columns'], start=1):
                    ws.cell(spec.get('header_row', 1), col, column['name'])
        for sheet, spec in sheets.items():
            _write_rows(wb[sheet], spec.get('header_row', 1), spec['rows'])
        wb.save(dest / deliverable.name)
    files = inventory(job.zip)
    decisions = key.get('files', {})
    used = [p for p, d in decisions.items() if d == 'used'] or [files[0]['path']]
    (dest / DOCUMENT_MAP).write_text(
        json.dumps(
            {
                'files': [
                    {'path': f['path'], 'decision': decisions.get(f['path'], 'reference'), 'reason': 'answer key'}
                    for f in files
                ]
            }
        ),
        encoding='utf-8',
    )
    provenance = []
    for deliverable in job.deliverables:
        wb = load_workbook(dest / deliverable.name)
        for sheet in wb.worksheets:
            rows = [c.row for row in sheet.iter_rows() for c in row[:1]]
            filled = [r for r in rows if any(not blank(c.value) for c in sheet[r])]
            if filled:
                provenance.append({'file': deliverable.name, 'sheet': sheet.title, 'rows': filled, 'sources': used})
    (dest / PROVENANCE).write_text(json.dumps(provenance), encoding='utf-8')
    return files


def selftest(job: JobSpec, key: dict, dest: Path) -> dict:
    files = perfect_outputs(job, key, dest)
    check = run_checks(dest, job, files)
    asked = [{'question': ' '.join(str(k) for k in q['keywords'])} for q in key.get('questions', [])]
    result = score(dest, key, asked)
    perfect = (
        check['passed']
        and result['row_recall'] in (1.0, None)
        and result['row_precision'] in (1.0, None)
        and result['cell_accuracy'] in (1.0, None)
        and result['decision_accuracy'] in (1.0, None)
    )
    return {'passed': perfect, 'check': check, 'score': result}
