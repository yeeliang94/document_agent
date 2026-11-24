"""The agent must take a lasting rule from a comment by itself, and the rule must hold afterwards.

Run one starts from a finished output where names are written as the joiners list has them. The user's comment on
one name says how this company writes names. It is not marked "remember": the agent has to see that this is a
standing rule and keep it with its remember tool. Run two is a new job from the playbook as that left it, with no
comment and no earlier run to look at: the names should come out right by themselves. Last, the test the app kept
for the lesson is replayed. The answer key is the second run's expected output.
"""

from __future__ import annotations

from pathlib import Path

from .common import write_json
from .joiners import BASIC, basic_row, build_packet, build_template, people, sheet_key, write_job
from .payroll_remember import DELIVERABLE, GOAL, TEMPLATE


def build(out: Path, seed: int = 20261031) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    staff = people(seed)
    files = build_packet(out, staff, seed)
    build_template(out / TEMPLATE, 'Joiners', BASIC, [])
    styled = [basic_row(p, f'{p["last"].upper()}, {p["first"]}') for p in staff]
    key = sheet_key(DELIVERABLE, 'Joiners', BASIC, styled, files)
    write_json(out / 'prior_key.json', sheet_key(DELIVERABLE, 'Joiners', BASIC, [basic_row(p) for p in staff], files))
    example = f'{staff[0]["last"].upper()}, {staff[0]["first"]}'
    comment = {'path': f'outputs/{DELIVERABLE}', 'sheet': 'Joiners', 'cell': 'B2',
               'text': f'We always write names as SURNAME, First name on payroll sheets, for example {example}.'}  # fmt: skip
    write_json(out / 'loop.json', {'prior_key': 'prior_key.json', 'comments': [comment], 'then_fresh_run': True, 'learn': True})
    write_job(out, 'payroll_learn', 'SG01 joiner set-up, November 2026', GOAL, TEMPLATE, key)
    return key
