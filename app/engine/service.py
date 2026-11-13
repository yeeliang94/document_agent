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

from ..jobstatus import stop_status
from ..modules import MODULE_LABELS
from ..store import clean
from .agent import Control, run_job
from .endpoint import Settings as Endpoint
from .jobspec import Answerer, Deliverable, JobSpec

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
    'instructions.delivered': lambda d: 'New instructions delivered to the agent',
    'sandbox.keepalive_failed': lambda d: 'The sandbox did not answer a keep-alive check',
    'run.finished': lambda d: f'Run finished: {d.get("status")}',
    # Shown only when the script opened with a comment saying what it is for.
    'code.ran': lambda d: d.get('summary'),
}
WARNINGS = {'tool.failed', 'scout.failed', 'sandbox.keepalive_failed'}

HINTS = [
    (
        ('container', 'not found'),
        'The sandbox was not found. The model alias and the endpoint may point to different regions; use a model '
        'alias from the same region as OPENAI_BASE_URL (for example an "eu" alias with an EMEA address).',
    ),
    (
        ('403',),
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

    def new_messages(self) -> list[str]:
        if not self._current().get('pending_messages'):
            return []
        taken: list[str] = []

        def take(job):
            taken.extend(job.get('pending_messages') or [])
            job['pending_messages'] = []
            # Kept for good: a resumed run in a fresh sandbox must still follow them.
            job['engine_instructions'] = [*(job.get('engine_instructions') or []), *taken]

        self.store.mutate(self.job_id, take)
        self._job = {**self._job, 'pending_messages': []}
        return taken


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


def job_spec(store, job: dict) -> JobSpec:
    """A template (.xlsx) makes a fill-the-template deliverable; a format (.json, see engine/rules.py) makes a
    workbook built to that format."""
    root = store.path(job['id'])
    definition = root / next(e['path'] for e in job['files'] if e['role'] == 'template')
    if definition.suffix.lower() == '.json':
        spec = json.loads(definition.read_text(encoding='utf-8-sig'))
        deliverable = Deliverable(f'{definition.stem}.xlsx', format=spec, format_path=definition)
    else:
        deliverable = Deliverable(f'{definition.stem}_output.xlsx', template=definition)
    module = MODULE_LABELS.get(job['module'], job['module'])
    goal = (job.get('instructions') or '').strip() or f'Prepare the {module} deliverable from the supplied files.'
    return JobSpec(
        id=job['id'],
        module=job['module'],
        goal=f'{goal}\nReporting month: {job["period"]}.',
        zip=packet_zip(root, job['files'], root / 'working' / 'engine' / 'packet.zip'),
        deliverables=(deliverable,),
        context=job.get('context') or '',
    )


def resume_card(store, job: dict) -> str | None:
    """What a fresh sandbox needs to continue an earlier run instead of starting over."""
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
        parts.append(
            "Questions you asked and the user's answers:\n"
            + '\n'.join(f'- Q: {q["question"]}\n  A: {q["answer"]}' for q in asked)
        )
    return '\n\n'.join(parts)


def shown_issues(issues: list[str], limit: int = 10) -> list[str]:
    more = len(issues) - limit
    return issues[:limit] + ([f'... and {more} more; see the run record.'] if more > 0 else [])


def take_answer(job: dict) -> None:
    """Move an answered question into the job's question history (store.mutate callback)."""
    if job.get('question') and job.get('answer'):
        asked = job.get('engine_questions') or []
        job['engine_questions'] = [*asked, {'question': job['question']['text'], 'answer': job['answer']}]
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
        store.mutate(job_id, lambda j: (take_answer(j), j.update(status='running', error=None)))
        options = store.settings()
        team = {'max_scouts': options['max_scouts'], 'reviewer': options['reviewer']}
        job = store.update(job_id, engine_team=team)
        root = store.path(job_id)
        attempt = len(list((root / 'engine').glob('run-*'))) + 1
        resume = resume_card(store, job)
        spec = await asyncio.to_thread(job_spec, store, job)
        control = AppControl(store, job_id)

        def on_event(name: str, data: dict):
            message = MESSAGES.get(name)
            text = message(data) if message is not None else None
            if text:
                level = 'WARN' if name in WARNINGS else 'INFO'
                extra = {'files': data['files']} if isinstance(data.get('files'), list) else {}
                store.event(job_id, 'engine.' + name, text, type='engine', level=level, run=attempt, **extra)

        one_question = asyncio.Lock()  # the agent may call tools in parallel; the user sees one question at a time

        async def ask(question: str) -> str:
            async with one_question:
                if control.waiting:  # an earlier question is already parked for the user
                    return (
                        'Another question is waiting for the user. End your turn now; the job resumes with the answer.'
                    )
                return await ask_one(question)

        async def ask_one(question: str) -> str:
            stopped = []

            def post(j):
                if j['status'] in ('pausing', 'cancelled'):
                    stopped.append(j['status'])
                else:
                    j.update(status='waiting', question={'text': question, 'call_id': 'sandbox'}, answer=None)

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

        record = (
            await run_job(
                spec,
                self.endpoint(options),
                root / 'engine',
                run_id=f'run-{attempt:02d}',
                outputs=root / 'outputs',
                on_event=on_event,
                control=control,
                resume=resume,
                answerer=Answerer(ask=ask, log=list(job.get('engine_questions') or [])),
                max_scouts=options['max_scouts'],
                review_on=options['reviewer'],
                proof=options['proof'],
                effort=options['effort'],
                memory=options['sandbox_memory'],
                max_turns=options['max_turns'],
                max_tokens=options['token_limit_thousands'] * 1000,
                timeout_seconds=options['time_limit_minutes'] * 60,
                echo=lambda _: None,
            )
        ).record
        self.settle(job_id, record, control.waiting)

    def artifacts(self, job_id: str, validated: bool) -> list[dict]:
        outputs = self.store.path(job_id) / 'outputs'
        items = []
        for path in sorted(outputs.glob('*')):
            if path.is_file() and not path.name.endswith('.pending'):
                item = {'path': 'outputs/' + path.name, 'name': path.name, 'size': path.stat().st_size}
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

        def apply(job):
            # A cancel the run did not see in time still wins; nothing the run reports overrides it.
            final['status'] = 'cancelled' if job['status'] == 'cancelled' else status
            job.update(updates, status=final['status'])
            if final['status'] != 'waiting':
                job['question'] = None
            # An instruction that arrived after the agent's last turn was never read. Keep the run open for it.
            if final['status'] == 'completed' and job.get('pending_messages'):
                final['status'] = job['status'] = 'partial'
                job['summary'] = final['summary'] = (
                    summary
                    + '\nA new instruction arrived after the last step and was not applied. Continue to apply it.'
                ).strip()

        store.mutate(job_id, apply)
        level = 'ERROR' if final['status'] == 'failed' else 'INFO'
        message = {'failed': error or 'The run failed.', 'waiting': 'Waiting for your answer.'}.get(
            final['status'], final.get('summary', summary)
        )
        store.event(job_id, 'job.' + final['status'], message or final['status'], type='job', level=level)
