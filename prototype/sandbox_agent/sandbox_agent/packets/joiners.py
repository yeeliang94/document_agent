"""Shared by the three packets that test playbooks: skills, comments on outputs, and "remember for next time".

Everything is invented. The inputs are the same small November 2026 joiners packet for entity SG01: the joiners
list (a workbook), an October headcount report and a welcome-lunch note. What differs is the template, and what each
packet adds: a skill, or a finished output with a comment on it.
"""

from __future__ import annotations

import calendar
import random
from datetime import date
from pathlib import Path

from openpyxl import Workbook

from .common import header, packet_files, write_docx, write_json, zip_packet

FIRST = 'Avery Blake Casey Devon Ellis Finley Gray Harper Indigo Jordan Kai Logan'.split()
LAST = 'Ashford Brook Calder Dunmore Everly Fairley Garrow Hollis Ingram Jessup Kettering Larkin'.split()
DEPARTMENTS = ['Sales', 'Operations', 'Finance', 'Engineering', 'People']
JOINERS = 'HR/Joiners_Nov2026.xlsx'
HEADCOUNT = 'Reports/SG01_Headcount_Oct2026.xlsx'
LUNCH = 'Admin/Welcome lunch - November joiners.docx'
BASIC = ['Employee ID', 'Name', 'Start Date', 'Department', 'Monthly Salary']


def people(seed: int) -> list[dict]:
    rng = random.Random(seed)
    surnames, firsts = rng.sample(LAST, 10), rng.sample(FIRST, 10)
    days = sorted(rng.sample(range(2, 27), 10))
    return [
        {'id': f'SG{1301 + i}', 'first': firsts[i], 'last': surnames[i], 'name': f'{firsts[i]} {surnames[i]}',
         'start': date(2026, 11, days[i]).isoformat(), 'department': DEPARTMENTS[i % 5],
         'salary': float(rng.randrange(3900, 9800, 100))}
        for i in range(10)
    ]  # fmt: skip


def basic_row(p: dict, name: str | None = None) -> dict:
    return {'Employee ID': p['id'], 'Name': name or p['name'], 'Start Date': p['start'], 'Department': p['department'],
            'Monthly Salary': p['salary']}  # fmt: skip


def build_template(path: Path, sheet: str, columns: list[str], notes: list[str]):
    wb = Workbook()
    readme = wb.active
    readme.title = 'Read Me'
    for line in ['One row per new joiner, in the order of the joiners list.', 'Dates as YYYY-MM-DD.',
                 'Amounts are monthly SGD as plain numbers, no currency signs.', *notes]:  # fmt: skip
        readme.append([line])
    header(wb.create_sheet(sheet), columns)
    wb.save(path)


def build_packet(out: Path, staff: list[dict], seed: int) -> dict[str, str]:
    """Write packet/ and packet.zip under out/; returns the expected decision for each file."""
    packet = out / 'packet'
    if packet.exists():
        raise SystemExit(f'{packet} already exists; choose an empty folder')
    for relative in (JOINERS, HEADCOUNT, LUNCH):
        (packet / relative).parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Joiners'
    ws.append(['SG01 new joiners, November 2026 payroll'])
    ws.append([])
    header(ws, ['Employee ID', 'First Name', 'Surname', 'Start Date', 'Department', 'Monthly Salary (SGD)'])
    for p in staff:
        ws.append([p['id'], p['first'], p['last'], date.fromisoformat(p['start']), p['department'], p['salary']])
    wb.save(packet / JOINERS)
    rng = random.Random(seed + 1)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Headcount'
    header(ws, ['Employee ID', 'Name', 'Department', 'Monthly Salary (SGD)', 'Status'])
    for i in range(14):
        name = f'{rng.choice(FIRST)} {rng.choice(LAST)}'
        ws.append([f'SG{1001 + i * 7}', name, rng.choice(DEPARTMENTS), float(rng.randrange(4200, 11800, 100)), 'Active'])
    wb.save(packet / HEADCOUNT)
    write_docx(packet / LUNCH, ['Welcome lunch for the November joiners', 'Friday 27 November 2026, 12:30, Level 4 pantry.',
                                'Please tell the People team about dietary needs by 20 November.'])  # fmt: skip
    zip_packet(packet, out / 'packet.zip')
    decisions = {f: 'not_relevant' for f in packet_files(packet)}
    decisions[JOINERS] = 'used'
    return decisions


def paid_days(start: str) -> int:
    """Mondays to Saturdays from the start date to the end of its month, both included, capped at 26."""
    first = date.fromisoformat(start)
    last = calendar.monthrange(first.year, first.month)[1]
    return min(26, sum(date(first.year, first.month, d).weekday() != 6 for d in range(first.day, last + 1)))


def write_job(out: Path, job_id: str, title: str, goal: str, template: str, key: dict, **extra):
    """job.json and answer_key.json. The deliverable takes the name the app gives it: <template>_output.xlsx."""
    write_json(out / 'answer_key.json', key)
    deliverable = f'{Path(template).stem}_output.xlsx'
    write_json(out / 'job.json', {'id': job_id, 'module': 'payroll', 'title': title, 'goal': goal, 'zip': 'packet.zip',
                                  'deliverables': [{'name': deliverable, 'template': template}], **extra})  # fmt: skip


def sheet_key(deliverable: str, sheet: str, columns: list[str], rows: list[dict], files: dict) -> dict:
    spec = {'key': ['Employee ID'], 'fields': [c for c in columns if c != 'Employee ID'], 'rows': rows}
    return {'deliverables': {deliverable: {'sheets': {sheet: spec}}}, 'files': files, 'questions': []}
