"""Score a finished run against a test packet's answer key. The agent never sees the key.

answer_key.json:

    {
      "deliverables": {
        "filled_output.xlsx": {
          "sheets": {
            "Employees": {
              "header_row": 1,
              "key": ["Entity", "Employee ID"],          # headers that identify a row
              "fields": ["Employee Name", "Base Salary (Local)"],   # headers compared cell by cell
              "rows": [{"Entity": "SG01", "Employee ID": "SG001", "Employee Name": "...", ...}]
            }
          }
        }
      },
      "files": {"Group Finance/GL/GL_detail.xlsx": "not_relevant", ...},   # expected decision per file (any subset)
      "questions": [{"topic": "conflicting salary for E104", "keywords": ["E104"]}]   # questions the agent should ask
    }
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from app.engine.checks import _decision, entry_for, read_map
from app.engine.rules import blank, table

TOLERANCE = 0.011
RELEVANT = ('used', 'reference')


def norm(value):
    """A comparable form: numbers as floats, dates as YYYY-MM-DD, text trimmed and case-folded."""
    if isinstance(value, bool):
        return str(value).casefold()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = ' '.join(str(value if value is not None else '').split()).casefold()
    digits = text.replace(',', '').removeprefix('-')
    leading_zero = len(digits) > 1 and digits[0] == '0' and digits[1] != '.'  # an identifier such as 00123
    if digits.replace('.', '', 1).isdigit() and not leading_zero:
        return float(text.replace(',', ''))
    return text


def same(expected, actual) -> bool:
    a, b = norm(expected), norm(actual)
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= TOLERANCE
    if isinstance(a, float) or isinstance(b, float):
        return False
    return a == b


def row_key(values: dict, columns: list[str]) -> tuple:
    out = []
    for c in columns:
        v = norm(values.get(c))
        out.append(str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
    return tuple(out)


def score_sheet(wb, sheet: str, spec: dict) -> dict:
    if sheet not in wb.sheetnames:
        return {'rows_expected': len(spec['rows']), 'rows_found': 0, 'rows_extra': 0, 'sheet_missing': True,
                'cells_right': 0, 'cells_total': 0}  # fmt: skip
    _, rows = table(wb[sheet], spec.get('header_row', 1))
    got, repeats = {}, 0
    for _, values in rows:
        if all(blank(values.get(c)) for c in spec['key']):
            continue
        key = row_key(values, spec['key'])
        repeats += key in got  # a duplicated row is an extra row, not a free one
        got.setdefault(key, values)
    want = {row_key(r, spec['key']): r for r in spec['rows']}
    matched = set(want) & set(got)
    by_field = {f: sum(same(want[k].get(f), got[k].get(f)) for k in matched) for f in spec['fields']}
    return {
        'rows_expected': len(want),
        'rows_found': len(matched),
        'rows_extra': len(set(got) - set(want)) + repeats,
        'missing': sorted('/'.join(k) for k in set(want) - set(got))[:20],
        'extra': sorted('/'.join(k) for k in set(got) - set(want))[:20],
        'cells_right': sum(by_field.values()),
        'cells_total': len(matched) * len(spec['fields']),
        'by_field': {f: round(n / len(matched), 4) if matched else None for f, n in by_field.items()},
    }


def score(outputs: Path, key: dict, questions: list[dict] | None = None) -> dict:
    result = {'deliverables': {}}
    totals = {'expected': 0, 'found': 0, 'extra': 0, 'right': 0, 'cells': 0}
    for name, spec in key.get('deliverables', {}).items():
        path = outputs / name
        sheets = {}
        try:
            wb = load_workbook(path) if path.exists() else None
        except Exception:  # noqa: BLE001 - an unreadable workbook scores zero
            wb = None
        for sheet, sheet_spec in spec['sheets'].items():
            sheets[sheet] = (
                score_sheet(wb, sheet, sheet_spec)
                if wb is not None
                else {
                    'rows_expected': len(sheet_spec['rows']),
                    'rows_found': 0,
                    'rows_extra': 0,
                    'cells_right': 0,
                    'cells_total': 0,
                    'workbook_missing': True,
                }  # fmt: skip
            )
            s = sheets[sheet]
            totals['expected'] += s['rows_expected']
            totals['found'] += s['rows_found']
            totals['extra'] += s['rows_extra']
            totals['right'] += s['cells_right']
            totals['cells'] += s['cells_total']
        result['deliverables'][name] = sheets
    result['row_recall'] = round(totals['found'] / totals['expected'], 4) if totals['expected'] else None
    got_rows = totals['found'] + totals['extra']
    result['row_precision'] = round(totals['found'] / got_rows, 4) if got_rows else None
    result['cell_accuracy'] = round(totals['right'] / totals['cells'], 4) if totals['cells'] else None
    result.update(score_files(outputs, key.get('files', {})))
    result.update(score_questions(key.get('questions', []), questions or []))
    return result


def score_files(outputs: Path, expected: dict[str, str]) -> dict:
    entries, _ = read_map(outputs)
    entries = entries or {}
    relevance, exact, wrong = 0, 0, []
    for path, want in expected.items():
        entry = entry_for(entries, path)
        got = _decision(entry.get('decision')) if entry else 'missing'
        exact += got == want
        if (got in RELEVANT) == (want in RELEVANT):
            relevance += 1
        else:
            wrong.append(f'{path}: expected {want}, got {got}')
    n = len(expected)
    return {
        'files_scored': n,
        'relevance_accuracy': round(relevance / n, 4) if n else None,
        'decision_accuracy': round(exact / n, 4) if n else None,
        'relevance_wrong': wrong[:20],
    }


def score_questions(expected: list[dict], asked: list[dict]) -> dict:
    texts = [q.get('question', '').casefold() for q in asked]
    hits = [any(any(str(k).casefold() in t for k in e['keywords']) for t in texts) for e in expected]
    return {
        'questions_expected': len(expected),
        'questions_asked': len(asked),
        'asked_expected': all(hits) if expected else None,
        'unneeded_questions': len(asked) if not expected else max(0, len(asked) - sum(hits)),
    }
