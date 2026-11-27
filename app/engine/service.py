"""Runs main-app jobs on the sandbox engine.

It turns an app job into a JobSpec, steers the run from the job record (pause, cancel, new instructions, questions),
shows the engine's events in the activity feed, and settles the job's status and outputs. Each run of a job gets its
own record under <job>/engine/run-NN/; outputs always live in <job>/outputs/, where the app serves them.

A question holds the sandbox for a few minutes (a Settings-page value) while the user answers in the app. If no
answer comes in time, the run stops with status "waiting"; the answer then starts a new run in a fresh sandbox,
which picks up from the saved outputs, notes and the question and answer (the resume card).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

from .. import clients, feedback, lessons, plain_errors, preview
from ..jobstatus import FINISHED, stop_status
from ..modules import MODULE_LABELS
from ..store import clean
from . import plan as plans
from . import report as finish
from .agent import Control, run_job
from .endpoint import Settings as Endpoint
from .jobspec import DEFAULT_ANSWER, Answerer, Deliverable, JobSpec
from .pastruns import PAST_LIMIT, past_files

POLL_SECONDS = 1.0
SECONDS_PER_MINUTE = 60  # tests shorten the question wait through this
NOTES_LIMIT = 6000


def count(n, word: str) -> str:
    """'1 file', '3 files': the feed says a number the way a person would."""
    return f'{n} {word}' + ('' if n == 1 else 's')


# What the activity feed says for each engine event; events not listed are kept in the run record only.
MESSAGES = {
    'run.started': lambda d: f'Started on {count(d.get("files"), "file")}',
    'sandbox.ready': lambda d: 'Set up the workspace and copied in the files',
    'code.started': lambda d: 'Working on the files',
    'tool.called': lambda d: None if d.get('tool') in OWN_LINE else f'Using {d.get("tool")}',
    'tool.failed': lambda d: tool_failure(d.get('error')),
    'scout.started': lambda d: f'A helper is reading {d.get("folder")} ({count(d.get("files"), "file")})',
    'scout.finished': lambda d: f'The helper finished reading {d.get("folder")}',
    'scout.failed': lambda d: f'The helper stopped part way through {d.get("folder")}',
    'outputs.saved': lambda d: (
        'Saved the work; the checks passed'
        if d.get('passed')
        else f'Saved the work; {count(len(d.get("issues") or []), "problem")} to fix'
    ),
    'question.asked': lambda d: f'Question for you: {d.get("question")}',
    'question.answered': lambda d: 'Answer received',
    'review.started': lambda d: 'An independent reviewer is checking the result',
    'review.skipped': lambda d: f'No review: {d.get("reason")}',
    'review.finished': lambda d: f'The reviewer finished: {d.get("verdict")}, {count(d.get("doubts"), "doubt")}',
    'skill.read': lambda d: f'Reading skill {d.get("skill")}' if d.get('first') else None,  # once per skill
    'skill.run': lambda d: f'Running {d.get("program")} from skill {d.get("skill")}',
    'rules.held': lambda d: 'The checks passed; now checking the result against your house rules',
    'past_run.opened': lambda d: f'Looking at an earlier run: {d.get("title")}',
    'instructions.delivered': lambda d: (
        'Stopped the current step to read your message' if d.get('interrupted') else 'Your message reached the agent'
    ),
    'sandbox.keepalive_failed': lambda d: 'The sandbox did not answer a keep-alive check',
    'run.finished': lambda d: f'Run finished: {d.get("status")}',
    # A script, as it streams in: what it is for once its opening comment is written, then the files it names.
    'code.writing': lambda d: d.get('summary'),
    'code.running': lambda d: d.get('summary') or ('Reading' if d.get('files') else None),
    'code.finished': lambda d: d.get('summary') or 'Worked on the files',
    # At the end of a model reply. Shown only when the stream did not already show it, and the script opened with a
    # comment saying what it is for.
    'code.ran': lambda d: None if d.get('shown') else d.get('summary'),
    'limit.near': lambda d: f'{d.get("said")}. The agent is saving its work and writing down what is still open',
    'limit.extended': lambda d: f'Gave the agent {d.get("added")} more steps; it can now use {d.get("max_turns")}',
    'limit.after_pass': lambda d: 'A limit was reached after the work passed its checks; the checked result stands',
    # No size: one reply can grow past the limit by itself, so the size it started at says little.
    'history.condensed': lambda d: 'Condensed the older conversation to keep it small',
    'model.retry': lambda d: 'OpenAI dropped a reply; carrying on',
    'model.item_refused': lambda d: (
        'OpenAI could not find an earlier sandbox step; it is sent as a note and the run carries on'
    ),
    'model.unavailable': lambda d: 'The AI service stopped answering; saved work is kept',
    'outputs.versioned': lambda d: f'Kept version {d.get("number")}: {d.get("description")}',
    'output.looked': lambda d: f'Looking at {d.get("file")} as Excel shows it',
    # Kept in the Technical log; the run itself shows no thinking then, and the Settings page says why that can be.
    'thinking.refused': lambda d: (
        "The AI service does not send summaries of the agent's thinking; carrying on without them"
    ),
    'outputs.pictured': lambda d: (
        f'Showed the agent {count(d.get("pictures"), "picture")} of its files before it finishes '
        f'(about {d.get("tokens") or 0:,} tokens)'
    ),
}
WARNINGS = {'limit.near', 'tool.failed', 'scout.failed', 'sandbox.keepalive_failed', 'model.unavailable',
            'thinking.refused'}  # fmt: skip
# Tools whose work the feed already says in its own words (a save, a look at a file, the plan), so "Using ..." is not.
OWN_LINE = {'update_plan', 'save_outputs', 'look_at_output'}
STAGE_NAMES = {'reading': 'Reading files', 'filling': 'Filling in', 'checking': 'Checking'}


def plan_message(data: dict) -> str:
    """What the feed says when the agent writes or changes its plan."""
    if data.get('first'):
        return f'Made a plan: {plans.counted(data.get("steps") or [])}'
    said = [f'added "{s}"' for s in data.get('added') or []]
    said += [f'changed "{c["was"]}" to "{c["step"]}"' for c in data.get('changed') or []]
    said += [f'dropped "{s}"' for s in data.get('removed') or []]
    if said:
        return 'Changed its plan: ' + '; '.join(said)
    return f'Updated its plan: {plans.counted(data.get("steps") or [])}'


DAY_USED_UP = (
    "Today's token limit is used up. Saved work is kept. Continue tomorrow, or raise the limit on the Settings page."
)

def tool_failure(raw) -> str:
    """A tool failure for the feed: the plain reason when it is a known one; the agent itself is told the raw text."""
    found = plain_errors.explain(raw)
    known = found is not None and found.message != plain_errors.UNKNOWN[0]
    return f'A tool failed: {found.message}' if known else 'A tool the agent used failed; it was told why and carries on'


def message_text(message) -> str:
    """A message as the agent reads it. Files the user added with it are named by their path in the packet, so the
    agent can find them, cite them and give each one a decision like any other packet file."""
    if isinstance(message, str):
        return message
    text = message.get('text') or ''
    names = [f['packet'] for f in message.get('files') or ()]
    if names:
        text = (text + '\n' if text else '') + 'Files the user added to the packet: ' + ', '.join(names) + '.'
    return text


def picture_of(path: Path) -> dict | None:
    """An added picture, so the agent sees it as well as having the file. None for any other file. It is made as small
    as a page the user pins a comment to, and a JPEG unless it has see-through parts: it travels in every later
    request, and the agent has the full file in its sandbox to enlarge any part."""
    if path.suffix.lower() not in preview.IMAGES:
        return None
    try:
        from PIL import Image

        with Image.open(path) as image:
            if image.width * image.height > feedback.MAX_IMAGE_PIXELS:
                return None
            image.seek(0)  # the first frame of an animation
            image.thumbnail((preview.PAGE_PIXELS, preview.PAGE_PIXELS))
            out = io.BytesIO()
            if 'A' in image.getbands() or image.info.get('transparency') is not None:
                image.convert('RGBA').save(out, 'PNG')
                kind = 'image/png'
            else:
                image.convert('RGB').save(out, 'JPEG', quality=85)
                kind = 'image/jpeg'
        return {'name': path.name, 'data': out.getvalue(), 'type': kind}
    except Exception:  # noqa: BLE001 - a picture that does not open is still in the packet for the agent to read
        return None


class AppControl(Control):
    """Reads the job record at most once a second, to learn about pauses, cancels and new instructions."""

    def __init__(self, store, job_id: str):
        self.store, self.job_id = store, job_id
        self.waiting = False  # set when a question timed out: stop and wait for the answer
        self._checked = 0.0
        self._job: dict = {}

    def _current(self) -> dict:
        now = time.monotonic()
        if now - self._checked >= POLL_SECONDS:
            self._checked = now
            self._job = self.store.get(self.job_id)
        return self._job

    def stop_requested(self) -> str | None:
        if self.waiting:
            return 'waiting'
        return stop_status(self._current().get('status', ''))

    def new_messages(self) -> list:
        """What the user sent since the last look: messages typed in the conversation, with any files added to the
        packet, and rounds of comments pinned to the files (app/feedback.py), which come with their pictures."""
        if not self._current().get('pending_messages'):
            return []
        taken: list = []

        def take(job):
            taken.extend(job.get('pending_messages') or [])
            job['pending_messages'] = []
            job['interrupt'] = False  # an interrupt asked for the messages taken here
            # Kept for good: a resumed run in a fresh sandbox must still follow them.
            texts = [message_text(m) for m in taken]
            job['engine_instructions'] = [*(job.get('engine_instructions') or []), *texts]
            rounds = {m['round'] for m in taken if isinstance(m, dict) and 'round' in m}
            for entry in job.get('feedback') or []:
                if entry['number'] in rounds:
                    entry['status'] = 'delivered'

        self.store.mutate(self.job_id, take)
        self._job = {**self._job, 'pending_messages': [], 'interrupt': False}
        root, messages = self.store.path(self.job_id), []
        for item in taken:
            if isinstance(item, str):
                messages.append(item)
            elif 'round' in item:
                self.store.event(
                    self.job_id, 'feedback.delivered', 'Your comments reached the agent', type='engine',
                    round=item['round'],
                )  # fmt: skip
                images = [{'name': i['name'], 'data': (root / i['path']).read_bytes()} for i in item['images']]
                messages.append({'text': item['text'], 'images': images})
            elif item.get('files'):
                added = [{'packet': f['packet'], 'local': root / f['path']} for f in item['files']]
                images = [p for f in added if (p := picture_of(f['local'])) is not None]
                messages.append({'text': message_text(item), 'images': images, 'files': added})
            else:
                messages.append(item['text'])
        return messages

    def more_turns(self) -> int:
        if not self._current().get('more_turns'):
            return 0
        taken = []

        def take(job):
            taken.append(job.get('more_turns') or 0)
            job['more_turns'] = 0

        self.store.mutate(self.job_id, take)
        self._job = {**self._job, 'more_turns': 0}
        return taken[0]

    def interrupt_requested(self) -> bool:
        job = self._current()
        return bool(job.get('interrupt') and job.get('pending_messages'))

    @property
    def can_remember(self) -> bool:
        return bool((self._current().get('playbook') or {}).get('id'))

    def remember(self, comment: str, lesson: str, scope: str = 'playbook') -> str:
        return lessons.note(self.store, self.job_id, comment, lesson, scope)

    def follow_client(self, name: str) -> dict:
        return clients.follow(self.store, self.job_id, name)

    def answer(self, answers: list[dict]) -> dict:
        return feedback.answer(self.store, self.job_id, answers)

    def after_save(self) -> dict:
        """After comments, tell the agent what its save changed where they asked and what else changed."""
        try:
            report = feedback.change_report(self.store, self.job_id)
        except Exception as error:  # noqa: BLE001 - say so to the agent; its save worked
            return {
                'comment_changes': {
                    'unavailable': f'The comparison could not be made ({type(error).__name__}: {error}).'
                }
            }
        return {'comment_changes': report} if report else {}


def packet_zip(root: Path, files: list[dict], folder: Path) -> Path:
    """The job's source files as one zip, paths relative to inputs/. Its name comes from what it holds, so a file the
    user adds later makes a new zip and an older one is never reused for it. A run keeps its own link to the zip it
    started with, so older zips can go. Compressed, because OpenAI sandboxes take files of at most 50 MB."""
    inputs = sorted((e['path'], e['sha256']) for e in files if e['role'] == 'inputs')
    target = folder / f'packet-{hashlib.sha256(json.dumps(inputs).encode()).hexdigest()[:16]}.zip'
    if target.exists():
        return target
    folder.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix('.partial')
    with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, _ in inputs:
            archive.write(root / path, path.removeprefix('inputs/'))
    partial.replace(target)
    for older in folder.glob('packet*.zip'):
        if older != target:
            older.unlink(missing_ok=True)
    return target


def job_title(job: dict) -> str:
    """One line that tells runs of a playbook apart: the month, and how the user's own goal begins."""
    goal = ' '.join((job.get('instructions') or '').split())
    return ' - '.join(part for part in (job.get('period') or '', goal[:80]) if part) or 'Untitled run'


