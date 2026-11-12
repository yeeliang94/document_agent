"""Generate a deliberately messy, synthetic multi-entity payroll packet and its answer key.

Everything is invented. The packet tests what real submissions do to an agent:
- folders nested four or five levels deep, with inconsistent naming
- workbooks with many tabs, title rows above the header, merged headers, hidden sheets
- the same facts under different column names, orders and languages
- one employee table split across department tabs
- per-employee allowances that must be summed from a separate tab
- a superseded file that must be ignored, a leavers list and a joiners CSV that change who is included
- an FX table where the right month must be picked
- a large irrelevant ledger that should be profiled, not read row by row
- an output template with formulas, a hidden lookup sheet and a file map to fill

The answer key is written outside the packet, so the agent never sees it.
"""

from __future__ import annotations

import csv
import json
import random
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

PERIOD_END = date(2026, 10, 31)
FX_OCT = {'SGD': 0.7712, 'MYR': 0.2241, 'GBP': 1.2905, 'EUR': 1.0874, 'USD': 1.0}
FX_SEP = {'SGD': 0.7650, 'MYR': 0.2199, 'GBP': 1.3010, 'EUR': 1.0791, 'USD': 1.0}
FX_NOV = {'SGD': 0.7790, 'MYR': 0.2260, 'GBP': 1.2850, 'EUR': 1.0902, 'USD': 1.0}

FIRST = 'Avery Blake Casey Devon Ellis Finley Gray Harper Indigo Jordan Kai Logan Morgan Noel Oakley Parker Quinn Reese Sage Taylor Umber Vale Wren Yael Zion'.split()
LAST = 'Ashford Brook Calder Dunmore Everly Fairley Garrow Hollis Ingram Jessup Kettering Larkin Merrow Norland Orchard Pemberton Quarry Rowan Stroud Thorne Upton Vance Whitlow Yardley'.split()
DEPARTMENTS = ['Sales', 'Operations', 'Finance', 'Engineering', 'People']
NOISE_COLUMNS = [
    'Cost Centre',
    'Grade',
    'Manager ID',
    'Work Pattern',
    'FTE',
    'Location Code',
    'Contract Type',
    'Probation End',
    'Pension Scheme',
    'Union Member',
    'Badge No',
    'Shift',
    'Notes',
]


@dataclass
class Employee:
    entity: str
    emp_id: str
    name: str
    department: str
    currency: str
    base: float
    allowances: float = 0.0
    leave_date: date | None = None
    included: bool = True


def _people(rng: random.Random, entity: str, prefix: str, count: int, currency: str, low: int, high: int):
    used = set()
    people = []
    for index in range(count):
        while True:
            name = f'{rng.choice(FIRST)} {rng.choice(LAST)}'
            if name not in used:
                used.add(name)
                break
        people.append(
            Employee(
                entity=entity,
                emp_id=f'{prefix}{index + 1:04d}',
                name=name,
                department=rng.choice(DEPARTMENTS),
                currency=currency,
                base=float(rng.randrange(low, high, 50)),
            )
        )
    return people


def _noise(rng: random.Random, column: str):
    return {
        'Cost Centre': f'CC-{rng.randint(100, 999)}',
        'Grade': rng.choice(['G3', 'G4', 'G5', 'G6', 'M1']),
        'Manager ID': f'M{rng.randint(1, 60):03d}',
        'Work Pattern': rng.choice(['5x8', '4x10', 'Flexi']),
        'FTE': rng.choice([1, 1, 1, 0.8, 0.6]),
        'Location Code': rng.choice(['HQ', 'BR1', 'BR2', 'RMT']),
        'Contract Type': rng.choice(['Permanent', 'Fixed term']),
        'Probation End': '2026-12-31',
        'Pension Scheme': rng.choice(['A', 'B', 'None']),
        'Union Member': rng.choice(['Y', 'N']),
        'Badge No': rng.randint(10000, 99999),
        'Shift': rng.choice(['Day', 'Late', '']),
        'Notes': '',
    }[column]


def _title_block(ws, lines: list[str]):
    for line in lines:
        ws.append([line])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([])


def _header(ws, columns: list[str]):
    ws.append(columns)
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')


