"""A comment marked "remember for next time" must change the next run, not only this one.

Run one starts from a finished output where names are written as the joiners list has them, with the user's comment
asking for this company's style (SURNAME, First name) and marked to be remembered. That adds the comment to the
playbook. Run two is a new job on the same packet, started from the playbook as it now stands, with no comment: the
names should come out in the company's style by themselves. The answer key is that second run's expected output.
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
    styled = [basic_row(p, f'{p["last"].upper()}, {p["first"]}') for p in staff]
    key = sheet_key(DELIVERABLE, 'Joiners', BASIC, styled, files)
    write_json(out / 'prior_key.json', sheet_key(DELIVERABLE, 'Joiners', BASIC, [basic_row(p) for p in staff], files))
    example = f'{staff[0]["last"].upper()}, {staff[0]["first"]}'
    comment = {'path': f'outputs/{DELIVERABLE}', 'sheet': 'Joiners', 'cell': 'B2', 'remember': True,
               'text': f'Write every name as SURNAME, First name, for example {example}.'}  # fmt: skip
    write_json(out / 'loop.json', {'prior_key': 'prior_key.json', 'comments': [comment], 'then_fresh_run': True})
    write_job(out, 'payroll_remember', 'SG01 joiner set-up, November 2026', GOAL, TEMPLATE, key)
    return key
