"""A November 2026 payroll instructions packet for SG01 that needs exactly one question to the user.

Everything is invented. The inputs are mixed HR documents (emails as .eml and .txt, a signed salary change form,
resignation letters as PDF, a bonus memo as Word) and the agent fills a four-sheet instructions template.

Traps:
- THE question: one employee's salary change is 6,500 in the signed change form and 6,200 in a later HR email, and
  nothing says which is final. A careful agent asks; the scripted answer says to use 6,500 from the signed form.
- the later email date tempts an agent to assume "latest wins" without asking
- a late joiner arrives in a separate plain-text email reply, not in the main joiners email
- leavers are only in PDF letters; the Final Pay Note is in the HR box at the foot of each letter
- one-time payments are only in a Word memo, including a sign-on bonus for a November joiner
- a headcount report is reference only (it confirms old salaries and departments)
- an outdated 2024 payroll policy, a team offsite agenda and last month's filled instructions are not relevant
"""

from __future__ import annotations

import random
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.datavalidation import DataValidation

from .common import header, packet_files, write_docx, write_json, write_pdf, zip_packet

FIRST = 'Avery Blake Casey Devon Ellis Finley Gray Harper Indigo Jordan Kai Logan Morgan Noel Oakley Parker Quinn Reese Sage Taylor'.split()
LAST = 'Ashford Brook Calder Dunmore Everly Fairley Garrow Hollis Ingram Jessup Kettering Larkin Merrow Norland Orchard Pemberton Quarry Rowan Stroud Thorne Upton Vance Whitlow Yardley'.split()
DEPARTMENTS = ['Sales', 'Operations', 'Finance', 'Engineering', 'People']
REASONS = ['Promotion', 'Annual Review', 'Market Adjustment', 'Role Change']
TYPES = ['Bonus', 'Sign-on Bonus', 'Referral Bonus', 'Commission']
SHEETS = {
    'New Joiners': ['Employee ID', 'Name', 'Start Date', 'Department', 'Monthly Salary'],
    'Salary Changes': ['Employee ID', 'Name', 'Effective Date', 'Old Salary', 'New Salary', 'Reason'],
    'Leavers': ['Employee ID', 'Name', 'Last Day', 'Final Pay Note'],
    'One-time Payments': ['Employee ID', 'Name', 'Type', 'Amount', 'Pay Month'],
}
KEYS = {'One-time Payments': ['Employee ID', 'Type']}
TEMPLATE = 'payroll_instructions_template.xlsx'
DELIVERABLE = 'payroll_instructions.xlsx'
GOAL = (
    'Prepare the November 2026 payroll instructions for entity SG01 from the HR inputs in the packet. Fill the '
    'template: every new joiner, salary change, leaver and one-time payment that takes effect in the November 2026 '
    'payroll, one row each, following the Read Me sheet.'
)

EMAIL_JOINERS = 'HR/Inbox/2026-10-20 New joiners for November.eml'
TXT_LATE_JOINER = 'HR/Inbox/RE_ late joiner - Engineering.txt'
SIGNED_FORM = 'HR/Forms/Signed/SG01_Salary_Change_Form_Nov2026_signed.xlsx'
EMAIL_CHANGES = 'HR/Inbox/2026-11-04 Salary updates for November payroll.eml'
BONUS_MEMO = 'Approvals/Bonus approval memo - November 2026.docx'
HEADCOUNT = 'Reports/SG01_Headcount_Report_Oct2026.xlsx'
OLD_POLICY = 'Policies/Archive/SG01_Payroll_Policy_2024.docx'
OFFSITE = 'Admin/Events/Team offsite agenda - Dec 2026.docx'
LAST_MONTH = 'Payroll/2026-10/SG01_payroll_instructions_Oct2026.xlsx'


def _long(iso: str) -> str:
    year, month, day = iso.split('-')
    months = 'January February March April May June July August September October November December'.split()
    return f'{int(day)} {months[int(month) - 1]} {year}'


