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
import json
import time
import zipfile
from pathlib import Path

from .. import feedback, lessons
from ..jobstatus import stop_status
from ..modules import MODULE_LABELS
from ..store import clean
from .agent import Control, run_job
from .endpoint import Settings as Endpoint
from .jobspec import DEFAULT_ANSWER, Answerer, Deliverable, JobSpec
from .pastruns import PAST_LIMIT, past_files

POLL_SECONDS = 1.0
SECONDS_PER_MINUTE = 60  # tests shorten the question wait through this
NOTES_LIMIT = 6000

# What the activity feed says for each engine event; events not listed are kept in the run record only.
MESSAGES = {
    'run.started': lambda d: f'Started on {d.get("files")} files',
    'sandbox.ready': lambda d: 'Sandbox ready; inputs uploaded',
    'code.started': lambda d: 'Running code in the sandbox',
    'tool.called': lambda d: f'Using {d.get("tool")}',
    'tool.failed': lambda d: f'A tool failed: {d.get("error")}',
    'scout.started': lambda d: f'Scouting {d.get("folder")} ({d.get("files")} files)',
    'scout.finished': lambda d: f'Scout finished {d.get("folder")}',
    'scout.failed': lambda d: f'Scout stopped on {d.get("folder")}',
    'outputs.saved': lambda d: (
        'Saved outputs; checks passed'
        if d.get('passed')
        else f'Saved outputs; {len(d.get("issues") or [])} problem(s) to fix'
    ),
    'question.asked': lambda d: f'Question for you: {d.get("question")}',
    'question.answered': lambda d: 'Answer received',
    'review.started': lambda d: 'An independent reviewer is checking the result',
    'review.skipped': lambda d: f'No review: {d.get("reason")}',
    'review.finished': lambda d: f'Review finished: {d.get("verdict")}, {d.get("doubts")} doubt(s)',
    'skill.read': lambda d: f'Reading skill {d.get("skill")}' if d.get('first') else None,  # once per skill
    'skill.run': lambda d: f'Running {d.get("program")} from skill {d.get("skill")}',
    'rules.held': lambda d: 'Checks passed; holding the result against your house rules',
    'past_run.opened': lambda d: f'Looking at an earlier run: {d.get("title")}',
    'instructions.delivered': lambda d: 'New instructions delivered to the agent',
    'sandbox.keepalive_failed': lambda d: 'The sandbox did not answer a keep-alive check',
    'run.finished': lambda d: f'Run finished: {d.get("status")}',
    # A script, as it streams in: what it is for once its opening comment is written, then the files it names.
    'code.writing': lambda d: d.get('summary'),
    'code.running': lambda d: d.get('summary') or ('Reading' if d.get('files') else None),
    'code.finished': lambda d: d.get('summary') or 'Ran code in the sandbox',
    # At the end of a model reply. Shown only when the stream did not already show it, and the script opened with a
    # comment saying what it is for.
    'code.ran': lambda d: None if d.get('shown') else d.get('summary'),
    'model.retry': lambda d: 'OpenAI dropped a reply; carrying on',
    'model.item_refused': lambda d: (
        'OpenAI could not find an earlier sandbox step; it is sent as a note and the run carries on'
    ),
    'model.unavailable': lambda d: 'The AI service stopped answering; saved work is kept',
}
WARNINGS = {'tool.failed', 'scout.failed', 'sandbox.keepalive_failed', 'model.unavailable'}
DAY_USED_UP = (
    "Today's token limit is used up. Saved work is kept. Continue tomorrow, or raise the limit on the Settings page."
)

HINTS = [
    (
        ('container', 'not found'),
        'The sandbox was not found. The model alias and the endpoint may point to different regions; use a model '
        'alias from the same region as OPENAI_BASE_URL (for example an "eu" alias with an EMEA address).',
    ),
    (
        ('error code: 403',),  # the status, not any 403 inside an item id
        'The endpoint refused a sandbox call. Ask the proxy team to allow the /v1/containers routes, or set the '
        'LiteLLM options on the Settings page.',
    ),
]


