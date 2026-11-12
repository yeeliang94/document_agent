"""Deliverable rules written as data, so a module or a job can add checks without code.

A format (for a deliverable built from scratch) looks like:

    {
      "sheets": [
        {"name": "Journal", "header_row": 1,
         "columns": [
           {"name": "Journal Ref", "type": "text", "required": true, "pattern": "^PAY-"},
           {"name": "Account", "type": "text", "required": true, "allowed": ["2100", "1010"]},
           {"name": "Debit", "type": "number", "min": 0}
         ],
         "unique": [["Journal Ref", "Line"]]}
      ],
      "rules": [ ... any of the rules below ... ],
      "notes": "Anything else the agent should know about the format."
    }

Column types: text, number, integer, date (a real date or text YYYY-MM-DD).

Rules (in a format's "rules" or a job's "rules"; "file" limits a rule to one deliverable, "header_row" defaults to 1):
    {"type": "balanced", "sheet": "Journal", "debit": "Debit", "credit": "Credit", "group_by": "Journal Ref"}
    {"type": "one_side", "sheet": "Journal", "columns": ["Debit", "Credit"]}   # exactly one is filled and non-zero
    {"type": "required", "sheet": "Employees", "columns": ["Employee ID"]}
    {"type": "unique", "sheet": "Employees", "columns": ["Entity", "Employee ID"]}
    {"type": "allowed", "sheet": "Journal", "column": "Currency", "values": ["SGD", "USD"]}
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime

TYPES = ('text', 'number', 'integer', 'date')
ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def table(ws, header_row: int = 1) -> tuple[dict[str, int], list[tuple[int, dict]]]:
    """Header name -> column number, and every non-empty row below the header as (row number, {header: value})."""
    headers = {}
    for cell in ws[header_row]:
        if not blank(cell.value):
            headers.setdefault(str(cell.value).strip(), cell.column)
    rows = []
    for row in ws.iter_rows(min_row=header_row + 1):
        values = {name: row[col - 1].value if col - 1 < len(row) else None for name, col in headers.items()}
        if any(not blank(c.value) for c in row):
            rows.append((row[0].row, values))
    return headers, rows


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def type_ok(kind: str, value) -> bool:
    if kind == 'number':
        return _number(value)
    if kind == 'integer':
        return _number(value) and float(value).is_integer()
    if kind == 'date':
        return isinstance(value, (date, datetime)) or (isinstance(value, str) and bool(ISO_DATE.match(value.strip())))
    return True


def _key(value) -> str:
    """A comparable form, as Excel compares list values: case and spaces ignored, 1 and '1' alike."""
    if _number(value) and float(value).is_integer():
        return str(int(value))
    return str(value).strip().casefold()


def _amount(value) -> float:
    return float(value) if _number(value) else 0.0


def check_format(wb, spec: dict, label: str) -> list[str]:
    """A workbook built from scratch against its format specification."""
    issues = []
    for sheet in spec.get('sheets', []):
        name = sheet['name']
        if name not in wb.sheetnames:
            issues.append(
                f'{label}: sheet "{name}" is missing (the format needs sheets {[s["name"] for s in spec["sheets"]]}).'
            )
            continue
        header_row = sheet.get('header_row', 1)
        columns = sheet.get('columns', [])
        headers, rows = table(wb[name], header_row)
        wanted = [c['name'] for c in columns]
        if list(headers)[: len(wanted)] != wanted:
            issues.append(
                f'{label}: "{name}" row {header_row} must hold the headers {wanted} in this order; it holds {list(headers)}.'
            )
            continue
        if not rows:
            issues.append(f'{label}: "{name}" has no data rows.')
        for column in columns:
            col = column['name']
            pattern = re.compile(column['pattern']) if column.get('pattern') else None
            allowed = {_key(v) for v in column['allowed']} if column.get('allowed') else None
            for number, values in rows:
                value = values.get(col)
                where = f'{label}: {name}!{col} row {number}'
                if blank(value):
                    if column.get('required'):
                        issues.append(f'{where} is blank; it is required.')
                    continue
                if not type_ok(column.get('type', 'text'), value):
                    issues.append(f'{where} = {value!r} must be a {column.get("type")}.')
                elif allowed is not None and _key(value) not in allowed:
                    issues.append(f'{where} = {value!r} is not one of {column["allowed"]}.')
                elif pattern and not pattern.search(str(value)):
                    issues.append(f'{where} = {value!r} does not match the pattern {column["pattern"]}.')
                elif _number(value) and 'min' in column and value < column['min']:
                    issues.append(f'{where} = {value!r} is below the minimum {column["min"]}.')
                elif _number(value) and 'max' in column and value > column['max']:
                    issues.append(f'{where} = {value!r} is above the maximum {column["max"]}.')
        for combo in sheet.get('unique', []):
            issues += _unique(label, name, rows, combo)
    return issues


def _unique(label, sheet, rows, columns) -> list[str]:
    seen, issues = {}, []
    for number, values in rows:
        key = tuple(_key(values.get(c)) for c in columns)
        if key in seen:
            issues.append(f'{label}: {sheet} row {number} repeats {columns} = {key} from row {seen[key]}.')
        seen.setdefault(key, number)
    return issues


def check_rules(wb, rules: list[dict], label: str) -> list[str]:
    """Rules that compare values across rows or columns. A rule naming a missing sheet or column says so."""
    issues = []
    for rule in rules:
        sheet = rule.get('sheet')
        if sheet not in wb.sheetnames:
            issues.append(f'{label}: rule {rule["type"]} needs sheet "{sheet}", which is missing.')
            continue
        headers, rows = table(wb[sheet], rule.get('header_row', 1))
        needed = [rule.get(k) for k in ('debit', 'credit', 'group_by', 'column') if rule.get(k)] + rule.get(
            'columns', []
        )
        absent = [c for c in needed if c not in headers]
        if absent:
            issues.append(f'{label}: rule {rule["type"]} on "{sheet}" needs column(s) {absent}, which are missing.')
            continue
        kind = rule['type']
        if kind == 'balanced':
            totals = defaultdict(float)
            for _, values in rows:
                group = _key(values.get(rule['group_by'])) if rule.get('group_by') else 'all rows'
                totals[group] += _amount(values.get(rule['debit'])) - _amount(values.get(rule['credit']))
            tolerance = rule.get('tolerance', 0.01)
            for group, diff in totals.items():
                if abs(diff) > tolerance:
                    issues.append(
                        f'{label}: {sheet} {group}: debits and credits differ by {diff:,.2f}; they must balance.'
                    )
        elif kind == 'one_side':
            first, second = rule['columns']
            for number, values in rows:
                filled = [c for c in (first, second) if _amount(values.get(c)) != 0]
                if len(filled) != 1:
                    issues.append(f'{label}: {sheet} row {number} must have exactly one of {first} or {second}.')
        elif kind == 'required':
            for number, values in rows:
                for col in rule['columns']:
                    if blank(values.get(col)):
                        issues.append(f'{label}: {sheet}!{col} row {number} is blank; it is required.')
        elif kind == 'unique':
            issues += _unique(label, sheet, rows, rule['columns'])
        elif kind == 'allowed':
            allowed = {_key(v) for v in rule['values']}
            for number, values in rows:
                value = values.get(rule['column'])
                if not blank(value) and _key(value) not in allowed:
                    issues.append(
                        f'{label}: {sheet}!{rule["column"]} row {number} = {value!r} is not one of {rule["values"]}.'
                    )
        else:
            issues.append(f'{label}: unknown rule type {kind!r}.')
    return issues


def describe_format(spec: dict) -> str:
    """The format in words, for the agent's goal card."""
    lines = []
    for sheet in spec.get('sheets', []):
        lines.append(f'Sheet "{sheet["name"]}", headers in row {sheet.get("header_row", 1)}, in this order:')
        for c in sheet.get('columns', []):
            extras = [c.get('type', 'text')]
            if c.get('required'):
                extras.append('required')
            if c.get('allowed'):
                extras.append(f'one of {c["allowed"]}')
            if c.get('pattern'):
                extras.append(f'matches {c["pattern"]}')
            if 'min' in c:
                extras.append(f'at least {c["min"]}')
            lines.append(
                f'  - {c["name"]} ({", ".join(extras)})' + (f': {c["description"]}' if c.get('description') else '')
            )
        for combo in sheet.get('unique', []):
            lines.append(f'  No two rows share {combo}.')
    lines += describe_rules(spec.get('rules', []))
    if spec.get('notes'):
        lines.append(f'Notes: {spec["notes"]}')
    return '\n'.join(lines)


