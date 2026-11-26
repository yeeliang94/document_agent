"""The goal card: one short brief that tells the agent what done looks like, built from the job, not from steps."""

from __future__ import annotations

from ..modules import DEFAULT_CONTEXT
from .checks import describe_template
from .jobspec import JobSpec
from .pastruns import describe as describe_past_runs
from .rules import describe_format, describe_rules

MODULE_CONTEXT = DEFAULT_CONTEXT  # the defaults; the app passes the edited text as JobSpec.context


def _skill_line(skill: dict) -> str:
    held = skill.get('programs') or ()
    return f'- {skill["name"]}: {skill["description"]}' + (f' Programs: {", ".join(held)}.' if held else '')


def _style_call(remote: dict[str, str], name: str, format_path: str = '') -> str:
    """The line of code that runs the house-style helper on one output, with its format when it has one."""
    folder = remote['house_style.py'].rsplit('/', 1)[0]
    extra = f", '{format_path}'" if format_path else ''
    return (f"import sys; sys.path.insert(0, '{folder}'); from house_style import apply; "
            f"apply('/mnt/data/outputs/{name}'{extra})")  # fmt: skip


def goal_card(job: JobSpec, remote: dict[str, str], proof: str) -> str:
    """remote maps each input file name to its path in the sandbox."""
    # A job made from a playbook follows its text exactly, empty included; the built-in rules are for the rest.
    context = job.context if job.playbook_id else job.context or MODULE_CONTEXT.get(job.module, '')
    parts = [f'# Goal\n{job.goal}']
    if context:
        parts.append(f'# House rules for {job.playbook or job.module}\n{context}')
    if job.client:
        parts.append(
            f"# Client notes for {job.client}\nThe user's standing notes for this client, for all their work. They "
            'apply the way the house rules do. Where they and the house rules disagree, follow the client notes and '
            'say in notes.md which rule you followed and why.\n'
            + (job.client_notes.strip() or '(No notes yet. In your final answer, put in for_next_time what a later run '
               'for this client should follow, so the user can keep it as their notes.)')
        )
    elif job.known_clients:
        parts.append(
            '# Known clients\nThe user keeps notes for these clients: ' + ', '.join(job.known_clients) + '. No client '
            'was chosen for this run. If the documents clearly belong to one of them, call ask_user once to ask '
            "whether this run is for that client; if the user says yes, call follow_client with its name to get its "
            'notes, which then apply as if the client had been chosen at the start. Do not ask otherwise.'
        )
    if job.skills:
        parts.append(
            "# Skills\nThe user's written know-how for this kind of work. It applies the way the house rules do. "
            'Before you do work a skill covers, read it with read_skill and follow what it says; read any other '
            f"text file it points to the same way. {remote['skills.zip']} holds every skill's files, one folder per "
            'skill, for code that needs to load one as data, such as a rate table.\n'
            + '\n'.join(_skill_line(s) for s in job.skills)
            + (
                '\nA skill that lists programs comes with Python the user wants used for that work. Unpack the '
                'skills zip and run the program as its skill describes, rather than writing your own version of what '
                'it does; the result is then the same every time. If a program fails, say so in notes.md.'
                if any(s.get('programs') for s in job.skills)
                else ''
            )
        )
    if job.past_runs:
        parts.append(describe_past_runs(job.past_runs))
    if not job.deliverables:
        parts.append(
            '# Deliverables\nThe user gave no template and no written format. Work out from the goal what they need in '
            'hand at the end, and in what form a person doing this work would hand it over: a workbook for records '
            'and figures, a document for something to read, more than one file when the goal has more than one '
            'part. Give each file a clear name and save it under /mnt/data/outputs/. Say in notes.md which files you '
            'made and why in that form.'
            + (
                ' To give a workbook you build a plain, tidy look (header row, number and date formats, widths), '
                f'run {_style_call(remote, "<file>.xlsx")}.'
                if 'house_style.py' in remote
                else ''
            )
            + (
                ' In a workbook you build, put the headings in the first row of each sheet: every filled row below '
                'it counts as a row you wrote, and needs its sources in provenance.json.'
                if proof == 'row'
                else ''
            )
        )
    else:
        parts.append('# Deliverables (save each under /mnt/data/outputs/ with exactly this file name)')
    for d in job.deliverables:
        job_rules = describe_rules([r for r in job.rules if r.get('file') in (None, d.name)])
        if d.template is not None:
            parts.append(
                f'## {d.name}\nA filled copy of the template at {remote[d.template.name]}, filled the way a careful '
                'person would fill it: each record inside the table it belongs to, with rows inserted inside that '
                'table when it is too short. Keep every sheet (hidden ones hidden), instruction, label, formula and '
                'data-validation rule; they may move down with inserted rows, and totals and rules then stretch over '
                'the new rows. Change or clear other template content only on purpose, and say why in notes.md.\n'
                + describe_template(d.template)
                + ('\n' + '\n'.join(job_rules) if job_rules else '')
            )
        else:
            parts.append(
                f'## {d.name}\nA new workbook built to this format (full specification: {remote[d.format_path.name]}):\n'
                + describe_format(d.format)
                + ('\n' + '\n'.join(job_rules) if job_rules else '')
                + (
                    '\nOnce the data is in, give it its look with the house-style helper: '
                    + _style_call(remote, d.name, remote[d.format_path.name])
                    if 'house_style.py' in remote
                    else ''
                )
            )
    done = [
        'Every deliverable above is saved and save_outputs reports no problems.'
        if job.deliverables
        else 'The files the goal calls for are saved and save_outputs reports no problems.',
        'document_map.json gives every packet file a decision (used, reference, not_relevant or unclear) and a reason.',
    ]
    if any(d.template is not None for d in job.deliverables):
        done.append(
            'Every template change that save_outputs lists, other than moves from rows you inserted, is deliberate and '
            'explained in notes.md.'
        )
    if proof == 'row':
        done.append('provenance.json names the packet files behind every row you wrote.')
    done.append('notes.md records conflicts and how you settled them, assumptions, and anything still open.')
    parts.append('# Done means\n' + '\n'.join(f'- {line}' for line in done))
    return '\n\n'.join(parts)