def choose_past_runs(store, job: dict) -> list[dict]:
    """The finished runs of the same playbook that this job may look at, newest first. Chosen once, when the job
    first runs, and kept on the job: a run that continues later sees the same list under the same labels."""
    if job.get('past_runs') is not None:
        return job['past_runs']
    client = (job.get('client') or {}).get('id')
    same_client, no_client = [], []
    for other in store.jobs():  # newest first
        same = other['id'] != job['id'] and other['module'] == job['module'] and other['status'] == 'completed'
        same = same and not other.get('replay')  # a replayed test is not work the user accepted
        if not same or other['created_at'] > job['created_at']:
            continue
        # A run for a client is shown that client's earlier runs first, then runs for no client, never another
        # client's: their layout and conventions are that client's own.
        theirs = (other.get('client') or {}).get('id')
        group = same_client if client and theirs == client else no_client if not client or not theirs else None
        if group is not None and len(group) < PAST_LIMIT and past_files(store.path(other['id']) / 'outputs'):
            group.append(other)
        if len(same_client if client else no_client) == PAST_LIMIT:
            break
    found = [
        {'label': f'earlier-{n}', 'id': other['id'], 'title': job_title(other), 'finished': other['updated_at'][:10],
         **({'client': other['client']['name']} if other.get('client') else {})}
        for n, other in enumerate((same_client + no_client)[:PAST_LIMIT], 1)
    ]  # fmt: skip
    store.mutate(job['id'], lambda j: j.setdefault('past_runs', found))
    return found