def describe_rules(rules: list[dict]) -> list[str]:
    words = {
        'balanced': lambda r: (
            f'In "{r["sheet"]}", {r["debit"]} and {r["credit"]} balance'
            + (f' within each {r["group_by"]}.' if r.get('group_by') else '.')
        ),
        'one_side': lambda r: f'In "{r["sheet"]}", each row has exactly one of {r["columns"][0]} or {r["columns"][1]}.',
        'required': lambda r: f'In "{r["sheet"]}", {r["columns"]} are never blank.',
        'unique': lambda r: f'In "{r["sheet"]}", no two rows share {r["columns"]}.',
        'allowed': lambda r: f'In "{r["sheet"]}", {r["column"]} is one of {r["values"]}.',
    }
    return [words[r['type']](r) for r in rules if r.get('type') in words]


RULE_KEYS = {
    'balanced': ('sheet', 'debit', 'credit'),
    'one_side': ('sheet', 'columns'),
    'required': ('sheet', 'columns'),
    'unique': ('sheet', 'columns'),
    'allowed': ('sheet', 'column', 'values'),
}


def check_rule_definitions(rules) -> None:
    """Refuse rules the checks cannot apply, so a setup mistake is reported to the person, not to the agent."""
    if not isinstance(rules, list):
        raise ValueError('"rules" must be a list.')
    for number, rule in enumerate(rules, start=1):
        kind = rule.get('type') if isinstance(rule, dict) else None
        if kind not in RULE_KEYS:
            raise ValueError(f'Rule {number} has unknown type {kind!r}; use one of {sorted(RULE_KEYS)}.')
        missing = [k for k in RULE_KEYS[kind] if not rule.get(k)]
        if missing:
            raise ValueError(f'Rule {number} ({kind}) needs {missing}.')


