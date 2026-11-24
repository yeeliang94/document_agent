"""The right answer follows two habits that only an earlier run shows, never the packet or the template.

- Name is written "SURNAME, First" (the joiners list gives first name and surname apart).
- Dept Code is this company's own code for each department. The codes cannot be guessed.

The packet's job names one earlier run: October's finished sheet for other people, with the same two habits. Run
it with the earlier run and without (--no-past-runs): without, the agent can only guess or ask, and those two
columns score low. The run record says whether the earlier run was opened.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from .joiners import BASIC, basic_row, build_packet, build_template, people, sheet_key, write_job

TEMPLATE = 'joiners_sheet.xlsx'
COLUMNS = [*BASIC[:4], 'Dept Code', 'Monthly Salary']
CODES = {'Sales': 'D-4107', 'Operations': 'D-2209', 'Finance': 'D-1303', 'Engineering': 'D-3511', 'People': 'D-1602'}
GOAL = (
    'Prepare the new joiners sheet for the November 2026 joiners of entity SG01 from the packet. Fill the template, '
    'one row per joiner, the same way the earlier months were done.'
)


def habit_row(p: dict) -> dict:
    return {**basic_row(p, f'{p["last"].upper()}, {p["first"]}'), 'Dept Code': CODES[p['department']]}


def build(out: Path, seed: int = 20261031) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    staff = people(seed)
    files = build_packet(out, staff, seed)
    build_template(out / TEMPLATE, 'Joiners', COLUMNS, ['Name and Dept Code are written as in earlier months.'])
    # October's finished sheet: other people, the same habits. Every department appears, so every code is shown.
    october = out / 'past' / 'october'
    october.mkdir(parents=True)
    wb = load_workbook(out / TEMPLATE)
    for p in people(seed + 7):
        row = habit_row({**p, 'id': p['id'].replace('SG13', 'SG12'), 'start': p['start'].replace('-11-', '-10-')})
        wb['Joiners'].append([row[c] for c in COLUMNS])
    wb.save(october / 'joiners_sheet_output.xlsx')
    (october / 'notes.md').write_text('Filled from the October joiners list. Nothing left open.\n', encoding='utf-8')
    key = sheet_key('joiners_sheet_output.xlsx', 'Joiners', COLUMNS, [habit_row(p) for p in staff], files)
    past = [{'title': 'SG01 new joiners, October 2026', 'finished': '2026-10-30', 'folder': 'past/october',
             'goal': GOAL.replace('November', 'October')}]  # fmt: skip
    write_job(out, 'payroll_like_last_month', 'SG01 new joiners, November 2026', GOAL, TEMPLATE, key, past_runs=past)
    return key
