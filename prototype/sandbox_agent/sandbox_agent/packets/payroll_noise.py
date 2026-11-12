"""A November 2026 payroll changes packet for MY02, buried in look-alike noise.

Everything is invented. 28 files, of which 5 are used and 1 is reference; the rest look similar but are not relevant.

Traps:
- files for other entities (MY01, SG01) and other months (September, October 2026) with the same names and layouts
- the overtime amounts sit on a HIDDEN sheet ("OT_Calc") of the MY02 attendance workbook; the visible sheet has none
- one salary change appears in both the HR export and the change log; it belongs in the output once
- the HR export has a pay change effective 1 December 2026, which is not a November change
- a nested zip, inputs/nov_late_items.zip, holds a csv with a late allowance and a deduction; the nested zip is the
  file the document map judges (used). A sibling zip for MY01 is not relevant
- a loan deduction arrives only by email; a near-identical email for MY01 is not relevant
- an archive zip of old submissions, last month's submitted changes, a GL export, a png, Thumbs.db and an Excel lock
  file (~$lock.xlsx) are not relevant
- the policy pdf is reference only
"""

from __future__ import annotations

import csv
import io
import random
import struct
import zipfile
import zlib
from datetime import date
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.datavalidation import DataValidation

from .common import header, packet_files, write_json, write_pdf, zip_packet

FIRST = 'Aina Bao Chen Dian Eshan Farah Gopal Hana Irfan Jia Kumar Lina Mei Nadia Omar Priya Rahim Siti Tan Wei Yusof Zara'.split()
LAST = 'Abdullah Chong Das Fernandez Goh Hassan Ibrahim Krishnan Lee Lim Menon Ng Ong Pillai Rahman Salleh Tan Wong Yap Zainal'.split()
CHANGE_TYPES = ['Joiner', 'Leaver', 'Salary Change', 'Allowance', 'Overtime', 'Deduction']
COLUMNS = ['Employee ID', 'Name', 'Change Type', 'Effective Date', 'Amount', 'Source Note']
HR_EVENT = {'Joiner': 'New Hire', 'Leaver': 'Termination', 'Salary Change': 'Pay Change'}
TEMPLATE = 'payroll_changes_template.xlsx'
DELIVERABLE = 'payroll_changes.xlsx'
GOAL = (
    'Prepare the November 2026 payroll changes for entity MY02 only. Fill the Changes sheet of the template with '
    'every joiner, leaver, salary change, allowance, overtime payment and deduction for MY02 that takes effect in '
    'November 2026, one row per employee and change type, following the Read Me sheet.'
)

HR_EXPORT = 'MY02/2026-11/HR_export_MY02_Nov2026.xlsx'
CHANGE_LOG = 'MY02/2026-11/Change_log_MY02_Nov2026.csv'
ATTENDANCE = 'MY02/2026-11/Timesheets/MY02_Nov2026_attendance.xlsx'
LOAN_EMAIL = 'MY02/2026-11/Emails/Loan deduction - MY02 November.eml'
NESTED = 'inputs/nov_late_items.zip'
POLICY = 'Policies/MY_Payroll_Allowance_and_Overtime_Policy_2026.pdf'


def build_template(path: Path):
    wb = Workbook()
    readme = wb.active
    readme.title = 'Read Me'
    for line in [
        'MY02 monthly payroll changes',
        'One row per employee and change type. Do not repeat a change that appears in more than one source.',
        'Effective Date as YYYY-MM-DD. Overtime is effective on the last day of the month.',
        'Amount in MYR as a plain number: Joiner = monthly salary; Leaver = monthly salary at leaving;',
        'Salary Change = new monthly salary; Allowance, Overtime, Deduction = the amount for the month (positive).',
        'Source Note: a few words naming where the change came from.',
    ]:
        readme.append([line])
    ws = wb.create_sheet('Changes')
    header(ws, COLUMNS)
    lists = wb.create_sheet('Lists')
    lists.append(['Change Type'])
    for value in CHANGE_TYPES:
        lists.append([value])
    lists.sheet_state = 'hidden'
    dropdown = DataValidation(type='list', formula1=f'Lists!$A$2:$A${len(CHANGE_TYPES) + 1}', allow_blank=False)
    dropdown.add('C2:C1000')
    ws.add_data_validation(dropdown)
    amount = DataValidation(type='decimal', operator='greaterThanOrEqual', formula1='0', allow_blank=False)
    amount.add('E2:E1000')
    ws.add_data_validation(amount)
    wb.save(path)