def keep_copy(outputs: Path, copy: Path) -> None:
    """A fixed copy of a finished run's files, put in place whole: a reader never sees half of it."""
    building = copy.with_name(copy.name + '.part')
    shutil.rmtree(building, ignore_errors=True)
    for path in past_files(outputs):
        target = building / path.relative_to(outputs)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    building.mkdir(parents=True, exist_ok=True)
    building.rename(copy)


def past_runs(store, job: dict) -> tuple[dict, ...]:
    """The job's earlier runs with what the engine needs to open one. Left out: a run deleted since, and a run
    that is no longer finished because the user sent it back with comments. Its files are then work in progress;
    it is offered again, under the same label, once it finishes. Each is read from a copy this job takes while that
    run is finished: several runs go at once, so the run itself may be sent back and change while this one reads it."""
    held = []
    for run in choose_past_runs(store, job):
        try:
            other = store.get(run['id'])
        except KeyError:
            continue
        if other['status'] != 'completed':
            continue
        copy = store.path(job['id']) / 'past' / run['label']
        if not copy.is_dir():
            keep_copy(store.path(run['id']) / 'outputs', copy)
        held.append({**run, 'goal': (other.get('instructions') or '')[:1000], 'outputs': copy})
    return tuple(held)


def job_spec(store, job: dict) -> JobSpec:
    """A template (.xlsx) makes a fill-the-template deliverable; a format (.json, see engine/rules.py) makes a
    workbook built to that format; a job with neither leaves the form of the output to the agent."""
    root = store.path(job['id'])
    given = next((e['path'] for e in job['files'] if e['role'] == 'template'), None)
    if given is None:  # neither a template nor a format: the agent decides what files the goal calls for
        deliverables = ()
    elif (definition := root / given).suffix.lower() == '.json':
        spec = json.loads(definition.read_text(encoding='utf-8-sig'))
        deliverables = (Deliverable(f'{definition.stem}.xlsx', format=spec, format_path=definition),)
    else:
        deliverables = (Deliverable(f'{definition.stem}_output.xlsx', template=definition),)
    playbook = job.get('playbook') or {}  # absent on jobs made before playbooks
    name = playbook.get('name') or MODULE_LABELS.get(job['module'], job['module'])
    goal = (job.get('instructions') or '').strip() or f'Prepare the {name} deliverable from the supplied files.'
    return JobSpec(
        id=job['id'],
        module=job['module'],
        playbook=playbook.get('name', ''),
        playbook_id=playbook.get('id', ''),
        playbook_version=playbook.get('version'),
        skills=tuple(job.get('skills') or ()),
        skills_dir=root / 'skills',
        past_runs=past_runs(store, job),
        goal=goal + (f'\nReporting month: {job["period"]}.' if job.get('period') else ''),
        zip=packet_zip(root, job['files'], root / 'working' / 'engine'),
        deliverables=deliverables,
        context=job.get('context') or '',
        client=(job.get('client') or {}).get('name', ''),
        client_notes=job.get('client_notes') or '',
        known_clients=() if job.get('client') else tuple(c['name'] for c in store.clients.list()),
    )