def build_singapore(root: Path, rng: random.Random) -> list[Employee]:
    people = _people(rng, 'SG01', 'SG', 48, 'SGD', 4200, 12800)
    folder = root / 'Region_APAC' / 'Singapore' / 'Payroll' / '2026' / '10_October'
    folder.mkdir(parents=True)
    for person in people:
        person.allowances = 0.0

    def workbook(employees, scale, path):
        wb = Workbook()
        cover = wb.active
        cover.title = 'Cover'
        _title_block(
            cover,
            [
                'Singapore payroll submission',
                'Entity: SG01 Example Pte Ltd',
                'Period: October 2026',
                'Prepared by: payroll bureau',
                'Status: FINAL' if scale == 1 else 'Status: DRAFT v1 - superseded',
            ],
        )
        staff = wb.create_sheet('Staff Master')
        _title_block(staff, ['Staff master - October 2026', 'All amounts in SGD'])
        columns = ['Staff No', 'Employee Name', 'Dept', 'Basic Salary (SGD)'] + NOISE_COLUMNS[:9]
        _header(staff, columns)
        for person in employees:
            staff.append(
                [person.emp_id, person.name, person.department, round(person.base * scale, 2)]
                + [_noise(rng, c) for c in columns[4:]]
            )
        allowances = wb.create_sheet('Allowances')
        _header(allowances, ['Staff No', 'Allowance Type', 'Amount (SGD)', 'Taxable'])
        for person in employees:
            for kind in rng.sample(['Transport', 'Meal', 'Phone', 'Housing', 'Shift'], rng.randint(0, 3)):
                amount = float(rng.randrange(50, 900, 10))
                allowances.append([person.emp_id, kind, amount, rng.choice(['Y', 'N'])])
                if scale == 1:
                    person.allowances += amount
        cpf = wb.create_sheet('CPF Contributions')
        _header(cpf, ['Staff No', 'Employee CPF', 'Employer CPF'])
        for person in employees:
            cpf.append([person.emp_id, round(person.base * 0.2, 2), round(person.base * 0.17, 2)])
        hidden = wb.create_sheet('_codes')
        hidden.append(['Dept', 'Code'])
        for i, dept in enumerate(DEPARTMENTS):
            hidden.append([dept, f'D{i}'])
        hidden.sheet_state = 'hidden'
        wb.save(path)

    current = people[:46]
    workbook(current, 1, folder / 'SG01_Payroll_Oct2026_FINAL.xlsx')
    workbook(current, 0.97, folder / 'SG01_Payroll_Oct2026_v1_DRAFT_superseded.xlsx')
    hr = root / 'Region_APAC' / 'Singapore' / 'HR' / 'Movements'
    hr.mkdir(parents=True)
    with open(hr / 'new_joiners_oct2026.csv', 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['staff_no', 'name', 'department', 'start_date', 'monthly_basic_sgd', 'note'])
        for person in people[46:]:
            writer.writerow(
                [
                    person.emp_id,
                    person.name,
                    person.department,
                    '2026-10-13',
                    person.base,
                    'Not yet in Staff Master; include from October',
                ]
            )
    return people


