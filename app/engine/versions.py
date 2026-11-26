"""A version of the outputs at every save, so a run that stops can be picked up from any of them.

<job>/versions/v0001/files/   the outputs as they were after one save, file for file
                 version.json  {"number", "run", "step", "at", "description", "passed", "files"}
The record sits beside the copied files, not among them, so an output of any name is kept as it was.

Files are copied, not linked: a file opened from the outputs folder and saved in Excel changes in place, and must
not change a kept version with it. Each run keeps at most a set number of its versions (a Settings page value); older
ones of that run are removed. Continuing from a version copies it back as the outputs.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .checks import RECORD_FILES
from .runstore import _rename

META = 'version.json'
FILES = 'files'


def _files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob('*') if p.is_file() and not p.name.endswith('.pending'))


def listed(folder: Path) -> list[dict]:
    """Every version kept, newest first."""
    found = []
    for meta in folder.glob('v*/' + META) if folder.is_dir() else ():
        try:
            found.append(json.loads(meta.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            continue
    return sorted(found, key=lambda v: v['number'], reverse=True)


def keep(outputs: Path, folder: Path, run: str, step: int, description: str, passed: bool, cap: int) -> dict:
    """Keep the outputs as they are now as a new version; drop this run's oldest beyond `cap`."""
    number = max((v['number'] for v in listed(folder)), default=0) + 1
    target = folder / f'v{number:04d}'
    staging = folder / f'.v{number:04d}.partial'
    shutil.rmtree(staging, ignore_errors=True)
    files = []
    for path in _files(outputs):
        relative = path.relative_to(outputs)
        (staging / FILES / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, staging / FILES / relative)
        files.append(relative.as_posix())
    meta = {'number': number, 'run': run, 'step': step, 'description': description, 'passed': passed,
            'files': files, 'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}  # fmt: skip
    staging.mkdir(parents=True, exist_ok=True)
    (staging / META).write_text(json.dumps(meta, indent=1), encoding='utf-8')
    staging.rename(target)
    own = [v for v in listed(folder) if v['run'] == run]
    for old in own[cap:]:
        shutil.rmtree(folder / f'v{old["number"]:04d}', ignore_errors=True)
    return meta


def find(folder: Path, number: int) -> dict:
    for version in listed(folder):
        if version['number'] == number:
            return version
    raise KeyError(number)


def restore(folder: Path, number: int, outputs: Path) -> dict:
    """Make the outputs exactly that version's files. Built beside the outputs first, so a failure leaves them as
    they were."""
    version = find(folder, number)
    source = folder / f'v{number:04d}'
    holder = source / FILES if (source / FILES).is_dir() else source  # older versions keep the files beside the record
    staging = outputs.with_name(outputs.name + '.restoring')
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for relative in version['files']:
        (staging / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(holder / relative, staging / relative)  # a copy: the version must not change with the outputs
    previous = outputs.with_name(outputs.name + '.before-restore')
    shutil.rmtree(previous, ignore_errors=True)
    if outputs.exists():
        try:
            _rename(outputs, previous)
        except PermissionError as error:  # Windows: a file in outputs/ is open, usually in Excel
            shutil.rmtree(staging, ignore_errors=True)
            raise PermissionError(f'A file in {outputs} is open in another program, usually Excel. Close it and '
                                  'try again.') from error  # fmt: skip
    try:
        _rename(staging, outputs)
    except OSError:
        _rename(previous, outputs)
        raise
    shutil.rmtree(previous, ignore_errors=True)
    return version


def set_aside(outputs: Path) -> None:
    """Empty the outputs for a run that starts over. The kept versions stay, so the work can still be picked up from
    one of them. Moved aside first, so a file open in Excel leaves the outputs as they were."""
    if not outputs.exists():
        return
    previous = outputs.with_name(outputs.name + '.before-start-over')
    shutil.rmtree(previous, ignore_errors=True)
    try:
        _rename(outputs, previous)
    except PermissionError as error:  # Windows: a file in outputs/ is open, usually in Excel
        raise PermissionError(f'A file in {outputs} is open in another program, usually Excel. Close it and '
                              'try again.') from error  # fmt: skip
    outputs.mkdir()
    shutil.rmtree(previous, ignore_errors=True)


def describe(step: int, saved: list[str], check: dict) -> str:
    """One line for a version: the step, the deliverables saved and how the checks went."""
    made = [name for name in saved if name.rsplit('/', 1)[-1] not in RECORD_FILES]
    files = ', '.join(made[:3]) + (f' and {len(made) - 3} more' if len(made) > 3 else '') if made else 'notes only'
    issues = len(check.get('issues') or [])
    outcome = 'checks passed' if check.get('passed') else f'{issues} problem{"" if issues == 1 else "s"} to fix'
    return f'Step {step}: saved {files}; {outcome}'