def check_format_definition(spec) -> None:
    """Refuse a format the checks cannot apply: no sheets, columns without names, unknown types or rule types, or
    rules that name a column the format does not have."""
    if not isinstance(spec, dict) or not isinstance(spec.get('sheets'), list) or not spec['sheets']:
        raise ValueError('A format needs a "sheets" list with at least one sheet.')
    columns = {}
    for sheet in spec['sheets']:
        if not isinstance(sheet, dict) or not sheet.get('name') or not isinstance(sheet.get('columns'), list):
            raise ValueError('Each format sheet needs a "name" and a "columns" list.')
        names = []
        for column in sheet['columns']:
            if not isinstance(column, dict) or not column.get('name'):
                raise ValueError(f'Sheet "{sheet["name"]}": each column needs a "name".')
            if column.get('type', 'text') not in TYPES:
                raise ValueError(
                    f'Sheet "{sheet["name"]}": column "{column["name"]}" type must be one of {list(TYPES)}.'
                )
            names.append(column['name'])
        columns[sheet['name']] = set(names)
    check_rule_definitions(spec.get('rules', []))
    for rule in spec.get('rules', []):
        named = [rule.get(k) for k in ('debit', 'credit', 'column') if rule.get(k)] + list(rule.get('columns', []))
        if rule['sheet'] not in columns:
            raise ValueError(f'Rule {rule["type"]} names sheet "{rule["sheet"]}", which the format does not have.')
        unknown = [c for c in named if c not in columns[rule['sheet']]]
        if unknown:
            raise ValueError(f'Rule {rule["type"]} on "{rule["sheet"]}" names columns {unknown} the format lacks.')