def resume_card(store, job: dict, new: list[str] = ()) -> str | None:
    """What a fresh sandbox needs to continue an earlier run instead of starting over. new: what the user sent
    since that run ended, which this run is starting in order to act on."""
    root = store.path(job['id'])
    runs = sorted((root / 'engine').glob('run-*/run.json'))
    if not runs:
        return None
    last = json.loads(runs[-1].read_text(encoding='utf-8'))
    if job.get('start_over'):  # the user set the earlier work aside: what still holds is what they said and answered
        parts = [
            'You are starting this job over in a fresh sandbox. The user chose to set the earlier work aside: the '
            'outputs folder is empty and notes.md is gone. Do the job from the beginning, as on a first run.'
        ]
        return '\n\n'.join(parts + _still_holds(job, new, fresh=True))
    parts = [
        'You are resuming this job in a fresh sandbox; the earlier sandbox is gone. Continue from where the work '
        f'stands rather than starting over. The previous run ended as "{last.get("status")}"{why_stopped(last)}.'
    ]
    start = job.get('resume_from')
    if start:
        parts.append(
            f'The user chose to continue from version {start["number"]}, saved at step {start["step"]} of '
            f'{start["run"]} ({start["description"]}). Your outputs and notes.md are as they were then; anything '
            'done after that version was set aside. Check what that version still needs before you change it.'
        )
    check = last.get('final_check') or {}
    if check.get('issues'):
        parts.append('The last check listed:\n' + '\n'.join(f'- {i}' for i in some_issues(check['issues'], total=check.get('issue_count'))))
    notes = root / 'outputs' / 'notes.md'
    if notes.exists():
        parts.append('Your notes so far:\n' + notes.read_text(encoding='utf-8', errors='replace')[:NOTES_LIMIT])
    steps = (job.get('plan') or {}).get('steps')
    if steps:
        parts.append(
            'Your plan from the earlier run, as you left it. Carry on from it with update_plan: tick what is done, and '
            'change it, saying why, if the work now is different:\n' + plans.as_text(steps)
        )
    return '\n\n'.join(parts + _still_holds(job, new))


