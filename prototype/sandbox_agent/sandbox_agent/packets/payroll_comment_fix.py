"""A finished output with one planted mistake, and the user's comment on that cell.

The run starts from the earlier outputs (prior_key.json, where one salary has two digits swapped) and one round of
comments (loop.json). The agent should correct that cell from the joiners list and change nothing else. The answer
key is the output as it should be afterwards.
"""

from __future__ import annotations

from pathlib import Path

from .common import write_json
from .joiners import BASIC, basic_row, build_packet, build_template, people, sheet_key, write_job

TEMPLATE = 'joiner_setup.xlsx'
DELIVERABLE = 'joiner_setup_output.xlsx'
GOAL = (
    'Prepare the payroll set-up sheet for the November 2026 new joiners of entity SG01 from the packet. Fill the '
    'template, one row per joiner, following its Read Me sheet.'
)


def build(out: Path, seed: int = 20261031) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    staff = people(seed)
    files = build_packet(out, staff, seed)
    build_template(out / TEMPLATE, 'Joiners', BASIC, [])
    rows = [basic_row(p) for p in staff]
    key = sheet_key(DELIVERABLE, 'Joiners', BASIC, rows, files)
    wrong = 3  # the fourth joiner: thousands and hundreds swapped, a slip a person makes
    salary = staff[wrong]['salary']
    swapped = float(int(salary) % 1000 // 100 * 1000 + int(salary) // 1000 * 100 + int(salary) % 100)
    if swapped == salary:
        swapped = salary + 900
    prior = [dict(r) for r in rows]
    prior[wrong]['Monthly Salary'] = swapped
    write_json(out / 'prior_key.json', sheet_key(DELIVERABLE, 'Joiners', BASIC, prior, files))
    comment = {'path': f'outputs/{DELIVERABLE}', 'sheet': 'Joiners', 'cell': f'E{wrong + 2}',
               'text': 'This salary does not match the joiners list. Please correct it.'}  # fmt: skip
    write_json(out / 'loop.json', {'prior_key': 'prior_key.json', 'comments': [comment]})
    write_job(out, 'payroll_comment_fix', 'SG01 joiner set-up, November 2026', GOAL, TEMPLATE, key)
    return key