def _staff(rng: random.Random, prefix: str, count: int, low: int, high: int) -> list[dict]:
    names = set()
    people = []
    while len(people) < count:
        name = f'{rng.choice(FIRST)} {rng.choice(LAST)}'
        if name in names:
            continue
        names.add(name)
        people.append({'id': f'{prefix}{len(people) + 1:03d}', 'name': name,
                       'salary': float(rng.randrange(low, high, 50))})  # fmt: skip
    return people


def _short(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime('%d-%b-%Y')


def _hr_export(path: Path, entity: str, period: str, events: list[dict], staff: list[dict]):
    """events: {'p', 'type' (Joiner/Leaver/Salary Change), 'date', 'amount', 'previous'}."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Events'
    ws.append([f'HR system export - {entity} - {period}'])
    ws.append([])
    header(ws, ['Entity', 'Employee ID', 'Employee Name', 'Event', 'Effective Date', 'Monthly Salary (MYR)',
                'Previous Salary (MYR)', 'Comment'])  # fmt: skip
    for e in events:
        ws.append([entity, e['p']['id'], e['p']['name'], HR_EVENT[e['type']], date.fromisoformat(e['date']),
                   e['amount'], e.get('previous'), e.get('comment', '')])  # fmt: skip
    for row in ws.iter_rows(min_row=4, min_col=5, max_col=5):
        for cell in row:
            cell.number_format = 'yyyy-mm-dd'
    active = wb.create_sheet('Active Staff')
    header(active, ['Employee ID', 'Employee Name', 'Monthly Salary (MYR)'])
    for p in staff:
        active.append([p['id'], p['name'], p['salary']])
    wb.save(path)


def _change_log(path: Path, entity: str, items: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['log_id', 'entity', 'emp_id', 'emp_name', 'change', 'effective', 'value_myr', 'approved_by', 'note'])
        for n, item in enumerate(items, start=1):
            writer.writerow([f'CL-{entity}-{n:03d}', entity, item['p']['id'], item['p']['name'], item['change'],
                             _short(item['date']), f'{item["amount"]:.2f}', item.get('by', 'HR Manager'),
                             item.get('note', '')])  # fmt: skip


def _attendance(path: Path, entity: str, period: str, staff: list[dict], overtime: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Summary'
    ws.append([f'Attendance summary - {entity} - {period}'])
    ws.append([])
    header(ws, ['Employee ID', 'Employee Name', 'Days Worked', 'Leave Days'])
    for p in staff:
        ws.append([p['id'], p['name'], 21 - p.get('leave', 0), p.get('leave', 0)])
    ot = wb.create_sheet('OT_Calc')
    header(ot, ['Employee ID', 'Employee Name', 'OT Hours', 'OT Amount (MYR)'])
    for o in overtime:
        ot.append([o['p']['id'], o['p']['name'], o['hours'], o['amount']])
    ot.sheet_state = 'hidden'
    wb.save(path)


def _eml(path: Path, sender: str, subject: str, sent: str, body: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'From: {sender}', 'To: payroll-my@example.test', f'Subject: {subject}', f'Date: {sent}',
             'MIME-Version: 1.0', 'Content-Type: text/plain; charset="utf-8"', '', *body]  # fmt: skip
    path.write_text('\r\n'.join(lines) + '\r\n', encoding='utf-8')


def _csv_bytes(rows: list[list]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue().encode('utf-8')


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _png() -> bytes:
    """A valid 2x2 grey PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b''.join(b'\x00' + b'\x80\x80\x80' * 2 for _ in range(2))
    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(raw))
        + chunk(b'IEND', b'')
    )