CARD_ISSUES = 20  # check issues on the resume card
MORE = re.compile(r'\.\.\. and (\d+) more\.')


def without_offer(report: dict | None, offer: dict | None) -> dict | None:
    """The report, less its lines to keep for the client once the person saved or put away this run's offer."""
    if report and offer and (offer.get('saved') or offer.get('dismissed')):
        return {k: v for k, v in report.items() if k != 'for_next_time'}
    return report


def why_stopped(last: dict) -> str:
    """Why an earlier run ended, for the resume card: a limit, the AI service, the user, or a fault."""
    cut, status, error = last.get('cut_short'), last.get('status'), last.get('error')
    if cut in ('turns', 'tokens', 'time'):
        return f', because it reached a limit ({error or cut})'
    if cut == 'service':
        return ', because the AI service stopped answering'
    if status == 'waiting':
        return ', because it was waiting for an answer to its question'
    if status == 'paused':
        return ', because the user paused it'
    if status == 'cancelled':
        return ', because the user stopped it'
    if status == 'failed' and error:
        return f', because of a fault ({error[:200]})'
    return ''


def some_issues(issues: list[str], limit: int = CARD_ISSUES, total: int | None = None) -> list[str]:
    """The first issues, and how many were left out. `total` is how many the checks found (issue_count); a record
    from before it was kept is read from the checks' own closing '... and N more.' line."""
    more = MORE.fullmatch(issues[-1]) if issues else None
    real = issues[:-1] if more else issues
    if total is None:
        total = len(real) + (int(more.group(1)) if more else 0)
    shown = real[:limit]
    return shown + ([f'... and {total - len(shown)} more.'] if total > len(shown) else [])


def _still_holds(job: dict, new, fresh: bool = False) -> list[str]:
    """What the user said and answered in earlier runs, and what they sent since, for the card of a fresh sandbox."""
    parts = []
    told = job.get('engine_instructions') or []
    if told:
        parts.append(
            'Instructions the user gave during earlier runs. They still apply, and override the goal where they '
            'differ:\n' + '\n'.join(f'- {m}' for m in told)
        )
    asked = job.get('engine_questions') or []
    if asked:
        parts.append("Questions you asked and the user's answers:\n" + '\n'.join(_asked(q) for q in asked))
    if new and fresh:
        parts.append('New from the user since that run ended. Take it into account as you do the job:\n' + '\n'.join(new))
    elif new:
        parts.append(
            'New from the user since that run ended. This is what to act on now: decide what it changes, redo only '
            'the affected work, and save again:\n' + '\n'.join(new)
        )
    return parts