def build_template(path: Path):
    wb = Workbook()
    readme = wb.active
    readme.title = 'Read Me'
    for line in [
        'SG01 payroll instructions',
        'One row per item on the sheet that matches it. Leave a sheet empty when nothing applies.',
        'Dates as YYYY-MM-DD (for example 2026-11-03). Pay Month as YYYY-MM (for example 2026-11).',
        'Amounts are monthly SGD as plain numbers, no currency signs.',
        'Department, Reason and Type must come from the dropdowns.',
        'Leavers: copy the Final Pay Note from the HR box on the resignation letter.',
    ]:
        readme.append([line])
    sheets = {}
    for name, columns in SHEETS.items():
        ws = wb.create_sheet(name)
        header(ws, columns)
        sheets[name] = ws
    lists = wb.create_sheet('Lists')
    lists.append(['Department', 'Reason', 'Type'])
    for i in range(len(DEPARTMENTS)):
        lists.append([DEPARTMENTS[i], REASONS[i] if i < len(REASONS) else None, TYPES[i] if i < len(TYPES) else None])
    lists.sheet_state = 'hidden'

    def rule(ws, cells, **kwargs):
        dv = DataValidation(allow_blank=False, **kwargs)
        dv.add(cells)
        ws.add_data_validation(dv)

    rule(sheets['New Joiners'], 'D2:D500', type='list', formula1=f'Lists!$A$2:$A${len(DEPARTMENTS) + 1}')
    rule(sheets['Salary Changes'], 'F2:F500', type='list', formula1=f'Lists!$B$2:$B${len(REASONS) + 1}')
    rule(sheets['One-time Payments'], 'C2:C500', type='list', formula1=f'Lists!$C$2:$C${len(TYPES) + 1}')
    for name, cells in [('New Joiners', 'E2:E500'), ('Salary Changes', 'D2:E500'), ('One-time Payments', 'D2:D500')]:
        rule(sheets[name], cells, type='decimal', operator='greaterThanOrEqual', formula1='0')
    wb.save(path)


def _people(rng: random.Random, count: int) -> list[dict]:
    surnames = rng.sample(LAST, count)
    firsts = [rng.choice(FIRST) for _ in range(count)]
    return [
        {'id': f'SG{1001 + i * 7}', 'name': f'{firsts[i]} {surnames[i]}', 'surname': surnames[i],
         'department': rng.choice(DEPARTMENTS), 'salary': float(rng.randrange(4200, 11800, 100))}
        for i in range(count)
    ]  # fmt: skip