def _noise_month(rng: random.Random, staff: list[dict], month: str, prefix_new: str) -> tuple[list[dict], list[dict]]:
    """Random HR events and change-log items for a month that is not the one asked for."""
    picks = rng.sample(staff, 4)
    events = [
        {'p': {'id': f'{prefix_new}{rng.randint(150, 199)}', 'name': f'{rng.choice(FIRST)} {rng.choice(LAST)}'},
         'type': 'Joiner', 'date': f'{month}-{rng.randint(1, 20):02d}', 'amount': float(rng.randrange(3500, 9000, 50))},
        {'p': picks[0], 'type': 'Leaver', 'date': f'{month}-{rng.randint(10, 28):02d}', 'amount': picks[0]['salary']},
        {'p': picks[1], 'type': 'Salary Change', 'date': f'{month}-01', 'amount': picks[1]['salary'] + 300,
         'previous': picks[1]['salary']},
    ]  # fmt: skip
    log = [
        {'p': picks[1], 'change': 'Salary change', 'date': f'{month}-01', 'amount': picks[1]['salary'] + 300},
        {'p': picks[2], 'change': 'Transport allowance', 'date': f'{month}-01', 'amount': 200.0},
        {'p': picks[3], 'change': 'Meal allowance', 'date': f'{month}-01', 'amount': 150.0},
    ]
    return events, log


