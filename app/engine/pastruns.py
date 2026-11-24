"""Earlier runs of the same playbook, offered to the agent as examples of how this work was done before.

The goal card lists a few finished runs by label, title and date only. The agent opens one with the open_past_run
tool when it would help: the tool copies that run's saved files into the sandbox and says where they are. Every
opening is recorded. An earlier run shows shape and conventions; this job's own source files decide the values.
"""

from __future__ import annotations

from pathlib import Path

PAST_LIMIT = 3  # how many earlier runs a goal card offers
PAST_FILE_BYTES = 20 * 1024**2  # one file of an earlier run; a larger one is left out and named
PAST_FILES = 40  # files copied from one earlier run


def past_files(folder: Path) -> list[Path]:
    """The saved files of one earlier run. Links are left out: only the run's own files are copied."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.rglob('*') if p.is_file() and not p.is_symlink())


def describe(runs) -> str:
    """The goal card's section on earlier runs."""
    return (
        '# Earlier runs\nFinished runs of this same kind of work, newest first. One of them can show how the work '
        'was laid out and which conventions the user accepted. Open one with open_past_run when that would help; '
        'you do not have to. An earlier run is an example, not a source: the goal, the house rules and the skills '
        "come first where they differ, and every value you write comes from this job's own files.\n"
        + '\n'.join(f'- {r["label"]}: {r["title"]} (finished {r["finished"]})' for r in runs)
    )


async def open_run(job, label: str) -> dict:
    """Copy one earlier run's files into the sandbox. A wrong label gets an answer that lists the right ones."""
    runs = {r['label']: r for r in job.spec.past_runs}
    if label not in runs:
        return {'error': f'There is no earlier run "{label}". The earlier runs are: {", ".join(runs) or "none"}.'}
    if label in job.past_opened:
        return job.past_opened[label]
    run = runs[label]
    folder = Path(run['outputs'])
    found = past_files(folder)
    if not found:
        return {'error': f'The files of {label} are no longer on disk. Work without it.'}
    copied, left_out = [], []
    for path in found:
        name = path.relative_to(folder).as_posix()
        if len(copied) >= PAST_FILES or path.stat().st_size > PAST_FILE_BYTES:
            left_out.append(name)
            continue
        remote = await job.main.upload_bytes(f'{label}__{name.replace("/", "__")}', path.read_bytes())
        copied.append({'file': name, 'path': remote})
    result = {
        'run': label,
        'title': run['title'],
        'goal': run.get('goal', ''),
        'files': copied,
        'left_out': left_out,
        'note': "These are that run's saved outputs and notes. Read them with code. Do not copy values from them.",
    }
    job.past_opened[label] = result
    job.store.event('past_run.opened', run=label, job=run.get('id', ''), title=run['title'], files=len(copied), left_out=len(left_out))  # fmt: skip
    return result