def _eml(path: Path, sender: str, subject: str, sent: str, body: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = [
        f'From: {sender}',
        'To: payroll-sg@example.test',
        f'Subject: {subject}',
        f'Date: {sent}',
        'MIME-Version: 1.0',
        'Content-Type: text/plain; charset="utf-8"',
        '',
        *body,
    ]
    path.write_text('\r\n'.join(text) + '\r\n', encoding='utf-8')


def build(out: Path, seed: int = 20261031) -> dict:
    """Write packet/, packet.zip, the template, job.json, user_answers.json and answer_key.json under out/."""
    rng = random.Random(seed)
    packet = out / 'packet'
    if packet.exists():
        raise SystemExit(f'{packet} already exists; choose an empty folder')
    packet.mkdir(parents=True)
    staff = _people(rng, 21)
    current, joiners_pool = staff[:16], staff[16:]
    conflict, promo, role_change, leaver_a, leaver_b, bonus_a, bonus_b, referrer = current[:8]
    conflict['salary'] = 5900.0
    joiners = []
    for i, (person, start) in enumerate(zip(joiners_pool[:3], ['2026-11-02', '2026-11-09', '2026-11-16'], strict=True)):
        person['id'] = f'SG{1200 + i + 1}'
        person['start'] = start
        joiners.append(person)
    joiners[2]['department'] = 'Engineering'

    # Salary changes. The conflict: 6,500 on the signed form, 6,200 in a later email; the answer is 6,500.
    promo_new = promo['salary'] + 900
    role_new = role_change['salary'] + 500
    changes = [
        {'p': conflict, 'date': '2026-11-01', 'old': conflict['salary'], 'new': 6500.0, 'reason': 'Market Adjustment'},
        {'p': promo, 'date': '2026-11-01', 'old': promo['salary'], 'new': promo_new, 'reason': 'Promotion'},
        {'p': role_change, 'date': '2026-11-16', 'old': role_change['salary'], 'new': role_new, 'reason': 'Role Change'},
    ]
    leavers = [
        {'p': leaver_a, 'last': '2026-11-20', 'note': 'Pay out 3 days unused annual leave'},
        {'p': leaver_b, 'last': '2026-11-30', 'note': 'Recover 2 days annual leave taken in advance'},
    ]
    payments = [
        {'p': bonus_a, 'type': 'Bonus', 'amount': 3000.0},
        {'p': bonus_b, 'type': 'Bonus', 'amount': 1500.0},
        {'p': referrer, 'type': 'Referral Bonus', 'amount': 800.0},
        {'p': joiners[0], 'type': 'Sign-on Bonus', 'amount': 2000.0},
    ]

    # HR emails: joiners
    main = joiners[:2]
    _eml(
        packet / EMAIL_JOINERS,
        'Talent Acquisition <ta-sg@example.test>',
        'New joiners for November - SG01',
        'Tue, 20 Oct 2026 09:14:00 +0800',
        ['Hi Payroll,', '', 'Please set up the following new joiners for SG01 in the November 2026 payroll:', '']
        + [
            f'- {p["id"]} {p["name"]}, starts {_long(p["start"])}, {p["department"]}, monthly salary SGD {p["salary"]:,.0f}'
            for p in main
        ]
        + ['', 'Contracts are signed and filed. Bank details will follow from the employees directly.', '', 'Thanks,', 'TA team'],
    )
    late = joiners[2]
    (packet / TXT_LATE_JOINER).write_text(
        '\n'.join(
            [
                'From: Talent Acquisition <ta-sg@example.test>',
                'Sent: Monday, 26 October 2026 16:02',
                'To: Payroll SG',
                'Subject: RE: late joiner - Engineering',
                '',
                'Hi again,',
                '',
                'One more for November. The offer was accepted on Friday:',
                f'Employee ID {late["id"]}, {late["name"]}, Engineering, starting {_long(late["start"])}.',
                f'Monthly salary SGD {late["salary"]:,.0f}.',
                '',
                'Regards,',
                'TA team',
                '',
                '> -----Original Message-----',
                '> Is there anyone else joining in November? We are closing the joiner list this week.',
            ]
        ),
        encoding='utf-8',
    )

    # Signed salary change form (xlsx)
    form_path = packet / SIGNED_FORM
    form_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Change Form'
    ws.append(['SG01 Salary Change Form'])
    ws.append(['Payroll month: November 2026'])
    ws.append([])
    header(ws, ['Employee ID', 'Employee Name', 'Current Monthly Salary (SGD)', 'New Monthly Salary (SGD)',
                'Effective From', 'Reason for Change'])  # fmt: skip
    for c in changes[:2]:
        ws.append([c['p']['id'], c['p']['name'], c['old'], c['new'], _long(c['date']), c['reason']])
    ws.append([])
    ws.append(['Approved by: Head of People', 'Signed: 28 October 2026'])
    ws.append(['Approved by: Finance Director', 'Signed: 28 October 2026'])
    wb.save(form_path)

    # Later HR email: a different amount for the conflict employee, plus an unambiguous role change
    c0, c2 = changes[0], changes[2]
    _eml(
        packet / EMAIL_CHANGES,
        'HR Business Partner <hrbp-sg@example.test>',
        'Salary updates for November payroll',
        'Wed, 4 Nov 2026 11:37:00 +0800',
        [
            'Hi Payroll,',
            '',
            'Salary updates for SG01 this month:',
            '',
            f'1. {c0["p"]["name"]} ({c0["p"]["id"]}): market adjustment, monthly salary from SGD {c0["old"]:,.0f} '
            f'to SGD 6,200, effective {_long(c0["date"])}.',
            f'2. {c2["p"]["name"]} ({c2["p"]["id"]}): role change to team lead, monthly salary from SGD '
            f'{c2["old"]:,.0f} to SGD {c2["new"]:,.0f}, effective {_long(c2["date"])}.',
            '',
            'Let me know if you need anything else.',
            '',
            'Best,',
            'HRBP',
        ],
    )

    # Resignation letters (pdf)
    for item in leavers:
        p = item['p']
        path = packet / 'HR' / 'Resignations' / f'Resignation letter - {p["name"]}.pdf'
        path.parent.mkdir(parents=True, exist_ok=True)
        write_pdf(
            path,
            [
                'To: Head of People, SG01 Example Pte Ltd',
                f'From: {p["name"]} (Employee ID {p["id"]}), {p["department"]}',
                '',
                'Dear Head of People,',
                '',
                'Please accept this letter as formal notice of my resignation.',
                f'My last working day will be {_long(item["last"])}.',
                'Thank you for the support and opportunities over the past years.',
                '',
                'Yours sincerely,',
                p['name'],
                '',
                '----------------------------------------------',
                'HR USE ONLY',
                'Resignation accepted. Notice period served in full.',
                f'Final Pay Note: {item["note"]}',
                '----------------------------------------------',
            ],
        )

    # Bonus approval memo (docx)
    memo = packet / BONUS_MEMO
    memo.parent.mkdir(parents=True, exist_ok=True)
    write_docx(
        memo,
        [
            'MEMO - One-time payments approved for the November 2026 payroll (SG01)',
            'From: Finance Director. To: Payroll SG. Date: 30 October 2026.',
            'The following one-time payments are approved and are to be paid in the November 2026 payroll:',
        ]
        + [f'{p["p"]["id"]} {p["p"]["name"]}: {p["type"]}, SGD {p["amount"]:,.0f}' for p in payments]
        + ['All amounts are gross. No other one-time payments are approved for November.'],
    )

    # Headcount report (reference)
    head_path = packet / HEADCOUNT
    head_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Headcount'
    header(ws, ['Employee ID', 'Name', 'Department', 'Monthly Salary (SGD)', 'Status'])
    for p in current:
        ws.append([p['id'], p['name'], p['department'], p['salary'], 'Active'])
    summary = wb.create_sheet('By Department')
    header(summary, ['Department', 'Headcount'])
    for d in DEPARTMENTS:
        summary.append([d, sum(p['department'] == d for p in current)])
    wb.save(head_path)

    # Not relevant: outdated policy, offsite agenda, last month's instructions
    for path, paragraphs in [
        (
            OLD_POLICY,
            [
                'SG01 Payroll Policy (2024 edition) - superseded',
                'Payroll cut-off is the 15th of each month. Changes after cut-off move to the following month.',
                'Salary changes require a form signed by the line manager only.',
                'This policy was replaced on 1 January 2026.',
            ],
        ),
        (
            OFFSITE,
            [
                'People and Finance team offsite - agenda',
                'Thursday 10 December 2026, Harbour View Room',
                '09:00 Welcome and coffee. 09:30 Year in review. 11:00 Planning for 2027. 12:30 Lunch.',
                '14:00 Team activity. 16:30 Close.',
            ],
        ),
    ]:
        (packet / path).parent.mkdir(parents=True, exist_ok=True)
        write_docx(packet / path, paragraphs)
    last_path = packet / LAST_MONTH
    last_path.parent.mkdir(parents=True, exist_ok=True)
    build_template(last_path)
    wb = load_workbook(last_path)
    old = staff[8:12]
    wb['New Joiners'].append([old[0]['id'], old[0]['name'], '2026-10-05', old[0]['department'], old[0]['salary']])
    wb['Salary Changes'].append(
        [old[1]['id'], old[1]['name'], '2026-10-01', old[1]['salary'] - 400, old[1]['salary'], 'Annual Review']
    )
    wb['One-time Payments'].append([old[2]['id'], old[2]['name'], 'Commission', 1250.0, '2026-10'])
    wb.save(last_path)

    build_template(out / TEMPLATE)
    zip_packet(packet, out / 'packet.zip')

    rows = {
        'New Joiners': [
            {'Employee ID': p['id'], 'Name': p['name'], 'Start Date': p['start'], 'Department': p['department'],
             'Monthly Salary': p['salary']}
            for p in joiners
        ],
        'Salary Changes': [
            {'Employee ID': c['p']['id'], 'Name': c['p']['name'], 'Effective Date': c['date'], 'Old Salary': c['old'],
             'New Salary': c['new'], 'Reason': c['reason']}
            for c in changes
        ],
        'Leavers': [
            {'Employee ID': x['p']['id'], 'Name': x['p']['name'], 'Last Day': x['last'], 'Final Pay Note': x['note']}
            for x in leavers
        ],
        'One-time Payments': [
            {'Employee ID': x['p']['id'], 'Name': x['p']['name'], 'Type': x['type'], 'Amount': x['amount'],
             'Pay Month': '2026-11'}
            for x in payments
        ],
    }  # fmt: skip
    sheets = {}
    for name, columns in SHEETS.items():
        key_cols = KEYS.get(name, ['Employee ID'])
        sheets[name] = {'key': key_cols, 'fields': [c for c in columns if c not in key_cols], 'rows': rows[name]}
    decisions = {f: 'used' for f in packet_files(packet)}
    decisions[HEADCOUNT] = 'reference'
    for path in (OLD_POLICY, OFFSITE, LAST_MONTH):
        decisions[path] = 'not_relevant'
    keywords = [conflict['id'], conflict['name'], conflict['surname']]
    key = {
        'deliverables': {DELIVERABLE: {'sheets': sheets}},
        'files': decisions,
        'questions': [{'topic': f'conflicting salary change for {conflict["name"]}', 'keywords': keywords}],
    }
    write_json(out / 'answer_key.json', key)
    write_json(
        out / 'user_answers.json',
        [{'keywords': keywords, 'answer': 'Use 6,500 from the signed change form; the email had a typo.'}],
    )
    write_json(
        out / 'job.json',
        {
            'id': 'payroll_instructions_question',
            'module': 'payroll',
            'title': 'SG01 payroll instructions, November 2026',
            'goal': GOAL,
            'zip': 'packet.zip',
            'deliverables': [{'name': DELIVERABLE, 'template': TEMPLATE}],
            'user_answers': 'user_answers.json',
        },
    )
    return key