def build(out: Path, seed: int = 20261031) -> dict:
    """Write packet/, packet.zip, the template, job.json and answer_key.json under out/."""
    rng = random.Random(seed)
    packet = out / 'packet'
    if packet.exists():
        raise SystemExit(f'{packet} already exists; choose an empty folder')
    packet.mkdir(parents=True)
    my02 = _staff(rng, 'MY2', 18, 3500, 12000)
    my01 = _staff(rng, 'MY1', 15, 3200, 11000)
    sg01 = _staff(rng, 'SG', 12, 4200, 12800)

    # The real MY02 November changes
    joiners = [
        {'id': 'MY2101', 'name': 'Aina Salleh', 'salary': 4800.0},
        {'id': 'MY2102', 'name': 'Gopal Menon', 'salary': 6200.0},
    ]
    s = my02
    rows: list[dict] = []

    def add(p, kind, when, amount, note):
        rows.append({'Employee ID': p['id'], 'Name': p['name'], 'Change Type': kind, 'Effective Date': when,
                     'Amount': amount, 'Source Note': note})  # fmt: skip

    hr_events = [
        {'p': joiners[0], 'type': 'Joiner', 'date': '2026-11-02', 'amount': joiners[0]['salary'], 'comment': 'Offer accepted'},
        {'p': joiners[1], 'type': 'Joiner', 'date': '2026-11-16', 'amount': joiners[1]['salary']},
        {'p': s[0], 'type': 'Leaver', 'date': '2026-11-13', 'amount': s[0]['salary'], 'comment': 'Resigned'},
        {'p': s[1], 'type': 'Leaver', 'date': '2026-11-30', 'amount': s[1]['salary'], 'comment': 'End of contract'},
        {'p': s[2], 'type': 'Salary Change', 'date': '2026-11-01', 'amount': s[2]['salary'] + 450,
         'previous': s[2]['salary'], 'comment': 'Annual review'},
        {'p': s[3], 'type': 'Salary Change', 'date': '2026-12-01', 'amount': s[3]['salary'] + 600,
         'previous': s[3]['salary'], 'comment': 'Promotion - December payroll'},
    ]  # fmt: skip
    for e in hr_events[:4]:
        add(e['p'], e['type'], e['date'], e['amount'], 'HR export')
    add(s[2], 'Salary Change', '2026-11-01', s[2]['salary'] + 450, 'HR export and change log (same change)')
    log_items = [
        {'p': s[2], 'change': 'Salary change', 'date': '2026-11-01', 'amount': s[2]['salary'] + 450, 'note': 'Annual review'},
        {'p': s[4], 'change': 'Salary change', 'date': '2026-11-01', 'amount': s[4]['salary'] + 300, 'note': 'Market adjustment'},
        {'p': s[5], 'change': 'Transport allowance', 'date': '2026-11-01', 'amount': 250.0, 'note': 'Site-based role'},
    ]  # fmt: skip
    add(s[4], 'Salary Change', '2026-11-01', s[4]['salary'] + 300, 'Change log')
    add(s[5], 'Allowance', '2026-11-01', 250.0, 'Change log: transport allowance')
    overtime = [
        {'p': s[6], 'hours': 12, 'amount': 346.15},
        {'p': s[7], 'hours': 8, 'amount': 212.40},
        {'p': s[8], 'hours': 20, 'amount': 615.38},
    ]
    for o in overtime:
        add(o['p'], 'Overtime', '2026-11-30', o['amount'], 'Attendance workbook, hidden OT_Calc sheet')
    add(s[9], 'Deduction', '2026-11-30', 500.0, 'Email: staff loan repayment')
    late = [
        {'p': s[10], 'type': 'Allowance', 'label': 'Phone allowance', 'amount': 120.0, 'date': '2026-11-01'},
        {'p': s[11], 'type': 'Deduction', 'label': 'Laptop damage recovery', 'amount': 350.0, 'date': '2026-11-30'},
    ]
    for item in late:
        add(item['p'], item['type'], item['date'], item['amount'], f'nov_late_items.zip: {item["label"].lower()}')

    # Used files
    _hr_export(packet / HR_EXPORT, 'MY02', 'November 2026', hr_events, s)
    _change_log(packet / CHANGE_LOG, 'MY02', log_items)
    s[12]['leave'] = 2
    _attendance(packet / ATTENDANCE, 'MY02', 'November 2026', s, overtime)
    _eml(
        packet / LOAN_EMAIL,
        'Finance Ops MY <finops-my@example.test>',
        'Staff loan repayment - MY02 - November',
        'Mon, 2 Nov 2026 10:05:00 +0800',
        ['Hi Payroll,', '',
         f'Please deduct MYR 500.00 from {s[9]["name"]} ({s[9]["id"]}, MY02) in the November 2026 payroll as the '
         'monthly staff loan repayment, effective 30 November 2026.', '', 'Thanks,', 'Finance Ops'],
    )  # fmt: skip
    nested_csv = _csv_bytes(
        [['entity', 'employee_id', 'name', 'item_type', 'amount_myr', 'effective_date', 'comment']]
        + [['MY02', i['p']['id'], i['p']['name'], i['label'], f'{i["amount"]:.2f}', _short(i['date']), 'Late item, approved']
           for i in late]
    )  # fmt: skip
    (packet / NESTED).parent.mkdir(parents=True, exist_ok=True)
    (packet / NESTED).write_bytes(_zip_bytes({'late_items_MY02_Nov2026.csv': nested_csv}))

    # Reference
    (packet / POLICY).parent.mkdir(parents=True, exist_ok=True)
    write_pdf(
        packet / POLICY,
        ['Malaysia payroll policy - allowances, overtime and deductions (2026)', '',
         'Applies to MY01 and MY02.',
         'Overtime is paid in the month worked at the amounts calculated by the timesheet system.',
         'Recurring allowances start from the effective date approved by HR.',
         'Deductions from pay need written approval from Finance or HR.',
         'Pay changes take effect from the date on the approved record.'],
    )  # fmt: skip

    # Not relevant: other entities and other months that look the same
    noise: list[str] = []

    def keep(path: str) -> Path:
        noise.append(path)
        (packet / path).parent.mkdir(parents=True, exist_ok=True)
        return packet / path

    for entity, staff, prefix, folder_month, label in [
        ('MY01', my01, 'MY1', '2026-11', 'November 2026'),
        ('SG01', sg01, 'SG', '2026-11', 'November 2026'),
        ('MY02', my02[12:], 'MY2', '2026-10', 'October 2026'),
        ('MY02', my02[12:], 'MY2', '2026-09', 'September 2026'),
        ('SG01', sg01, 'SG', '2026-10', 'October 2026'),
    ]:
        events, log = _noise_month(rng, staff, folder_month, prefix)
        tag = date.fromisoformat(f'{folder_month}-01').strftime('%b%Y')
        _hr_export(keep(f'{entity}/{folder_month}/HR_export_{entity}_{tag}.xlsx'), entity, label, events, staff)
        if not (entity == 'SG01' and folder_month == '2026-10'):
            _change_log(keep(f'{entity}/{folder_month}/Change_log_{entity}_{tag}.csv'), entity, log)
        if entity == 'MY01' or (entity == 'MY02' and folder_month == '2026-10'):
            ot = [{'p': p, 'hours': rng.randint(4, 16), 'amount': round(rng.uniform(120, 480), 2)} for p in staff[:2]]
            _attendance(keep(f'{entity}/{folder_month}/Timesheets/{entity}_{tag}_attendance.xlsx'), entity, label, staff, ot)
    _eml(
        keep('MY01/2026-11/Emails/Loan deduction - MY01 November.eml'),
        'Finance Ops MY <finops-my@example.test>',
        'Staff loan repayment - MY01 - November',
        'Mon, 2 Nov 2026 10:11:00 +0800',
        ['Hi Payroll,', '', f'Please deduct MYR 400.00 from {my01[3]["name"]} ({my01[3]["id"]}, MY01) in the '
         'November 2026 payroll as the monthly staff loan repayment.', '', 'Thanks,', 'Finance Ops'],
    )  # fmt: skip
    keep('inputs/nov_late_items_MY01.zip').write_bytes(
        _zip_bytes({'late_items_MY01_Nov2026.csv': _csv_bytes(
            [['entity', 'employee_id', 'name', 'item_type', 'amount_myr', 'effective_date', 'comment'],
             ['MY01', my01[5]['id'], my01[5]['name'], 'Phone allowance', '120.00', '01-Nov-2026', 'Late item, approved']]
        )})
    )  # fmt: skip
    submitted = keep('MY02/2026-10/payroll_changes_MY02_Oct2026_submitted.xlsx')
    build_template(submitted)
    wb = load_workbook(submitted)
    for p in my02[13:16]:
        wb['Changes'].append([p['id'], p['name'], 'Allowance', '2026-10-01', 200.0, 'Change log'])
    wb.save(submitted)
    old_book = Workbook()
    old_book.active.append(['Employee ID', 'Change Type', 'Amount'])
    old_book.active.append(['MY2007', 'Overtime', 180.0])
    old_xlsx = io.BytesIO()
    old_book.save(old_xlsx)
    keep('Archive/old_submissions_2025.zip').write_bytes(
        _zip_bytes({f'payroll_changes_MY02_{m}2025.xlsx': old_xlsx.getvalue() for m in ('Oct', 'Nov', 'Dec')})
    )
    keep('MY02/2026-11/Thumbs.db').write_bytes(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + bytes(504))
    keep('MY02/2026-11/~$lock.xlsx').write_bytes(b'\x0bpayroll.user' + bytes(150))
    keep('Misc/office_floorplan.png').write_bytes(_png())
    keep('Misc/Team lunch RSVP.txt').write_text(
        'Team lunch on Friday 20 November 2026, 12:30. Reply with your name and food preference by Wednesday.\n',
        encoding='utf-8',
    )
    with keep('Finance/GL/MY02_GL_Oct2026.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['journal', 'account', 'description', 'debit', 'credit'])
        for n in range(40):
            value = round(rng.uniform(100, 9000), 2)
            writer.writerow([f'JV-10-{n:03d}', rng.choice(['6100', '6110', '2100', '1010']), 'Payroll accrual', value, 0])
    write_pdf(
        keep('HR/Benefits/Medical_insurance_renewal_2027.pdf'),
        ['Group medical insurance renewal - policy year 2027', 'Premiums change from 1 January 2027.',
         'No payroll deductions apply in 2026.'],
    )  # fmt: skip
    assets = Workbook()
    assets.active.title = 'Assets'
    header(assets.active, ['Asset Tag', 'Type', 'Assigned To'])
    for n in range(12):
        assets.active.append([f'IT-{n + 100}', rng.choice(['Laptop', 'Monitor', 'Phone']), rng.choice(my02)['name']])
    assets.save(keep('Misc/IT_asset_register.xlsx'))

    build_template(out / TEMPLATE)
    zip_packet(packet, out / 'packet.zip')

    decisions = {f: 'used' for f in packet_files(packet)}
    decisions[POLICY] = 'reference'
    for path in noise:
        decisions[path] = 'not_relevant'
    key = {
        'deliverables': {
            DELIVERABLE: {
                'sheets': {
                    'Changes': {
                        'key': ['Employee ID', 'Change Type'],
                        'fields': ['Name', 'Effective Date', 'Amount'],
                        'rows': rows,
                    }
                }
            }
        },
        'files': decisions,
        'questions': [],
    }
    write_json(out / 'answer_key.json', key)
    write_json(
        out / 'job.json',
        {
            'id': 'payroll_noise',
            'module': 'payroll',
            'title': 'MY02 payroll changes, November 2026',
            'goal': GOAL,
            'zip': 'packet.zip',
            'deliverables': [{'name': DELIVERABLE, 'template': TEMPLATE}],
        },
    )
    return key
