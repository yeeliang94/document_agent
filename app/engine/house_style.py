"""House style: gives a workbook the agent built a tidy, consistent look, so it does not hand-code one each run.

This file is copied into the agent's sandbox as it is, so it needs nothing but openpyxl. In the sandbox:

    import sys; sys.path.insert(0, '/mnt/data')
    from house_style import apply
    apply('/mnt/data/outputs/journal.xlsx', '/mnt/data/journal_format.json')   # a workbook built to a format
    apply('/mnt/data/outputs/summary.xlsx')                                     # any workbook: headings in row 1

A format can say how its workbook looks under "style" (every key is optional):

    "style": {
      "header": {"bold": true, "fill": "D9E1F2", "font_color": "000000", "border": "thin"},
      "number_format": "#,##0.00", "integer_format": "#,##0", "date_format": "dd/mm/yyyy",
      "column_widths": {"Description": 40},
      "freeze_header": true
    }

A format with no style, and a workbook with no format, get the defaults below. Columns take their type from the
format; without one, from the values in them. Numbers and dates written as text in a number or date column are
turned into real numbers and dates. A filled template keeps its own look: do not apply this to one.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

DEFAULT = {
    'header': {'bold': True, 'fill': 'D9E1F2', 'font_color': '000000', 'border': 'thin'},
    'number_format': '#,##0.00',
    'integer_format': '#,##0',
    'date_format': 'dd/mm/yyyy',
    'column_widths': {},
    'freeze_header': True,
}
KEYS = set(DEFAULT)
MIN_WIDTH, MAX_WIDTH = 8, 60
UNSET = {'General', 'yyyy-mm-dd h:mm:ss'}  # no format, or the one openpyxl gives any date it writes
NUMBER = re.compile(r'^\s*-?(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d+)?\s*$')
DATES = (('%Y-%m-%d', re.compile(r'^\d{4}-\d{2}-\d{2}$')), ('%d/%m/%Y', re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')))


def check_style(style) -> None:
    """Refuse a style the helper cannot apply, with a plain reason."""
    if not isinstance(style, dict):
        raise ValueError('A format\'s "style" must be an object.')
    unknown = sorted(set(style) - KEYS)
    if unknown:
        raise ValueError(f'Unknown style setting(s) {unknown}; known: {sorted(KEYS)}.')
    header = style.get('header', {})
    if not isinstance(header, dict) or set(header) - set(DEFAULT['header']):
        raise ValueError(f'"header" may set only {sorted(DEFAULT["header"])}.')
    for key in ('fill', 'font_color'):
        if key in header and not re.fullmatch(r'[0-9A-Fa-f]{6}', str(header[key])):
            raise ValueError(f'header {key} must be a colour such as "D9E1F2".')
    widths = style.get('column_widths', {})
    if not isinstance(widths, dict) or not all(isinstance(v, (int, float)) and 1 <= v <= 255 for v in widths.values()):
        raise ValueError('"column_widths" maps column names to widths from 1 to 255.')


def as_number(text: str):
    """A number written as text, such as "4,200.50", or None. Text with a leading zero (a code) stays text."""
    if not isinstance(text, str) or not NUMBER.match(text) or not re.search(r'\d', text):
        return None
    plain = text.strip().replace(',', '')
    if re.match(r'^-?0\d', plain):
        return None
    try:
        value = float(plain)
    except ValueError:
        return None
    return int(value) if value.is_integer() and '.' not in plain else value


def as_date(text: str):
    if not isinstance(text, str):
        return None
    for pattern, shape in DATES:
        if shape.match(text.strip()):
            try:
                return datetime.strptime(text.strip(), pattern)
            except ValueError:
                return None
    return None


def _kind(values: list) -> str:
    """A column's type from its values: date, integer, number or text."""
    real = [v for v in values if v not in (None, '') and not (isinstance(v, str) and v.startswith('='))]  # totals
    if not real:
        return 'text'
    if all(isinstance(v, (datetime, date)) or as_date(v) for v in real):
        return 'date'
    numbers = [v if isinstance(v, (int, float)) and not isinstance(v, bool) else as_number(v) for v in real]
    if all(n is not None for n in numbers):
        return 'integer' if all(float(n).is_integer() for n in numbers) else 'number'
    return 'text'


def _shown_length(value, kind: str, style: dict) -> int:
    if value is None:
        return 0
    if kind == 'date':
        return len(style['date_format'])
    if kind in ('number', 'integer') and isinstance(value, (int, float)):
        decimals = 2 if kind == 'number' and '.00' in style['number_format'] else 0
        return len(f'{value:,.{decimals}f}')
    return max((len(line) for line in str(value).split('\n')), default=0)


def _style_sheet(ws, header_row: int, types: dict[str, str], style: dict) -> None:
    head = style['header']
    side = Side(style=head.get('border')) if head.get('border') else None
    headers = {}
    for cell in ws[header_row]:
        if cell.value not in (None, ''):
            headers[cell.column] = str(cell.value).strip()
            cell.font = Font(bold=bool(head.get('bold')), color='FF' + str(head.get('font_color', '000000')).upper())
            if head.get('fill'):
                cell.fill = PatternFill('solid', fgColor='FF' + str(head['fill']).upper())
            if side:
                cell.border = Border(bottom=side)
    last = ws.max_row
    for column, name in headers.items():
        cells = [ws.cell(r, column) for r in range(header_row + 1, last + 1)]
        kind = types.get(name) or _kind([c.value for c in cells])
        for cell in cells:  # a format the agent chose for a cell (a percentage, a currency) is kept
            if kind == 'date':
                if isinstance(cell.value, str) and as_date(cell.value):
                    cell.value = as_date(cell.value)
                if cell.number_format in UNSET:
                    cell.number_format = style['date_format']
            elif kind in ('number', 'integer'):
                if isinstance(cell.value, str) and as_number(cell.value) is not None:
                    cell.value = as_number(cell.value)
                if cell.number_format in UNSET:
                    cell.number_format = style['integer_format' if kind == 'integer' else 'number_format']
        width = style['column_widths'].get(name)
        if width is None:
            longest = max([len(name), *(_shown_length(c.value, kind, style) for c in cells)])
            width = min(max(longest + 2, MIN_WIDTH), MAX_WIDTH)
        ws.column_dimensions[get_column_letter(column)].width = width
    if style['freeze_header'] and headers:
        ws.freeze_panes = f'A{header_row + 1}'


def apply(workbook: str | Path, format_path: str | Path | dict | None = None, save_as: str | Path | None = None) -> str:
    """Give a workbook the house style and save it (in place, or to save_as). Returns what was done, in a line."""
    spec = format_path
    if isinstance(format_path, (str, Path)):
        spec = json.loads(Path(format_path).read_text(encoding='utf-8-sig'))
    spec = spec or {}
    given = spec.get('style') or {}
    check_style(given)
    style = {**DEFAULT, **given, 'header': {**DEFAULT['header'], **given.get('header', {})}}
    wb = load_workbook(workbook)
    sheets = {s['name']: s for s in spec.get('sheets', [])}
    done = []
    for ws in wb.worksheets:
        if ws.sheet_state != 'visible' or (sheets and ws.title not in sheets):
            continue
        sheet = sheets.get(ws.title, {})
        types = {c['name']: c.get('type', 'text') for c in sheet.get('columns', [])}
        _style_sheet(ws, int(sheet.get('header_row', 1)), types, style)
        done.append(ws.title)
    wb.save(save_as or workbook)
    return f'House style applied to {", ".join(done) or "no sheet"}.'
