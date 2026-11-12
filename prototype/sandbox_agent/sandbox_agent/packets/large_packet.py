"""Generate a large synthetic payroll packet that tests speed and parallel scouting, and its answer key.

The packet holds twelve monthly payroll registers (November 2025 to October 2026) for ten entities, nested as
<Region>/<Country>/<Entity>/<Year>/<MM_Month>/, mixing xlsx and large csv files (well over 50 MB uncompressed). The
goal needs only the October 2026 registers of TH01 and VN01, plus one entity-code mapping workbook for currencies.

Traps:
- volume: 120+ files and 50+ MB, so the agent must profile files by name and shape instead of reading them all
- TH01's October folder holds a superseded "_v1" copy beside the "_FINAL" register; v1 has stale salaries for some
  employees and lacks the October joiners
- TH01's register has no currency column; the currency comes from the entity-code mapping reference workbook
- VN01's October register has its header on row 4 under a three-line title block
- VN01's October register still lists 3 employees with Status "Terminated" and a termination date in September 2026;
  they are not active and must be excluded
- TH02's October register is named "TH1_..." (its legacy payroll code), confusingly close to TH01; it is not relevant
- earlier months differ from October (joiners, leavers, an April raise and October promotions), so a wrong month
  gives wrong rows and wrong salaries

Everything is invented. The answer key is written outside the packet, so the agent never sees it.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .common import packet_files, write_json, zip_packet

MONTHS = [(2025, 11), (2025, 12)] + [(2026, m) for m in range(1, 11)]  # index 0 = Nov 2025, 11 = Oct 2026
OCTOBER = 11
RAISE_MONTH = 5  # April 2026
MONTH_NAMES = 'January February March April May June July August September October November December'.split()

FIRST = (
    'An Binh Chai Dao Duc Hanh Hoa Kanya Khanh Lan Linh Mai Malee Minh Nam Narin Ngoc Niran Phuong Pim Quang Somchai '
    'Somsak Suda Thanh Thao Tuan Vy Wichai Yen Arif Bayu Dewi Joy Liam Mia Noah Ravi Sari Tama'
).split()
LAST = (
    'Nguyen Tran Le Pham Hoang Vo Dang Bui Srisuk Chaiyaporn Wongsa Rattana Boonmee Kittisak Saelim Tan Lim Ong Reyes '
    'Santos Cruz Wijaya Putra Hartono Smith Walker Ngata Patel Kumar Lee Chen Wong'
).split()
DEPARTMENTS = ['Sales', 'Operations', 'Finance', 'Engineering', 'People', 'Logistics', 'Customer Service']
POSITIONS = ['Associate', 'Senior Associate', 'Specialist', 'Team Lead', 'Manager', 'Analyst', 'Coordinator']


@dataclass(frozen=True)
class Entity:
    code: str
    region: str
    country: str
    currency: str
    kind: str  # xlsx or csv
    staff: int
    low: int
    high: int
    step: int
    legal_name: str
    legacy: str


ENTITIES = [  # one line per entity, kept wide so the table reads as a table
    Entity('TH01', 'Southeast Asia', 'Thailand', 'THB', 'xlsx', 236, 18000, 165000, 500, 'Example Thailand Co., Ltd.', 'THA'),
    Entity('TH02', 'Southeast Asia', 'Thailand', 'THB', 'csv', 2300, 15000, 140000, 500, 'Example Thai Services Ltd.', 'TH1'),
    Entity('VN01', 'Southeast Asia', 'Vietnam', 'VND', 'xlsx', 184, 9_000_000, 85_000_000, 100_000, 'Example Vietnam Co., Ltd.', 'VNM'),
    Entity('VN02', 'Southeast Asia', 'Vietnam', 'VND', 'csv', 2100, 8_000_000, 70_000_000, 100_000, 'Example Vietnam Logistics JSC', 'VN2'),
    Entity('SG01', 'Southeast Asia', 'Singapore', 'SGD', 'xlsx', 700, 3800, 14000, 50, 'Example Singapore Pte. Ltd.', 'SGP'),
    Entity('MY01', 'Southeast Asia', 'Malaysia', 'MYR', 'csv', 2300, 3000, 18000, 50, 'Example Malaysia Sdn. Bhd.', 'MYS'),
    Entity('ID01', 'Southeast Asia', 'Indonesia', 'IDR', 'csv', 2500, 6_000_000, 60_000_000, 100_000, 'PT Example Indonesia', 'IDN'),
    Entity('PH01', 'Southeast Asia', 'Philippines', 'PHP', 'csv', 2300, 22000, 180000, 500, 'Example Philippines Inc.', 'PHL'),
    Entity('AU01', 'Oceania', 'Australia', 'AUD', 'xlsx', 650, 5200, 16000, 50, 'Example Australia Pty Ltd', 'AUS'),
    Entity('NZ01', 'Oceania', 'New Zealand', 'NZD', 'csv', 1900, 4800, 14500, 50, 'Example New Zealand Limited', 'NZL'),
]  # fmt: skip

BIG_COLUMNS = [
    'Entity', 'Employee ID', 'Employee Name', 'Department', 'Job Title', 'Grade', 'Cost Centre', 'Location',
    'Hire Date', 'Pay Period', 'Currency', 'Base Salary', 'Housing Allowance', 'Transport Allowance',
    'Meal Allowance', 'Overtime Hours', 'Overtime Pay', 'Bonus', 'Gross Pay', 'Employee Tax',
    'Employee Social', 'Employer Social', 'Pension EE', 'Pension ER', 'Net Pay', 'Bank Code', 'Bank Account',
    'Payment Date', 'Manager ID', 'Work Pattern', 'FTE', 'Remarks',
]  # fmt: skip
REMARKS = [
    'Standard monthly payroll run; no adjustments recorded for this period.',
    'Regular payroll; allowances per contract schedule, overtime approved by line manager.',
    'Monthly run processed by payroll bureau; see bureau control report for totals.',
    'No change from prior period apart from statutory contribution updates.',
]
TH01_COLUMNS = [
    'Emp Code', 'Employee Name', 'Department', 'Position', 'Base Salary', 'Position Allowance', 'OT Hours',
    'OT Amount', 'Social Security', 'Withholding Tax', 'Net Pay', 'Bank Account', 'Hire Date',
]  # fmt: skip
VN01_COLUMNS = [
    'Employee No', 'Full Name', 'Department', 'Status', 'Termination Date', 'Basic Salary', 'Allowance',
    'Social Insurance', 'PIT', 'Net Salary', 'Bank Account', 'Join Date',
]  # fmt: skip


@dataclass
class Person:
    emp_id: str
    name: str
    department: str
    position: str
    hire: date
    start: int  # first month index on the register (-1 = before the window)
    leave: int | None  # first month index no longer on the register
    base: float
    raise_pct: float = 0.0
    promo_pct: float = 0.0
    static: dict = field(default_factory=dict)

    def on_register(self, month: int) -> bool:
        return self.start <= month and (self.leave is None or month < self.leave)

    def salary(self, month: int, step: int) -> float:
        value = self.base
        if month >= RAISE_MONTH and self.raise_pct:
            value = round(value * (1 + self.raise_pct) / step) * step
        if month >= OCTOBER and self.promo_pct:
            value = round(value * (1 + self.promo_pct) / step) * step
        return float(value)


def _people(rng: random.Random, entity: Entity) -> list[Person]:
    prefix = {'TH01': 'TH', 'VN01': 'VN', 'TH02': 'TS', 'VN02': 'VL'}.get(entity.code, entity.code[:2])
    first_number = rng.randint(10000, 40000)
    people = []
    total = entity.staff + max(6, entity.staff // 20)  # extra people join or leave during the year
    for index in range(total):
        start, leave = -1, None
        roll = rng.random()
        if index >= entity.staff:
            start = rng.randint(1, OCTOBER)  # a joiner during the window
        elif roll < 0.05:
            leave = rng.randint(1, OCTOBER)  # a leaver during the window
        hire_year = 2026 if start >= 2 else (2025 if start >= 0 else rng.randint(2012, 2024))
        hire_month = MONTHS[start][1] if start >= 0 else rng.randint(1, 12)
        people.append(
            Person(
                emp_id=f'{prefix}{first_number + index}',
                name=f'{rng.choice(FIRST)} {rng.choice(LAST)}',
                department=rng.choice(DEPARTMENTS),
                position=rng.choice(POSITIONS),
                hire=date(hire_year, hire_month, rng.randint(1, 28)),
                start=start,
                leave=leave,
                base=float(rng.randrange(entity.low, entity.high, entity.step)),
                raise_pct=rng.choice([0, 0, 0.03, 0.04, 0.05, 0.06]),
                promo_pct=0.12 if rng.random() < 0.02 else 0.0,
                static={
                    'Grade': rng.choice(['G2', 'G3', 'G4', 'G5', 'G6', 'M1', 'M2']),
                    'Cost Centre': f'CC-{rng.randint(1000, 9999)}',
                    'Location': rng.choice(['HQ', 'Branch 1', 'Branch 2', 'Warehouse', 'Remote']),
                    'Bank Code': f'B{rng.randint(100, 999)}',
                    'Bank Account': f'{rng.randint(10**9, 10**10 - 1)}',
                    'Manager ID': f'{prefix}{first_number + rng.randint(0, 40)}',
                    'Work Pattern': rng.choice(['5x8', '4x10', 'Shift A', 'Shift B', 'Flexi']),
                    'FTE': rng.choice([1, 1, 1, 1, 0.8, 0.5]),
                },
            )
        )
    return people


def _folder(root: Path, entity: Entity, month: int) -> Path:
    year, number = MONTHS[month]
    folder = root / entity.region / entity.country / entity.code / str(year) / f'{number:02d}_{MONTH_NAMES[number - 1]}'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _label(month: int) -> str:
    year, number = MONTHS[month]
    return f'{MONTH_NAMES[number - 1][:3]}{year}'


def _big_row(rng: random.Random, entity: Entity, person: Person, month: int) -> list:
    year, number = MONTHS[month]
    base = person.salary(month, entity.step)
    housing = round(base * rng.choice([0, 0, 0.05, 0.1]), 2)
    transport = round(base * rng.choice([0, 0.02, 0.03]), 2)
    meal = round(base * 0.015, 2)
    hours = rng.randint(0, 24)
    overtime = round(base / 173 * 1.5 * hours, 2)
    bonus = round(base * 0.5, 2) if number == 12 and rng.random() < 0.3 else 0.0
    gross = round(base + housing + transport + meal + overtime + bonus, 2)
    tax = round(gross * 0.08, 2)
    social = round(min(gross, entity.high) * 0.05, 2)
    pension = round(base * 0.03, 2)
    net = round(gross - tax - social - pension, 2)
    s = person.static
    return [
        entity.code, person.emp_id, person.name, person.department, person.position, s['Grade'], s['Cost Centre'],
        s['Location'], person.hire.isoformat(), f'{year}-{number:02d}', entity.currency, f'{base:.2f}',
        f'{housing:.2f}', f'{transport:.2f}', f'{meal:.2f}', hours, f'{overtime:.2f}', f'{bonus:.2f}', f'{gross:.2f}',
        f'{tax:.2f}', f'{social:.2f}', f'{round(gross * 0.065, 2):.2f}', f'{pension:.2f}', f'{pension:.2f}',
        f'{net:.2f}', s['Bank Code'], s['Bank Account'], f'{year}-{number:02d}-25', s['Manager ID'],
        s['Work Pattern'], s['FTE'], rng.choice(REMARKS),
    ]  # fmt: skip


def _write_big(rng: random.Random, root: Path, entity: Entity, people: list[Person], month: int) -> Path:
    folder = _folder(root, entity, month)
    name = f'{entity.code}_Payroll_Register_{_label(month)}'
    if entity.code == 'TH02' and month == OCTOBER:
        name = f'TH1_Payroll_Register_{_label(month)}_FINAL'  # the bureau's legacy code, close to TH01
    rows = [_big_row(rng, entity, p, month) for p in people if p.on_register(month)]
    if entity.kind == 'csv':
        path = folder / f'{name}.csv'
        with open(path, 'w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(BIG_COLUMNS)
            writer.writerows(rows)
        return path
    path = folder / f'{name}.xlsx'
    wb = Workbook(write_only=True)
    ws = wb.create_sheet('Register')
    ws.append(BIG_COLUMNS)
    numeric = {BIG_COLUMNS.index(c) for c in BIG_COLUMNS[11:25] if c != 'Overtime Hours'}
    for row in rows:
        ws.append([float(v) if i in numeric else v for i, v in enumerate(row)])
    wb.save(path)
    return path


def _th01_register(rng: random.Random, path: Path, entity: Entity, rows: list[tuple[Person, float]]):
    wb = Workbook(write_only=True)
    ws = wb.create_sheet('Payroll')
    ws.append(TH01_COLUMNS)
    for person, base in rows:
        allowance = float(rng.choice([0, 1000, 2000, 3500]))
        hours = rng.randint(0, 20)
        overtime = round(base / 240 * 1.5 * hours, 2)
        social = 750.0 if base >= 15000 else round(base * 0.05, 2)
        tax = round((base + allowance) * 0.06, 2)
        ws.append([
            person.emp_id, person.name, person.department, person.position, base, allowance, hours, overtime,
            social, tax, round(base + allowance + overtime - social - tax, 2), person.static['Bank Account'],
            person.hire,
        ])  # fmt: skip
    summary = wb.create_sheet('Control')
    summary.append(['Entity', 'TH01'])
    summary.append(['Headcount', len(rows)])
    summary.append(['Total base', sum(base for _, base in rows)])
    wb.save(path)


def build_th01(rng: random.Random, root: Path, entity: Entity, people: list[Person]) -> tuple[list[dict], str, str]:
    # October differs from September: three joiners start in October and three people left at the end of September.
    for person in rng.sample([p for p in people if p.start > 0], 3):
        person.start, person.hire = OCTOBER, date(2026, 10, rng.randint(1, 15))
    for person in rng.sample([p for p in people if p.start < 0 and p.leave is None], 3):
        person.leave = OCTOBER
    for month in range(len(MONTHS)):
        rows = [(p, p.salary(month, entity.step)) for p in people if p.on_register(month)]
        folder = _folder(root, entity, month)
        if month < OCTOBER:
            _th01_register(rng, folder / f'TH01_Payroll_Register_{_label(month)}.xlsx', entity, rows)
            continue
        final = folder / f'TH01_Payroll_Register_{_label(month)}_FINAL.xlsx'
        draft = folder / f'TH01_Payroll_Register_{_label(month)}_v1.xlsx'
        _th01_register(rng, final, entity, rows)
        stale = set(rng.sample(range(len(rows)), 8))
        draft_rows = [
            (p, base - entity.step * (6 + i % 5) if i in stale else base)
            for i, (p, base) in enumerate(rows)
            if p.start != OCTOBER  # v1 was cut before the October joiners were keyed in
        ]
        _th01_register(rng, draft, entity, draft_rows)
        answer = [
            {'Entity': 'TH01', 'Employee ID': p.emp_id, 'Name': p.name, 'Department': p.department,
             'Base Salary': base, 'Currency': 'THB'}
            for p, base in rows
        ]  # fmt: skip
        return answer, final.relative_to(root).as_posix(), draft.relative_to(root).as_posix()
    raise AssertionError('unreachable')


def build_vn01(
    rng: random.Random, root: Path, entity: Entity, people: list[Person]
) -> tuple[list[dict], str, list[str]]:
    # Three people terminated in September 2026 still appear on the October register, marked Terminated.
    candidates = [p for p in people if p.start < 9 and p.leave is None]
    terminated = rng.sample(candidates, 3)
    for person, day in zip(terminated, (12, 20, 30), strict=True):
        person.static['terminated'] = date(2026, 9, day)
    answer, used = [], ''
    for month in range(len(MONTHS)):
        year, number = MONTHS[month]
        path = _folder(root, entity, month) / f'VN01_Bang_Luong_{year}{number:02d}.xlsx'
        wb = Workbook(write_only=True)
        ws = wb.create_sheet('Payroll')
        ws.append([f'{entity.legal_name} - Monthly payroll register'])
        ws.append([f'Period: {MONTH_NAMES[number - 1]} {year}'])
        ws.append([f'Currency: {entity.currency}    Prepared by: payroll bureau'])
        ws.append(VN01_COLUMNS)
        for person in people:
            if not person.on_register(month):
                continue
            base = person.salary(month, entity.step)
            ended = person.static.get('terminated')
            status = 'Terminated' if ended and month == OCTOBER else 'Active'
            allowance = float(rng.choice([0, 500_000, 1_000_000, 1_500_000]))
            insurance = round(base * 0.105, 0)
            pit = round(max(0.0, base + allowance - insurance - 11_000_000) * 0.1, 0)
            ws.append([
                person.emp_id, person.name, person.department, status, ended if status == 'Terminated' else None,
                base, allowance, insurance, pit, base + allowance - insurance - pit, person.static['Bank Account'],
                person.hire,
            ])  # fmt: skip
            if month == OCTOBER and status == 'Active':
                answer.append(
                    {'Entity': 'VN01', 'Employee ID': person.emp_id, 'Name': person.name,
                     'Department': person.department, 'Base Salary': base, 'Currency': 'VND'}
                )  # fmt: skip
        wb.save(path)
        if month == OCTOBER:
            used = path.relative_to(root).as_posix()
    return answer, used, [p.emp_id for p in terminated]


def build_mapping(root: Path) -> str:
    folder = root / 'Group' / 'Reference Data'
    folder.mkdir(parents=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Entity Codes'
    ws.append(['Entity Code', 'Legal Name', 'Region', 'Country', 'Functional Currency', 'Legacy Payroll Code',
               'Register Format'])  # fmt: skip
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')
    for e in ENTITIES:
        ws.append([e.code, e.legal_name, e.region, e.country, e.currency, e.legacy, e.kind])
    notes = wb.create_sheet('Notes')
    notes.append(['Legacy payroll codes still appear in some bureau file names. TH1 is TH02, not TH01.'])
    notes.append(['Payroll registers report amounts in the functional currency of the entity.'])
    path = folder / 'Entity_Code_Mapping_2026.xlsx'
    wb.save(path)
    return path.relative_to(root).as_posix()


def build_template(path: Path):
    wb = Workbook()
    employees = wb.active
    employees.title = 'Employees'
    employees.append(['Entity', 'Employee ID', 'Name', 'Department', 'Base Salary', 'Currency'])
    totals = wb.create_sheet('Totals')
    totals.append(['Entity', 'Currency', 'Headcount', 'Total Base Salary'])
    for row, (entity, currency) in enumerate([('TH01', 'THB'), ('VN01', 'VND')], start=2):
        count, total = f'=COUNTIF(Employees!A:A,A{row})', f'=SUMIF(Employees!A:A,A{row},Employees!E:E)'
        totals.append([entity, currency, count, total])
    lists = wb.create_sheet('Lists')
    lists.append(['Entity', 'Currency'])
    lists.append(['TH01', 'THB'])
    lists.append(['VN01', 'VND'])
    lists.sheet_state = 'hidden'
    for ws in (employees, totals):
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill('solid', fgColor='DDEBF7')
    for cells, source in (('A2:A5000', 'Lists!$A$2:$A$3'), ('F2:F5000', 'Lists!$B$2:$B$3')):
        rule = DataValidation(type='list', formula1=source, allow_blank=False)
        rule.add(cells)
        employees.add_data_validation(rule)
    numbers = DataValidation(type='decimal', operator='greaterThanOrEqual', formula1='0', allow_blank=False)
    numbers.add('E2:E5000')
    employees.add_data_validation(numbers)
    wb.save(path)


GOAL = (
    'Prepare the October 2026 headcount and base salary summary for entities TH01 and VN01 only. Fill the Employees '
    'sheet of the template with one row per active employee (Entity, Employee ID, Name, Department, Base Salary in '
    'local currency, Currency) and leave the Totals formulas to calculate. The packet holds a year of payroll '
    'registers for many entities; use only what this goal needs.'
)


def build(out: Path, seed: int = 20261031) -> dict:
    """Write packet/, packet.zip, headcount_template.xlsx, job.json and answer_key.json under out/."""
    rng = random.Random(seed)
    packet = out / 'packet'
    if packet.exists():
        raise SystemExit(f'{packet} already exists; choose an empty folder')
    packet.mkdir(parents=True)
    rows, used, not_relevant, terminated = [], [], [], []
    for entity in ENTITIES:
        people = _people(rng, entity)
        if entity.code == 'TH01':
            answer, final, draft = build_th01(rng, packet, entity, people)
            rows += answer
            used.append(final)
            not_relevant.append(draft)
        elif entity.code == 'VN01':
            answer, final, terminated = build_vn01(rng, packet, entity, people)
            rows += answer
            used.append(final)
        else:
            for month in range(len(MONTHS)):
                _write_big(rng, packet, entity, people, month)
    reference = build_mapping(packet)
    build_template(out / 'headcount_template.xlsx')
    zip_packet(packet, out / 'packet.zip')
    decisions = {f: 'not_relevant' for f in packet_files(packet)}
    decisions.update({f: 'used' for f in used})
    decisions[reference] = 'reference'
    key = {
        'deliverables': {
            'headcount_summary.xlsx': {
                'sheets': {
                    'Employees': {
                        'key': ['Entity', 'Employee ID'],
                        'fields': ['Name', 'Department', 'Base Salary', 'Currency'],
                        'rows': rows,
                    }
                }
            }
        },
        'files': decisions,
        'questions': [],
        'excluded_terminated': terminated,
        'superseded': not_relevant,
    }
    write_json(out / 'answer_key.json', key)
    write_json(
        out / 'job.json',
        {
            'id': 'large_packet',
            'module': 'payroll',
            'title': 'TH01 and VN01 headcount and base salary, October 2026',
            'goal': GOAL,
            'zip': 'packet.zip',
            'deliverables': [{'name': 'headcount_summary.xlsx', 'template': 'headcount_template.xlsx'}],
        },
    )
    return key
