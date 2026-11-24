"""The right answer needs a program that comes with a skill: the company's own levy calculator.

- Staff Levy follows banded rates with a floor and an odd rounding step. The rules live only in the skill's
  program (scripts/levy.py); SKILL.md says what the program is for and how to call it, not how it works.

Run it with the skill and without (--no-skills): without, the agent cannot know the levy. The run record says
whether the program was run (skills_run), not only whether the skill was read.
"""

from __future__ import annotations

from pathlib import Path

from .joiners import BASIC, basic_row, build_packet, build_template, people, sheet_key, write_job

TEMPLATE = 'levy_sheet.xlsx'
COLUMNS = [*BASIC, 'Staff Levy']
GOAL = (
    'Prepare the staff levy sheet for the November 2026 new joiners of entity SG01 from the packet. Fill the '
    'template, one row per joiner, following its Read Me sheet.'
)
SKILL = """---
name: staff-levy
description: Works out the monthly Staff Levy for an employee with the company's own calculator.
---
# Staff levy

The Staff Levy is worked out by the company's calculator, scripts/levy.py. Always use the calculator; do not work
the levy out by hand.

From Python, with this skill's folder unpacked:

    import sys
    sys.path.insert(0, '<where the skills zip was unpacked>/staff-levy/scripts')
    from levy import staff_levy
    staff_levy(5200.0)   # monthly salary in SGD -> levy in SGD

Or from a shell: python scripts/levy.py 5200
"""
PROGRAM = '''"""The company's Staff Levy calculator. Monthly salary in SGD in, levy in SGD out."""

import sys

BANDS = [(4500.0, 0.0125), (2500.0, 0.021), (float('inf'), 0.0335)]  # width of each band, rate
FLOOR = 61.5


def staff_levy(salary: float) -> float:
    left, levy = float(salary), 0.0
    for width, rate in BANDS:
        part = min(left, width)
        levy += part * rate
        left -= part
        if left <= 0:
            break
    levy = max(levy, FLOOR)
    return round(round(levy * 2) / 2, 2)  # to the nearest 50 cents


if __name__ == '__main__':
    print(staff_levy(float(sys.argv[1])))
'''


def staff_levy(salary: float) -> float:
    scope: dict = {'__name__': 'levy'}
    exec(PROGRAM, scope)  # the key is worked out by the very program the skill ships
    return scope['staff_levy'](salary)


def build(out: Path, seed: int = 20261031) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    staff = people(seed)
    files = build_packet(out, staff, seed)
    build_template(out / TEMPLATE, 'Levy', COLUMNS, ["Staff Levy follows the company's own calculator."])
    skill = out / 'skills' / 'staff-levy'
    (skill / 'scripts').mkdir(parents=True)
    (skill / 'SKILL.md').write_text(SKILL, encoding='utf-8')
    (skill / 'scripts' / 'levy.py').write_text(PROGRAM, encoding='utf-8')
    rows = [{**basic_row(p), 'Staff Levy': staff_levy(p['salary'])} for p in staff]
    key = sheet_key('levy_sheet_output.xlsx', 'Levy', COLUMNS, rows, files)
    write_job(out, 'payroll_skill_program', 'SG01 staff levy, November 2026', GOAL, TEMPLATE, key, skills='skills')
    return key