def build_malaysia(root: Path, rng: random.Random) -> list[Employee]:
    people = _people(rng, 'MY01', 'MY-', 37, 'MYR', 3500, 15000)
    folder = root / 'Region_APAC' / 'Malaysia' / 'monthly submissions' / 'Oct 2026'
    folder.mkdir(parents=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Employees'
    ws.append(['MY01 Example Sdn Bhd'])
    ws.append([])
    ws.append(['Identity', None, 'Pay', None, None, 'Other'])
    ws.merge_cells('A3:B3')
    ws.merge_cells('C3:E3')
    ws.merge_cells('F3:H3')
    _header(
        ws,
        [
            'Full Name',
            'Emp No',
            'Basic (MYR)',
            'Fixed Allowance (MYR)',
            'Overtime Hrs',
            'Department',
            'Grade',
            'EPF No',
        ],
    )
    for person in people:
        person.allowances = float(rng.randrange(0, 600, 25))
        ws.append(
            [
                person.name,
                person.emp_id,
                person.base,
                person.allowances,
                rng.randint(0, 20),
                person.department,
                _noise(rng, 'Grade'),
                f'EPF{rng.randint(100000, 999999)}',
            ]
        )
    deductions = wb.create_sheet('Deductions')
    _header(deductions, ['Emp No', 'EPF', 'SOCSO', 'EIS', 'PCB'])
    for person in people:
        deductions.append([person.emp_id, round(person.base * 0.11, 2), 19.75, 7.9, round(person.base * 0.05, 2)])
    summary = wb.create_sheet('Summary')
    summary.append(['Headcount', len(people)])
    summary.append(['Total basic', sum(p.base for p in people)])
    wb.save(folder / 'MY Payroll October.xlsx')
    return people


def build_uk(root: Path, rng: random.Random) -> list[Employee]:
    people = _people(rng, 'UK01', 'U', 63, 'GBP', 2400, 9800)
    folder = root / 'Region_EMEA' / 'United Kingdom' / 'Payroll' / 'Oct-26'
    folder.mkdir(parents=True)
    wb = Workbook()
    wb.remove(wb.active)
    by_department = {}
    for person in people:
        person.allowances = float(rng.choice([0, 0, 75, 150, 250]))
        by_department.setdefault(person.department, []).append(person)
    for department in DEPARTMENTS:
        ws = wb.create_sheet(department)
        _title_block(ws, [f'UK01 - {department} - October 2026 (GBP)'])
        columns = [
            'Payroll ID',
            'Surname',
            'Forename',
            'Monthly Salary',
            'Car Allowance',
            'Department',
        ] + NOISE_COLUMNS[4:]
        _header(ws, columns)
        for person in by_department.get(department, []):
            forename, surname = person.name.split(' ', 1)
            ws.append(
                [person.emp_id, surname, forename, person.base, person.allowances, department]
                + [_noise(rng, c) for c in columns[6:]]
            )
    totals = wb.create_sheet('Totals')
    totals.append(['Department', 'Headcount'])
    for department in DEPARTMENTS:
        totals.append([department, f'=COUNTA({department}!A4:A500)'])
    wb.save(folder / 'uk_payroll_oct26.xlsx')

    leavers_dir = root / 'Region_EMEA' / 'United Kingdom' / 'HR'
    leavers_dir.mkdir(parents=True)
    leavers = Workbook()
    ws = leavers.active
    ws.title = 'Leavers'
    _header(ws, ['Payroll ID', 'Name', 'Leaving Date', 'Reason'])
    for person, leave in zip(
        rng.sample(people, 4),
        [date(2026, 10, 3), date(2026, 10, 31), date(2026, 11, 14), date(2026, 10, 20)],
        strict=True,
    ):
        person.leave_date = leave
        person.included = leave > PERIOD_END
        ws.append([person.emp_id, person.name, leave, rng.choice(['Resignation', 'End of contract'])])
    leavers.save(leavers_dir / 'Leavers & Starters 2026.xlsx')
    return people


def build_germany(root: Path, rng: random.Random) -> list[Employee]:
    people = _people(rng, 'DE01', '7', 29, 'EUR', 3100, 11000)
    for index, person in enumerate(people):
        person.emp_id = f'{700100 + index}'
    folder = root / 'Region_EMEA' / 'Germany' / 'Lohn' / '2026_10'
    folder.mkdir(parents=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Gehaltsliste'
    _title_block(ws, ['Gehaltsabrechnung Oktober 2026', 'DE01 Beispiel GmbH', 'Waehrung: EUR'])
    _header(
        ws,
        [
            'Personalnummer',
            'Nachname',
            'Vorname',
            'Abteilung',
            'Grundgehalt',
            'Zulagen',
            'Steuerklasse',
            'Kostenstelle',
        ],
    )
    translate = {
        'Sales': 'Vertrieb',
        'Operations': 'Betrieb',
        'Finance': 'Finanzen',
        'Engineering': 'Technik',
        'People': 'Personal',
    }
    for person in people:
        person.allowances = float(rng.randrange(0, 400, 20))
        forename, surname = person.name.split(' ', 1)
        ws.append(
            [
                int(person.emp_id),
                surname,
                forename,
                translate[person.department],
                person.base,
                person.allowances,
                rng.randint(1, 6),
                f'KST{rng.randint(10, 99)}',
            ]
        )
    legend = wb.create_sheet('Legende')
    _header(legend, ['Abteilung', 'Department'])
    for english, german in translate.items():
        legend.append([german, english])
    wb.save(folder / 'Gehalt_Oktober_2026.xlsx')
    return people


def build_us(root: Path, rng: random.Random) -> list[Employee]:
    people = _people(rng, 'US01', 'US-', 41, 'USD', 5000, 16000)
    folder = root / 'Region_AMER' / 'USA' / 'payroll' / '2026' / 'Q4' / 'october'
    folder.mkdir(parents=True)
    with open(folder / 'us01_payroll_register_2026-10.csv', 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['EE_ID', 'EE_NAME', 'DEPT', 'GROSS_BASE_USD', 'STIPEND_USD', 'STATE', 'FLSA'])
        for person in people:
            person.allowances = float(rng.choice([0, 100, 200]))
            writer.writerow(
                [
                    person.emp_id,
                    person.name.upper(),
                    person.department.upper(),
                    f'{person.base:.2f}',
                    f'{person.allowances:.2f}',
                    rng.choice(['CA', 'NY', 'TX', 'WA']),
                    rng.choice(['Exempt', 'Non-exempt']),
                ]
            )
    return people


def build_reference(root: Path, rng: random.Random):
    finance = root / 'Group Finance' / 'Reference Data'
    finance.mkdir(parents=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Read me'
    ws.append(['Rates are month-average USD per 1 unit of currency. Use the column for the payroll month.'])
    rates = wb.create_sheet('FX Rates 2026')
    _header(rates, ['Currency', 'Sep-2026', 'Oct-2026', 'Nov-2026'])
    for currency in ['SGD', 'MYR', 'GBP', 'EUR', 'USD', 'JPY']:
        rates.append(
            [currency, FX_SEP.get(currency, 0.0067), FX_OCT.get(currency, 0.0068), FX_NOV.get(currency, 0.0066)]
        )
    wb.save(finance / 'fx_rates_2026.xlsx')

    ledger_dir = root / 'Group Finance' / 'GL' / 'exports'
    ledger_dir.mkdir(parents=True)
    ledger = Workbook()
    ws = ledger.active
    ws.title = 'GL Detail'
    columns = [
        'Journal',
        'Line',
        'Date',
        'Account',
        'Account Name',
        'Entity',
        'Cost Centre',
        'Debit',
        'Credit',
        'Currency',
        'Description',
        'Source',
        'Batch',
        'Period',
        'Approved By',
        'Posted',
        'Ref1',
        'Ref2',
        'Ref3',
        'Project',
        'Tax Code',
        'Intercompany',
        'Reversal',
        'Created',
        'Modified',
    ]
    _header(ws, columns)
    for row in range(6000):
        ws.append(
            [
                f'J{row // 4:05d}',
                row % 4 + 1,
                '2026-10-15',
                rng.randint(4000, 8999),
                'Expense',
                rng.choice(['SG01', 'MY01', 'UK01', 'DE01', 'US01']),
                f'CC-{rng.randint(100, 999)}',
                rng.randint(0, 9000),
                0,
                'USD',
                'Accrual',
                'AP',
                f'B{rng.randint(1, 80)}',
                '2026-10',
                'system',
                'Y',
                '',
                '',
                '',
                '',
                'T1',
                'N',
                'N',
                '2026-10-16',
                '2026-10-16',
            ]
        )
    for name in ['Accounts', 'Entities', 'Periods']:
        extra = ledger.create_sheet(name)
        extra.append([name, 'reference list'])
    ledger.save(ledger_dir / 'GL_detail_Oct2026.xlsx')

    misc = root / 'Group Finance' / 'Instructions'
    misc.mkdir(parents=True)
    (misc / 'October close instructions.txt').write_text(
        'Group payroll consolidation - October 2026\n\n'
        '1. Consolidate every active employee of SG01, MY01, UK01, DE01 and US01 into the Employees tab.\n'
        '2. Use only FINAL submissions. Drafts and superseded versions must be ignored.\n'
        '3. Exclude employees whose leaving date is on or before 31 October 2026. Include joiners who start in October.\n'
        '4. Allowances are the total of all fixed allowances for the month (car, transport, meal, phone, housing, shift, stipend, Zulagen).\n'
        '5. Convert base salary to USD with the October 2026 month-average rate. Round to 2 decimals.\n'
        '6. Department names must be in English: Sales, Operations, Finance, Engineering, People.\n'
        '7. Fill the File Map with every file in this packet and its role.\n',
        encoding='utf-8',
    )
    (root / 'Region_APAC' / 'Singapore' / 'Thumbs.db').write_bytes(b'\x00' * 64)


def build_template(path: Path):
    wb = Workbook()
    employees = wb.active
    employees.title = 'Employees'
    _header(
        employees,
        [
            'Entity',
            'Employee ID',
            'Employee Name',
            'Department',
            'Currency',
            'Base Salary (Local)',
            'Allowances (Local)',
            'FX Rate to USD',
            'Base Salary (USD)',
        ],
    )
    summary = wb.create_sheet('Entity Summary')
    _header(summary, ['Entity', 'Headcount', 'Total Base (USD)'])
    for row, entity in enumerate(['SG01', 'MY01', 'UK01', 'DE01', 'US01'], start=2):
        summary.append([entity, f'=COUNTIF(Employees!A:A,A{row})', f'=SUMIF(Employees!A:A,A{row},Employees!I:I)'])
    summary.append(['Total', '=SUM(B2:B6)', '=SUM(C2:C6)'])
    file_map = wb.create_sheet('File Map')
    _header(file_map, ['Relative Path', 'File Type', 'Sheets / Parts', 'Role', 'Entity', 'Notes'])
    lists = wb.create_sheet('Lists')
    lists.append(['Role', 'Department', 'Currency'])
    roles = ['payroll input', 'adjustment input', 'reference', 'superseded', 'instructions', 'not relevant']
    for role, department, currency in zip(
        roles, DEPARTMENTS + [None], ['SGD', 'MYR', 'GBP', 'EUR', 'USD', None], strict=True
    ):
        lists.append([role, department, currency])
    lists.sheet_state = 'hidden'

    def dropdown(ws, cells, source):
        rule = DataValidation(type='list', formula1=source, allow_blank=False)
        rule.add(cells)
        ws.add_data_validation(rule)

    dropdown(employees, 'A2:A5000', "'Entity Summary'!$A$2:$A$6")
    dropdown(employees, 'D2:D5000', 'Lists!$B$2:$B$6')
    dropdown(employees, 'E2:E5000', 'Lists!$C$2:$C$6')
    dropdown(file_map, 'D2:D1000', 'Lists!$A$2:$A$7')
    numbers = DataValidation(type='decimal', operator='greaterThanOrEqual', formula1='0', allow_blank=False)
    numbers.add('F2:I5000')
    employees.add_data_validation(numbers)
    wb.save(path)


def build(out: Path, seed: int = 20261031) -> dict:
    """Write packet/, packet.zip, output_template.xlsx and answer_key.json under out/."""
    rng = random.Random(seed)
    packet = out / 'packet'
    if packet.exists():
        raise SystemExit(f'{packet} already exists; choose an empty folder')
    packet.mkdir(parents=True)
    people = []
    for builder in (build_singapore, build_malaysia, build_uk, build_germany, build_us):
        people += builder(packet, rng)
    build_reference(packet, rng)
    build_template(out / 'output_template.xlsx')
    with zipfile.ZipFile(out / 'packet.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(packet.rglob('*')):
            if file.is_file():
                archive.write(file, file.relative_to(out).as_posix())
    rows = []
    for person in people:
        if not person.included:
            continue
        rate = FX_OCT[person.currency]
        rows.append(
            {
                'entity': person.entity,
                'employee_id': person.emp_id,
                'name': person.name,
                'department': person.department,
                'currency': person.currency,
                'base_local': person.base,
                'allowances_local': round(person.allowances, 2),
                'fx_rate': rate,
                'base_usd': round(person.base * rate, 2),
            }
        )
    files = sorted(file.relative_to(packet).as_posix() for file in packet.rglob('*') if file.is_file())
    key = {
        'employees': rows,
        'files': files,
        'roles': {
            'Region_APAC/Singapore/Payroll/2026/10_October/SG01_Payroll_Oct2026_v1_DRAFT_superseded.xlsx': 'superseded',
            'Group Finance/GL/exports/GL_detail_Oct2026.xlsx': 'not relevant',
            'Group Finance/Reference Data/fx_rates_2026.xlsx': 'reference',
            'Group Finance/Instructions/October close instructions.txt': 'instructions',
        },
        'excluded_leavers': [p.emp_id for p in people if not p.included],
    }
    (out / 'answer_key.json').write_text(json.dumps(key, indent=2), encoding='utf-8')
    return key


if __name__ == '__main__':
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else 'runs/fixture')
    result = build(target)
    print(f'{len(result["files"])} files, {len(result["employees"])} expected employee rows -> {target}')
