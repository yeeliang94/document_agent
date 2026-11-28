"""The user's skills inside a run: checked when the run starts, packed for the sandbox, and read by the agent on demand.

A job holds its own copy of each skill (see app/skills.py). The goal card lists the skills by name and description
only; the agent reads one with the read_skill tool when the work calls for it, and every read is recorded.
"""

from __future__ import annotations

import ast
import io
import zipfile
from pathlib import Path

from ..skills import MAIN, fingerprint, front_matter, programs

SKILL_CHARS = 20_000  # one skill file as returned to the agent; what it returns is re-sent on every later turn


class SkillsUnusable(Exception):
    """A skill the job lists is not on disk as the job copied it; the run stops before it starts."""


def skill_files(folder: Path) -> dict[str, Path]:
    """The files of one skill by their path inside it. Links are left out: a skill is its own files only."""
    if not folder.is_dir():
        return {}
    found = sorted(p for p in folder.rglob('*') if p.is_file() and not p.is_symlink())
    return {p.relative_to(folder).as_posix(): p for p in found if p.resolve().is_relative_to(folder.resolve())}


def describe(folder: Path) -> dict:
    """What a job records about one skill folder: its name, its one-line description and its fingerprint."""
    files = {path: found.read_bytes() for path, found in skill_files(folder).items()}
    fields = front_matter(files[MAIN].decode('utf-8'))
    return {'name': folder.name, 'description': fields.get('description', ''), 'sha256': fingerprint(files),
            'programs': programs(files)}  # fmt: skip


def load_skills(spec) -> bytes:
    """Check that every skill the job lists is on disk exactly as it was copied, and return them as one zip for the
    sandbox, a folder per skill. A run never starts with a listed skill missing or changed: it would work without
    the user's own rules and still look finished."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for skill in spec.skills:
            name = skill['name']
            files = {path: found.read_bytes() for path, found in skill_files(spec.skills_dir / name).items()}
            if MAIN not in files:
                raise SkillsUnusable(
                    f'The files of skill "{name}" are missing from this run. Start a new run to use it.'
                )
            if skill.get('sha256') and fingerprint(files) != skill['sha256']:
                raise SkillsUnusable(
                    f'The files of skill "{name}" changed after this run was created. Start a new run.'
                )
            for path, content in files.items():
                if path in programs(files):  # checked above as uploaded; the sandbox's copy notes when it starts
                    content = instrument(name, path, content)
                archive.writestr(f'{name}/{path}', content)
    return buffer.getvalue()


def skill_text(job, who: str, name: str, file: str = MAIN) -> str:
    """One file of one of the job's skills, for the agent (who: 'main' or 'review'). A wrong name or file gets an
    answer that lists the right ones, so the agent can correct itself; every read is recorded."""
    names = [s['name'] for s in job.spec.skills]
    if name not in names:
        return f'There is no skill named "{name}". The skills are: {", ".join(names) or "none"}.'
    held = skill_files(job.spec.skills_dir / name)
    file = file or MAIN
    if file not in held:  # also keeps a made-up path from reaching outside the skill
        return f'The skill "{name}" has no file "{file}". Its files are: {", ".join(held)}.'
    text = held[file].read_text(encoding='utf-8', errors='replace')
    read = job.skills_read.setdefault(who, [])
    job.store.event('skill.read', who=who, skill=name, file=file, chars=len(text), first=name not in read)
    if name not in read:
        read.append(name)
    if len(text) > SKILL_CHARS:
        text = text[:SKILL_CHARS] + (
            f'\n[Cut at {SKILL_CHARS:,} characters. The whole file is in the skills zip in your sandbox, under '
            f'{name}/{file}: load it with code and print only the part you need.]'
        )
    return text


RUN_LOG = '/mnt/data/.skill_programs_run'  # in the sandbox: one line per program that started


def instrument(name: str, path: str, source: bytes) -> bytes:
    """A skill's program as the sandbox gets it: the user's code with a few lines added that leave a note when the
    program starts, whether it is run or imported. The note is the run record's evidence that the program was used;
    the text of the agent's scripts would not be, since reading a program is not running it. The lines go after the
    program's opening docstring and __future__ imports, so neither changes meaning. A program that Python cannot
    parse is passed on as it is, and simply leaves no note."""
    try:
        text = source.decode('utf-8')
        body = ast.parse(text).body
    except (UnicodeDecodeError, SyntaxError, ValueError):
        return source
    after = 0
    for index, node in enumerate(body):
        docstring = index == 0 and isinstance(node, ast.Expr) and isinstance(getattr(node, 'value', None), ast.Constant)
        future = isinstance(node, ast.ImportFrom) and node.module == '__future__'
        if not (docstring and isinstance(node.value.value, str)) and not future:
            break
        after = node.end_lineno
    note = (
        '# Added by the app, not part of the skill: notes that this program started, for the run record.\n'
        'try:\n'
        f'    open({RUN_LOG!r}, "a").write({f"{name}/{path}"!r} + "\\n")\n'
        'except Exception:\n'
        '    pass\n'
    )
    lines = text.splitlines(keepends=True)
    if lines[:after] and not lines[after - 1].endswith('\n'):
        lines[after - 1] += '\n'
    return ''.join([*lines[:after], note, *lines[after:]]).encode('utf-8')


def record_runs(job, log: str) -> list[str]:
    """Take the sandbox's note of the programs that started (see instrument) into the run record. Only programs
    the job's skills really hold count; each is recorded the first time it shows up."""
    held = {
        f'{skill["name"]}/{path}': skill['name'] for skill in job.spec.skills for path in skill.get('programs') or ()
    }
    for line in log.splitlines():
        name = line.strip()
        if name in held and name not in job.skills_run:
            job.skills_run.append(name)
            job.store.event('skill.run', skill=held[name], program=name[len(held[name]) + 1 :])
    return job.skills_run