def run_spent(parts: list[dict], seconds: float) -> dict:
    """What one engine run took, from its lead, scout and review usage entries: an entry of the job's runs.
    cached_input_tokens is the part of the input the service had cached, which costs less (app/costs.py)."""
    return {
        'seconds': seconds,
        'requests': sum(p.get('requests', 0) for p in parts),
        **{k: sum(p.get(k, 0) for p in parts) for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')},
    }


def shown_issues(issues: list[str], limit: int = 10) -> list[str]:
    more = len(issues) - limit
    return issues[:limit] + ([f'... and {more} more.'] if more > 0 else [])


def _asked(q: dict) -> str:
    """One earlier question for the resume card, with the evidence and choices the user saw beside it."""
    lines = [f'- Q: {q["question"]}']
    lines += [f'  Evidence: {e.get("value")} ({", ".join(filter(None, [e.get("file"), e.get("where")]))})'
              for e in q.get('evidence') or []]  # fmt: skip
    lines += [f'  Choice: {c.get("label")}' + (' (you suggested this)' if c.get('suggested') else '')
              + (f' - {c["detail"]}' if c.get('detail') else '') for c in q.get('choices') or []]  # fmt: skip
    return '\n'.join([*lines, f'  A: {q["answer"]}'])


def take_answer(job: dict) -> None:
    """Move an answered question into the job's question history (store.mutate callback), keeping the evidence and
    choices it was asked with, so a run that resumes later reads the answer in context."""
    if job.get('question') and job.get('answer'):
        asked = job.get('engine_questions') or []
        q = job['question']
        entry = {'question': q['text'], 'answer': job['answer']}
        entry.update({k: q[k] for k in ('choices', 'evidence') if q.get(k)})
        job['engine_questions'] = [*asked, entry]
    job.update(question=None, answer=None)


class SandboxEngine:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    def endpoint(self, options: dict) -> Endpoint:
        return Endpoint(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            model=self.settings.model,
            provider=options['litellm_provider'] or None,
            model_on_create=options['model_on_create'],
        )

    async def run(self, job_id: str):
        store = self.store
        # The day's limit covers every run, the replayed tests too. A run that would start past it waits, with its
        # answer and comments kept; one that starts close to it gets only what is left of the day.
        left = store.tokens_left_today()
        if store.day_used_up():
            store.update(job_id, status='paused', error=DAY_USED_UP, cut_short='day')
            store.event(job_id, 'job.paused', DAY_USED_UP, type='job', level='WARN')
            return
        store.mutate(job_id, lambda j: (take_answer(j), j.update(status='running', error=None)))
        options = store.settings()
        allowance = options['token_limit_thousands'] * 1000
        day_bound = left is not None and left < allowance
        if left is not None:  # set aside now, before any wait, so a run starting alongside sees the rest only
            store.hold(job_id, min(left, allowance))
        team = {'max_scouts': options['max_scouts'], 'reviewer': options['reviewer']}
        job = store.update(job_id, engine_team=team)
        root = store.path(job_id)
        attempt = len(list((root / 'engine').glob('run-*'))) + 1
        control = AppControl(store, job_id)
        # A run that continues an earlier one opens with whatever the user sent in between, so it starts on that at
        # once. (job was read before these are taken, so the card lists them as new, not as earlier instructions.)
        fresh = await asyncio.to_thread(control.new_messages) if attempt > 1 else []
        resume = resume_card(store, job, [m if isinstance(m, str) else m['text'] for m in fresh])
        # The card repeats the text of every round of comments still being acted on, those just taken included;
        # the pictures that text names go with it.
        pictures = tuple(await asyncio.to_thread(feedback.open_pictures, store, job_id)) if resume else ()
        pictures += tuple(i for m in fresh if isinstance(m, dict) and m.get('files') for i in m['images'])
        # The packet is built from the file list as it is now, after the messages were taken: a file sent with one of
        # them a moment ago is in it, though not in `job`, which was read before.
        spec = await asyncio.to_thread(job_spec, store, store.get(job_id))

        def on_event(name: str, data: dict):
            if name == 'run.progress':  # how far the run has got, on the job for the screen; not a feed line
                store.update(job_id, progress={**data, 'run': attempt})
                return
            if name == 'plan.updated':
                # Kept on the job, so the plan shows at the top of the run and a later run picks it up.
                store.update(job_id, plan={'steps': data['steps'], 'why': data.get('why') or '', 'run': attempt})
                keep = {k: data.get(k) for k in ('steps', 'added', 'changed', 'removed', 'why', 'first')}
                store.event(job_id, 'engine.plan.updated', plan_message(data), type='engine', run=attempt, **keep)
                return
            if name == 'stage.changed':  # where the work moves on; the run screen folds the work by stage
                stage = data.get('stage')
                store.event(job_id, 'engine.stage.changed', STAGE_NAMES.get(stage, stage), type='engine', run=attempt,
                            stage=stage)  # fmt: skip
                return
            if name == 'thinking':
                parts = data.get('parts') or []
                store.event(job_id, 'engine.thinking', parts[0]['title'] if parts else '', type='engine', run=attempt,
                            parts=parts, detail=options['thinking_summaries'])  # fmt: skip
                return
            if name == 'code.ran' and data.get('shown'):
                # The script is already in the feed: this says only where its code and output are kept.
                store.event(job_id, 'engine.code.saved', 'Kept the script', type='engine', run=attempt,
                            code=data.get('file'), item=data.get('item'))  # fmt: skip
                return
            message = MESSAGES.get(name)
            text = message(data) if message is not None else None
            if text:
                level = 'WARN' if name in WARNINGS else 'INFO'
                extra = {'files': data['files']} if isinstance(data.get('files'), list) else {}
                if name == 'limit.near':
                    # Which limit (only steps can be added from the screen) and its short wording, for the card.
                    extra.update(what=data.get('what'), said=data.get('said'))
                if data.get('interrupted'):
                    extra['interrupted'] = True  # the thread says the step was stopped only once it was
                if data.get('item'):  # which script, so the screen can open its code and what it printed
                    extra['item'] = data['item']
                if name == 'code.ran' and data.get('file'):
                    extra['code'] = data['file']
                if name == 'outputs.saved':  # what came back, for the action opened in full
                    extra.update(passed=bool(data.get('passed')), issues=list(data.get('issues') or [])[:20])
                store.event(job_id, 'engine.' + name, text, type='engine', level=level, run=attempt, **extra)

        one_question = asyncio.Lock()  # the agent may call tools in parallel; the user sees one question at a time

        async def ask(question: str, details: dict) -> str:
            if job.get('replay'):  # a replayed test has nobody to ask; the agent decides and notes it
                return DEFAULT_ANSWER
            async with one_question:
                if control.waiting:  # an earlier question is already parked for the user
                    return (
                        'Another question is waiting for the user. End your turn now; the job resumes with the answer.'
                    )
                return await ask_one(question, details)

        async def ask_one(question: str, details: dict) -> str:
            stopped = []

            def post(j):
                if j['status'] in ('pausing', 'cancelled'):
                    stopped.append(j['status'])
                else:
                    choices, evidence = details.get('choices') or [], details.get('evidence') or []
                    asked = {'text': question, 'call_id': 'sandbox', 'choices': choices, 'evidence': evidence}
                    j.update(status='waiting', question=asked, answer=None)

            store.mutate(job_id, post)
            if stopped:
                return 'The user stopped the job. End your turn now.'
            deadline = time.monotonic() + options['question_wait_minutes'] * SECONDS_PER_MINUTE
            try:
                while True:
                    current = store.get(job_id)
                    if current['status'] == 'cancelled':
                        return 'The user cancelled the job. End your turn now.'
                    if current.get('answer'):
                        answer = current['answer']
                        store.mutate(job_id, lambda j: (take_answer(j), j.update(status='running')))
                        return answer
                    if time.monotonic() >= deadline:  # checked after one last look, so an answer just in time counts
                        break
                    await asyncio.sleep(POLL_SECONDS)
            except asyncio.CancelledError:
                # The run's own time limit ended the wait: keep the question, so the user's answer resumes the job.
                control.waiting = True
                raise
            control.waiting = True
            return (
                'The user has not answered yet, and this sandbox will not be kept. The job resumes with their answer '
                'in a fresh one, which starts from your saved outputs and notes.md. So first write in notes.md what '
                'you found, what you decided and what is still open, and call save_outputs (problems it lists can '
                'wait for the answer). Then end your turn with status partial.'
            )

        run_id = f'run-{attempt:02d}'
        try:
            record = await self._run(spec, options, root, run_id, on_event, control, resume, pictures, ask, job,
                                     left if day_bound else allowance)  # fmt: skip
        except BaseException:
            # Cut off before it could settle (the app stopping, the backstop time limit): what it used still counts
            # towards the day. The engine writes its record as it ends, so read the usage from there.
            await asyncio.shield(asyncio.to_thread(self.count_unsettled, job_id, root / 'engine' / run_id))
            raise
        if day_bound and record.get('cut_short') == 'tokens':
            record.update(error=DAY_USED_UP, cut_short='day')
        await asyncio.to_thread(self.settle, job_id, record, control.waiting)

    def count_unsettled(self, job_id: str, run_folder) -> None:
        try:
            record = json.loads((run_folder / 'run.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        usage = record.get('usage') or {}
        parts = [*usage.get('lead', []), *usage.get('scouts', []), *usage.get('reviews', [])]
        spent = run_spent(parts, record.get('seconds') or 0)
        self.store.add_spend(spent['input_tokens'] + spent['output_tokens'])
        # It is one of the job's runs too, so the job's whole time, usage and cost count it.
        try:
            self.store.mutate(job_id, lambda job: job.update(runs=[*(job.get('runs') or []), spent]))
        except KeyError:  # the job was removed meanwhile
            pass

    async def _run(
        self, spec, options, root, run_id, on_event, control, resume, pictures, ask, job, max_tokens
    ) -> dict:
        return (
            await run_job(
                spec,
                self.endpoint(options),
                root / 'engine',
                run_id=run_id,
                outputs=root / 'outputs',
                on_event=on_event,
                control=control,
                resume=resume,
                resume_images=pictures,
                answerer=Answerer(ask=ask, log=list(job.get('engine_questions') or [])),
                max_scouts=options['max_scouts'],
                review_on=options['reviewer'],
                proof=options['proof'],
                effort=options['effort'],
                memory=options['sandbox_memory'],
                max_turns=options['max_turns'],
                max_tokens=max_tokens,
                condense_at=options['condense_at_thousands'] * 1000,
                look_before_finish=options['look_before_finish'],
                versions=root / 'versions',
                versions_kept=options['versions_kept'],
                max_questions=options['max_questions'],
                timeout_seconds=options['time_limit_minutes'] * 60,
                thinking=options['thinking_summaries'],
                plan=None if job.get('start_over') or job.get('resume_from') else (job.get('plan') or {}).get('steps'),
                echo=lambda _: None,
            )
        ).record

    def artifacts(self, job_id: str, validated: bool) -> list[dict]:
        outputs = self.store.path(job_id) / 'outputs'
        items = []
        for path in sorted(outputs.rglob('*')):  # a file the agent saved in a folder of its own is an output too
            if path.is_file() and not path.name.endswith('.pending'):
                name = path.relative_to(outputs).as_posix()
                item = {'path': 'outputs/' + name, 'name': name, 'size': path.stat().st_size}
                if path.suffix.lower() == '.xlsx':
                    item['status'] = 'validated' if validated else 'draft'
                items.append(item)
        return items

    def settle(self, job_id: str, record: dict, waiting: bool):
        store = self.store
        check = record.get('final_check') or {}
        final_report = finish.from_answer(record.get('summary'))
        # The line the run screen shows until it shows the whole report (plan Step 27): the headline, then what to check.
        summary = (final_report or {}).get('headline', '')
        if (final_report or {}).get('to_check'):
            summary += ' Check before sending: ' + ' '.join(final_report['to_check'])
        unresolved = (record.get('summary') or {}).get('open_questions', [])
        status = {
            'complete': 'completed',  # run_job already turns a complete claim with failing checks into partial
            'partial': 'partial',
            'stopped': 'partial',  # a turn, time or token limit: saved work is kept and the user can continue
            'paused': 'paused',
            'cancelled': 'cancelled',
        }.get(record['status'], 'failed')
        if waiting:
            status = 'waiting'
        raw = clean(record.get('error') or '') or None  # kept for the Technical log
        error = plain_errors.sentence(raw)
        added = error if status == 'partial' and error else ''
        if status == 'partial' and check.get('issues'):
            left = check['issues']
            added += '\nStill to fix: ' + ' '.join(left[:3]) + (f' ... and {len(left) - 3} more.' if len(left) > 3 else '')
        added = added.strip()
        summary = '\n'.join(part for part in (summary, added) if part)
        tokens = record.get('usage') or {}
        parts = [*tokens.get('lead', []), *tokens.get('scouts', []), *tokens.get('reviews', [])]
        reviews = record.get('reviews') or []
        spent = run_spent(parts, record.get('seconds') or 0)
        updates = {
            # None when the run never reached its checks, so the screen shows no green pills for a failed start.
            'engine_check': {
                'passed': bool(check.get('passed')),
                'issues': shown_issues(check.get('issues') or []),
                'summary': check.get('summary') or {},
                'reviews': len(reviews),
                'doubts': sum(len(r.get('doubts') or []) for r in reviews),
            }
            if check
            else None,
            'error': error if status in ('failed', 'paused') else None,
            # Why a partly done run stopped, in a sentence, for its report on the run screen.
            'stopped_because': error if status == 'partial' else None,
            'error_detail': raw,
            'cut_short': record.get('cut_short'),  # turns, tokens, day, time or service: the agent did not finish
            'artifacts': self.artifacts(job_id, status == 'completed'),
            'summary': summary,
            # None for a run that ended without one; older runs have only the summary. Lines to keep for a client are
            # offered once per run: a run continued after they were saved or put away does not offer them again.
            'report': without_offer(final_report, store.get(job_id).get('offer')),
            'unresolved': unresolved,
            'request_count': spent['requests'],
            'usage': {k: spent[k] for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')},  # this run's
        }
        final = {}
        store.add_spend(spent['input_tokens'] + spent['output_tokens'])

        def apply(job):
            # What each run of this job took, so the job's whole time and usage can be shown, not only the last run's.
            job['runs'] = [*(job.get('runs') or []), spent]
            # A cancel the run did not see in time still wins; nothing the run reports overrides it.
            final['status'] = 'cancelled' if job['status'] == 'cancelled' else status
            job.update(updates, status=final['status'])
            if record['status'] != 'failed':  # where a run started from is said once, by a run that got going
                job.pop('resume_from', None)
                job.pop('start_over', None)
            if final['status'] != 'waiting':
                job['question'] = None
            # Steps given too late for this run to take go no further; a paused or waiting run takes them on restart.
            if final['status'] not in ('paused', 'waiting'):
                job.pop('more_turns', None)
            # A message that arrived after the agent's last turn was never read. The runner starts the job again for it
            # as soon as this run lets go (runtime.resume_if_answered), so the run ends as it is; only its lessons wait.
            if final['status'] in FINISHED and job.get('pending_messages'):
                final['unread'] = True

        # The agent finished its work (rather than being paused, cut off or left waiting), so the rounds of comments
        # it read are finished too: keep their final report. Done before the status shows, so the two arrive together.
        if record['status'] in ('complete', 'partial') and not waiting:
            try:
                feedback.close_rounds(store, job_id)
            except Exception as error:  # noqa: BLE001 - a report that cannot be made never changes how the run ended
                message = f'The report on your comments could not be made ({type(error).__name__}).'
                store.event(job_id, 'feedback.report_failed', message, type='job', level='WARN', error=str(error))
        store.mutate(job_id, apply)
        # What the agent learned is decided on how the job really ended, which only apply knows: a cancel that came
        # late wins, and an instruction the agent never read keeps the question open for the run that reads it.
        if final.get('unread') or final['status'] in ('paused', 'waiting'):
            outcome = 'later'
        elif final['status'] == 'completed':
            outcome = 'passed'
        else:
            outcome = 'failed'
        try:
            lessons.settle(store, job_id, outcome)
        except Exception as error:  # noqa: BLE001 - a lesson that cannot be kept never changes how the run ended
            message = f'What the agent learned could not be kept ({type(error).__name__}).'
            store.event(job_id, 'lesson.failed', message, type='job', level='WARN', error=str(error))
        level = 'ERROR' if final['status'] == 'failed' else 'INFO'
        message = {'failed': error or 'The run failed.', 'waiting': 'Waiting for your answer.'}.get(
            final['status'], final.get('summary', summary)
        )
        detail = {'raw': raw} if raw and final['status'] in ('failed', 'paused', 'partial') else {}
        store.event(job_id, 'job.' + final['status'], message or final['status'], type='job', level=level, **detail)