def with_hint(error: str | None) -> str | None:
    if not error:
        return error
    lower = error.lower()
    for words, hint in HINTS:
        if all(w in lower for w in words):
            return f'{error} {hint}'
    return error


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
        """What the user sent since the last look: instructions typed in the conversation, and rounds of comments
        pinned to the files (app/feedback.py), which come with their pictures."""
        if not self._current().get('pending_messages'):
            return []
        taken: list = []

        def take(job):
            taken.extend(job.get('pending_messages') or [])
            job['pending_messages'] = []
            # Kept for good: a resumed run in a fresh sandbox must still follow them.
            texts = [m if isinstance(m, str) else m['text'] for m in taken]
            job['engine_instructions'] = [*(job.get('engine_instructions') or []), *texts]
            rounds = {m['round'] for m in taken if not isinstance(m, str)}
            for entry in job.get('feedback') or []:
                if entry['number'] in rounds:
                    entry['status'] = 'delivered'

        self.store.mutate(self.job_id, take)
        self._job = {**self._job, 'pending_messages': []}
        root, messages = self.store.path(self.job_id), []
        for item in taken:
            if isinstance(item, str):
                messages.append(item)
                continue
            self.store.event(
                self.job_id, 'feedback.delivered', 'Your comments reached the agent', type='engine', round=item['round']
            )
            images = [{'name': i['name'], 'data': (root / i['path']).read_bytes()} for i in item['images']]
            messages.append({'text': item['text'], 'images': images})
        return messages

    @property
    def can_remember(self) -> bool:
        return bool((self._current().get('playbook') or {}).get('id'))

    def remember(self, comment: str, lesson: str) -> str:
        return lessons.note(self.store, self.job_id, comment, lesson)

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


def packet_zip(root: Path, files: list[dict], target: Path) -> Path:
    """The job's source files as one zip, paths relative to inputs/. Inputs never change after upload, so the zip
    is built once per job. Compressed, because OpenAI sandboxes take files of at most 50 MB."""
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix('.partial')
    with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED) as archive:
        for entry in files:
            if entry['role'] == 'inputs':
                archive.write(root / entry['path'], entry['path'].removeprefix('inputs/'))
    partial.replace(target)
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
    found = []
    for other in store.jobs():  # newest first
        same = other['id'] != job['id'] and other['module'] == job['module'] and other['status'] == 'completed'
        same = same and not other.get('replay')  # a replayed test is not work the user accepted
        if same and other['created_at'] <= job['created_at'] and past_files(store.path(other['id']) / 'outputs'):
            found.append({'label': f'earlier-{len(found) + 1}', 'id': other['id'], 'title': job_title(other),
                          'finished': other['updated_at'][:10]})  # fmt: skip
        if len(found) == PAST_LIMIT:
            break
    store.mutate(job['id'], lambda j: j.setdefault('past_runs', found))
    return found


def past_runs(store, job: dict) -> tuple[dict, ...]:
    """The job's earlier runs with what the engine needs to open one. Left out: a run deleted since, and a run
    that is no longer finished because the user sent it back with comments. Its files are then work in progress;
    it is offered again, under the same label, once it finishes. (One job runs at a time, so a run cannot change
    while another job is reading it.)"""
    held = []
    for run in choose_past_runs(store, job):
        try:
            other = store.get(run['id'])
        except KeyError:
            continue
        if other['status'] != 'completed':
            continue
        outputs = store.path(run['id']) / 'outputs'
        held.append({**run, 'goal': (other.get('instructions') or '')[:1000], 'outputs': outputs})
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
        zip=packet_zip(root, job['files'], root / 'working' / 'engine' / 'packet.zip'),
        deliverables=deliverables,
        context=job.get('context') or '',
    )


def resume_card(store, job: dict, new: list[str] = ()) -> str | None:
    """What a fresh sandbox needs to continue an earlier run instead of starting over. new: what the user sent
    since that run ended, which this run is starting in order to act on."""
    root = store.path(job['id'])
    runs = sorted((root / 'engine').glob('run-*/run.json'))
    if not runs:
        return None
    last = json.loads(runs[-1].read_text(encoding='utf-8'))
    parts = [
        'You are resuming this job in a fresh sandbox; the earlier sandbox is gone. Continue from where the work '
        f'stands rather than starting over. The previous run ended as "{last.get("status")}".'
    ]
    check = last.get('final_check') or {}
    if check.get('issues'):
        parts.append('The last check listed:\n' + '\n'.join(f'- {i}' for i in check['issues'][:20]))
    notes = root / 'outputs' / 'notes.md'
    if notes.exists():
        parts.append('Your notes so far:\n' + notes.read_text(encoding='utf-8', errors='replace')[:NOTES_LIMIT])
    told = job.get('engine_instructions') or []
    if told:
        parts.append(
            'Instructions the user gave during earlier runs. They still apply, and override the goal where they '
            'differ:\n' + '\n'.join(f'- {m}' for m in told)
        )
    asked = job.get('engine_questions') or []
    if asked:
        parts.append("Questions you asked and the user's answers:\n" + '\n'.join(_asked(q) for q in asked))
    if new:
        parts.append(
            'New from the user since that run ended. This is what to act on now: decide what it changes, redo only '
            'the affected work, and save again:\n' + '\n'.join(new)
        )
    return '\n\n'.join(parts)


