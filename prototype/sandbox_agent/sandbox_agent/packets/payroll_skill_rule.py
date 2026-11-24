"""The right answer needs two company rules that exist only in a skill, never in the packet.

- First Month Pay follows a house rule nobody could guess: monthly salary x paid days / 26, where paid days are the
  Mondays to Saturdays from the start date to the end of the month. It is written in the skill's SKILL.md.
- Cost Centre comes from a table that is one of the skill's own files (reference/cost_centres.csv).

Run it with the skill and without (--no-skills): without, the agent can only guess or ask, and those two columns
score low. The run record says whether the skill was read.
"""

from __future__ import annotations

from pathlib import Path

from .joiners import BASIC, basic_row, build_packet, build_template, paid_days, people, sheet_key, write_job

TEMPLATE = 'first_pay.xlsx'
COLUMNS = [*BASIC[:4], 'Cost Centre', 'Monthly Salary', 'First Month Pay']
COST_CENTRES = {'Sales': 'CC-410', 'Operations': 'CC-220', 'Finance': 'CC-130', 'Engineering': 'CC-350', 'People': 'CC-160'}
GOAL = (
    'Prepare the first-month pay sheet for the November 2026 new joiners of entity SG01 from the packet. Fill the '
    'template, one row per joiner, following its Read Me sheet.'
)
SKILL = """---
name: first-month-pay
description: How this company works out a new joiner's first month of pay, and which cost centre each department uses.
---
# First month pay

A joiner's first month is paid by days, on this company's own basis:

First Month Pay = Monthly Salary x paid days / 26, rounded to 2 decimals.

Paid days are the Mondays to Saturdays from the start date to the last day of that month, both included. Sundays are
never paid days. Use 26 when the count comes to more than 26.

# Cost centre

Each department has one cost centre. The table is in reference/cost_centres.csv; use the code exactly as written.
"""


def build(out: Path, seed: int = 20261031) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    staff = people(seed)
    files = build_packet(out, staff, seed)
    build_template(out / TEMPLATE, 'First Pay', COLUMNS, ["Cost Centre and First Month Pay follow the company's own rules."])
    skill = out / 'skills' / 'first-month-pay'
    (skill / 'reference').mkdir(parents=True)
    (skill / 'SKILL.md').write_text(SKILL, encoding='utf-8')
    table = 'department,cost_centre\n' + ''.join(f'{d},{c}\n' for d, c in COST_CENTRES.items())
    (skill / 'reference' / 'cost_centres.csv').write_text(table, encoding='utf-8')
    rows = [
        {**basic_row(p), 'Cost Centre': COST_CENTRES[p['department']],
         'First Month Pay': round(p['salary'] * paid_days(p['start']) / 26, 2)}
        for p in staff
    ]  # fmt: skip
    key = sheet_key('first_pay_output.xlsx', 'First Pay', COLUMNS, rows, files)
    write_job(out, 'payroll_skill_rule', 'SG01 first-month pay, November 2026', GOAL, TEMPLATE, key, skills='skills')
    return key
