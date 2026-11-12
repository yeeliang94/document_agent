"""What a job is: a goal, a zip of inputs, and one or more deliverables, each defined by a template or a format.

A job folder (one per test packet) holds job.json and the files it names:

    {
      "id": "payment_journal",
      "module": "ap",                         # which module context applies (payroll, ap, ...)
      "title": "November payment run journal",
      "goal": "Prepare the payment journal for ...",
      "zip": "packet.zip",
      "deliverables": [
        {"name": "filled_output.xlsx", "template": "output_template.xlsx"},
        {"name": "payment_journal.xlsx", "format": "journal_format.json"}
      ],
      "rules": [ ... ],                       # optional extra rules, see rules.py
      "user_answers": "user_answers.json"     # optional, tests only: scripted replies to ask_user
    }

A deliverable has either a template (a workbook to fill, whose own content and data-validation rules must survive) or
a format (a workbook built from scratch to a written column specification; see rules.py for the format schema).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .rules import check_format_definition, check_rule_definitions

DEFAULT_ANSWER = (
    'No further information is available right now. Make the most reasonable choice, record it in notes.md and list '
    'it in open_questions.'
)


@dataclass(frozen=True)
class Deliverable:
    name: str
    template: Path | None = None
    format: dict | None = None
    format_path: Path | None = None


@dataclass(frozen=True)
class JobSpec:
    id: str
    module: str
    goal: str
    zip: Path
    deliverables: tuple[Deliverable, ...]
    title: str = ''
    rules: tuple[dict, ...] = ()
    answers: tuple[dict, ...] = ()
    folder: Path | None = None
    context: str = ''  # module context (house rules), added by the caller

    @property
    def inputs(self) -> list[Path]:
        """Files a run copies into its record and uploads to the sandbox, besides the zip."""
        found = []
        for d in self.deliverables:
            found += [p for p in (d.template, d.format_path) if p is not None]
        return found


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f'Cannot read {path}: {error}') from error


def load_job(path: Path) -> JobSpec:
    """Read job.json; every file it names is relative to its folder."""
    path = Path(path)
    if path.is_dir():
        path = path / 'job.json'
    folder = path.parent
    data = _read_json(path)
    missing = [k for k in ('id', 'goal', 'zip', 'deliverables') if not data.get(k)]
    if missing:
        raise SystemExit(f'{path} is missing {missing}.')
    deliverables = []
    for item in data['deliverables']:
        if bool(item.get('template')) == bool(item.get('format')):
            raise SystemExit(f'{path}: deliverable {item.get("name")!r} needs exactly one of "template" or "format".')
        if item.get('template'):
            deliverables.append(Deliverable(item['name'], template=folder / item['template']))
        else:
            spec_path = folder / item['format']
            spec = _read_json(spec_path)
            try:
                check_format_definition(spec)
            except ValueError as error:
                raise SystemExit(f'{spec_path.name}: {error}') from error
            deliverables.append(Deliverable(item['name'], format=spec, format_path=spec_path))
    answers = _read_json(folder / data['user_answers']) if data.get('user_answers') else []
    try:
        check_rule_definitions(data.get('rules', []))
    except ValueError as error:
        raise SystemExit(f'{path}: {error}') from error
    job = JobSpec(
        id=data['id'],
        module=data.get('module', 'payroll'),
        title=data.get('title', ''),
        goal=data['goal'],
        zip=folder / data['zip'],
        deliverables=tuple(deliverables),
        rules=tuple(data.get('rules', [])),
        answers=tuple(answers),
        folder=folder,
    )
    for needed in [job.zip, *job.inputs]:
        if not needed.exists():
            raise SystemExit(f'{path} names {needed.name}, which is not in {folder}.')
    return job


def adhoc_job(zip_path: Path, template: Path, goal: str, module: str = 'payroll') -> JobSpec:
    """A job from the command line: one zip, one template, one goal."""
    return JobSpec(
        id='adhoc',
        module=module,
        goal=goal,
        zip=Path(zip_path),
        deliverables=(Deliverable('filled_output.xlsx', template=Path(template)),),
    )


@dataclass
class Answerer:
    """Replies to ask_user. Tests use scripted answers matched by keywords; otherwise a callback (a person)."""

    scripted: tuple[dict, ...] = ()
    ask: object = None  # optional async callable(question: str) -> str
    log: list[dict] = field(default_factory=list)

    async def answer(self, question: str) -> str:
        if self.ask is not None:
            reply = await self.ask(question)
        else:
            text = question.casefold()
            reply = next(
                (a['answer'] for a in self.scripted if any(str(k).casefold() in text for k in a.get('keywords', []))),
                DEFAULT_ANSWER,
            )
        self.log.append({'question': question, 'answer': reply})
        return reply
