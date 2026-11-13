"""The goal card: one short brief that tells the agent what done looks like, built from the job, not from steps."""

from __future__ import annotations

from ..modules import DEFAULT_CONTEXT
from .checks import describe_template
from .jobspec import JobSpec
from .rules import describe_format, describe_rules

MODULE_CONTEXT = DEFAULT_CONTEXT  # the defaults; the app passes the edited text as JobSpec.context


def goal_card(job: JobSpec, remote: dict[str, str], proof: str) -> str:
    """remote maps each input file name to its path in the sandbox."""
    context = job.context or MODULE_CONTEXT.get(job.module, '')
    parts = [f'# Goal\n{job.goal}']
    if context:
        parts.append(f'# House rules for {job.module}\n{context}')
    parts.append('# Deliverables (save each under /mnt/data/outputs/ with exactly this file name)')
    for d in job.deliverables:
        job_rules = describe_rules([r for r in job.rules if r.get('file') in (None, d.name)])
        if d.template is not None:
            parts.append(
                f'## {d.name}\nA filled copy of the template at {remote[d.template.name]}. Add data only; keep every '
                'existing sheet, cell, formula, hidden sheet and data-validation rule, and respect those rules.\n'
                + describe_template(d.template)
                + ('\n' + '\n'.join(job_rules) if job_rules else '')
            )
        else:
            parts.append(
                f'## {d.name}\nA new workbook built to this format (full specification: {remote[d.format_path.name]}):\n'
                + describe_format(d.format)
                + ('\n' + '\n'.join(job_rules) if job_rules else '')
            )
    done = [
        'Every deliverable above is saved and save_outputs reports no problems.',
        'document_map.json gives every packet file a decision (used, reference, not_relevant or unclear) and a reason.',
    ]
    if proof == 'row':
        done.append('provenance.json names the packet files behind every row you wrote.')
    done.append('notes.md records conflicts and how you settled them, assumptions, and anything still open.')
    parts.append('# Done means\n' + '\n'.join(f'- {line}' for line in done))
    return '\n\n'.join(parts)