def shown_issues(issues: list[str], limit: int = 10) -> list[str]:
    more = len(issues) - limit
    return issues[:limit] + ([f'... and {more} more; see the run record.'] if more > 0 else [])


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
        team = {'max_scouts': options['max_scouts'], 'reviewer': options['reviewer']}
        job = store.update(job_id, engine_team=team)
        root = store.path(job_id)
        attempt = len(list((root / 'engine').glob('run-*'))) + 1
        control = AppControl(store, job_id)
        # A run that continues an earlier one opens with whatever the user sent in between, so it starts on that at
        # once. (job was read before these are taken, so the card lists them as new, not as earlier instructions.)
        fresh = control.new_messages() if attempt > 1 else []
        resume = resume_card(store, job, [m if isinstance(m, str) else m['text'] for m in fresh])
        # The card repeats the text of every round of comments still being acted on, those just taken included;
        # the pictures that text names go with it.
        pictures = tuple(await asyncio.to_thread(feedback.open_pictures, store, job_id)) if resume else ()
        spec = await asyncio.to_thread(job_spec, store, job)

        def on_event(name: str, data: dict):
            message = MESSAGES.get(name)
            text = message(data) if message is not None else None
            if text:
                level = 'WARN' if name in WARNINGS else 'INFO'
                extra = {'files': data['files']} if isinstance(data.get('files'), list) else {}
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
                'The user has not answered yet. End your turn now with status partial; the job resumes with their '
                'answer in a fresh sandbox.'
            )

        run_id = f'run-{attempt:02d}'
        try:
            record = await self._run(spec, options, root, run_id, on_event, control, resume, pictures, ask, job,
                                     left if day_bound else allowance)  # fmt: skip
        except BaseException:
            # Cut off before it could settle (the app stopping, the backstop time limit): what it used still counts
            # towards the day. The engine writes its record as it ends, so read the usage from there.
            await asyncio.shield(asyncio.to_thread(self.count_unsettled, root / 'engine' / run_id))
            raise
        if day_bound and record.get('cut_short') == 'tokens':
            record.update(error=DAY_USED_UP, cut_short='day')
        await asyncio.to_thread(self.settle, job_id, record, control.waiting)

    def count_unsettled(self, run_folder) -> None:
        try:
            usage = json.loads((run_folder / 'run.json').read_text(encoding='utf-8')).get('usage') or {}
        except (OSError, ValueError):
            return
        parts = [*usage.get('lead', []), *usage.get('scouts', []), *usage.get('reviews', [])]
        self.store.add_spend(sum(p.get('input_tokens', 0) + p.get('output_tokens', 0) for p in parts))

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
                timeout_seconds=options['time_limit_minutes'] * 60,
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
        summary = (record.get('summary') or {}).get('summary', '')
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
        error = clean(with_hint(record.get('error')) or '') or None
        if status == 'partial' and error:
            summary = (summary + '\n' if summary else '') + error
        if status == 'partial' and check.get('issues'):
            summary += '\nThe last check still lists problems; see the run record.'
        if status in ('completed', 'partial'):
            report = summary + ('\n' + '\n'.join(unresolved) if unresolved else '')
            (store.path(job_id) / 'outputs' / 'processing_summary.txt').write_text(report, encoding='utf-8')
        tokens = record.get('usage') or {}
        parts = [*tokens.get('lead', []), *tokens.get('scouts', []), *tokens.get('reviews', [])]
        reviews = record.get('reviews') or []
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
            'cut_short': record.get('cut_short'),  # turns, tokens, day, time or service: the agent did not finish
            'artifacts': self.artifacts(job_id, status == 'completed'),
            'summary': summary,
            'unresolved': unresolved,
            'request_count': sum(p.get('requests', 0) for p in parts),
            'usage': {
                'input_tokens': sum(p.get('input_tokens', 0) for p in parts),
                'output_tokens': sum(p.get('output_tokens', 0) for p in parts),
            },
        }
        final = {}
        store.add_spend(updates['usage']['input_tokens'] + updates['usage']['output_tokens'])

        spent = {
            'seconds': record.get('seconds') or 0,
            'requests': updates['request_count'],
            'input_tokens': updates['usage']['input_tokens'],
            'output_tokens': updates['usage']['output_tokens'],
        }

        def apply(job):
            # What each run of this job took, so the job's whole time and usage can be shown, not only the last run's.
            job['runs'] = [*(job.get('runs') or []), spent]
            # A cancel the run did not see in time still wins; nothing the run reports overrides it.
            final['status'] = 'cancelled' if job['status'] == 'cancelled' else status
            job.update(updates, status=final['status'])
            if final['status'] != 'waiting':
                job['question'] = None
            # An instruction that arrived after the agent's last turn was never read. Keep the run open for it.
            if final['status'] == 'completed' and job.get('pending_messages'):
                final['status'] = job['status'] = 'partial'
                final['unread'] = True
                job['summary'] = final['summary'] = (
                    summary
                    + '\nA new instruction arrived after the last step and was not applied. Continue to apply it.'
                ).strip()

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
        if final['status'] == 'completed':
            outcome = 'passed'
        elif final.get('unread') or final['status'] in ('paused', 'waiting'):
            outcome = 'later'
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
        store.event(job_id, 'job.' + final['status'], message or final['status'], type='job', level=level)
