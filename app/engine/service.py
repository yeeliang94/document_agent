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

from ..modules import MODULE_LABELS
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
    'review.finished': lambda d: f'Review finished: {d.get("verdict")}, {d.get("doubts")} doubt(s)',
    'instructions.delivered': lambda d: 'New instructions delivered to the agent',
    'sandbox.keepalive_failed': lambda d: 'The sandbox did not answer a keep-alive check',
    'run.finished': lambda d: f'Run finished: {d.get("status")}',
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
    """Reads the job record, at most once a second, to learn about pauses, cancels and new instructions."""

    def __init__(self, store, job_id: str):
        self.store, self.job_id = store, job_id
        self.waiting = False  # set when a question timed out: stop and wait for the answer
        self._checked = 0.0
        self._stop: str | None = None

    def stop_requested(self) -> str | None:
        if self.waiting:
            return 'waiting'
        now = time.monotonic()
        if now - self._checked >= POLL_SECONDS:
            self._checked = now
            status = self.store.get(self.job_id)['status']
            self._stop = {'pausing': 'paused', 'cancelled': 'cancelled'}.get(status)
        return self._stop

    def new_messages(self) -> list[str]:
        if not self.store.get(self.job_id).get('pending_messages'):
            return []
        taken: list[str] = []

        def take(job):
            taken.extend(job.get('pending_messages') or [])
            job['pending_messages'] = []

        self.store.mutate(self.job_id, take)
        return taken


def packet_zip(root: Path, files: list[dict], target: Path) -> Path:
    """The job's source files as one zip, paths relative to inputs/, as the engine expects."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for entry in files:
            if entry['role'] == 'inputs':
                archive.write(root / entry['path'], entry['path'].removeprefix('inputs/'))
    return target


def job_spec(store, job: dict) -> JobSpec:
    root = store.path(job['id'])
    template = root / next(e['path'] for e in job['files'] if e['role'] == 'template')
    module = MODULE_LABELS.get(job['module'], job['module'])
    goal = (job.get('instructions') or '').strip() or f'Prepare the {module} deliverable from the supplied files.'
    return JobSpec(
        id=job['id'],
        module=job['module'],
        goal=f'{goal}\nReporting month: {job["period"]}.',
        zip=packet_zip(root, job['files'], root / 'working' / 'engine' / 'packet.zip'),
        deliverables=(Deliverable(f'{template.stem}_output.xlsx', template=template),),
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
    asked = job.get('engine_questions') or []
    if job.get('question') and job.get('answer'):
        asked = [*asked, {'question': job['question']['text'], 'answer': job['answer']}]
    if asked:
        parts.append(
            "Questions you asked and the user's answers:\n"
            + '\n'.join(f'- Q: {q["question"]}\n  A: {q["answer"]}' for q in asked)
        )
    return '\n\n'.join(parts)


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
        job = store.update(job_id, status='running', error=None)
        options = store.settings()
        root = store.path(job_id)
        attempt = len(list((root / 'engine').glob('run-*'))) + 1
        resume = resume_card(store, job)
        spec = job_spec(store, job)
        questions = list(job.get('engine_questions') or [])
        if job.get('question') and job.get('answer'):
            questions.append({'question': job['question']['text'], 'answer': job['answer']})
        store.update(job_id, question=None, answer=None, engine_questions=questions)
        control = AppControl(store, job_id)

        def on_event(name: str, data: dict):
            message = MESSAGES.get(name)
            if message is not None:
                level = 'WARN' if name in WARNINGS else 'INFO'
                store.event(job_id, 'engine.' + name, message(data), type='engine', level=level, run=attempt)

        async def ask(question: str) -> str:
            store.update(job_id, status='waiting', question={'text': question, 'call_id': 'sandbox'}, answer=None)
            deadline = time.monotonic() + options['question_wait_minutes'] * SECONDS_PER_MINUTE
            while time.monotonic() < deadline:
                current = store.get(job_id)
                if current['status'] == 'cancelled':
                    control.waiting = False
                    return 'The user cancelled the job. Stop now.'
                if current.get('answer'):
                    answer = current['answer']

                    def answered(j, answer=answer):
                        asked = [*(j.get('engine_questions') or []), {'question': question, 'answer': answer}]
                        j.update(status='running', question=None, answer=None, engine_questions=asked)

                    store.mutate(job_id, answered)
                    return answer
                await asyncio.sleep(POLL_SECONDS)
            control.waiting = True
            store.update(job_id, status='waiting', question={'text': question, 'call_id': 'sandbox'})
            return (
                'The user has not answered yet. Stop now and end your turn with status partial; the job resumes '
                'with their answer in a fresh sandbox.'
            )

        run = await run_job(
            spec,
            self.endpoint(options),
            root / 'engine',
            run_id=f'run-{attempt:02d}',
            outputs=root / 'outputs',
            on_event=on_event,
            control=control,
            resume=resume,
            answerer=Answerer(ask=ask),
            max_scouts=options['max_scouts'],
            review_on=options['reviewer'],
            proof=options['proof'],
            effort=options['effort'],
            memory=options['sandbox_memory'],
            max_turns=options['max_turns'],
            timeout_seconds=max(60, self.settings.job_timeout - 30),
            echo=lambda _: None,
        )
        self.settle(job_id, run.record, control.waiting)

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
        engine_status = 'waiting' if waiting else record['status']
        check = record.get('final_check') or {}
        summary = (record.get('summary') or {}).get('summary', '')
        unresolved = (record.get('summary') or {}).get('open_questions', [])
        status = {
            'complete': 'completed',
            'partial': 'partial',
            'stopped': 'paused',  # a turn or time limit: saved work is kept and the user can resume
            'paused': 'paused',
            'cancelled': 'cancelled',
            'waiting': 'waiting',
        }.get(engine_status, 'failed')
        error = with_hint(record.get('error'))
        if status == 'completed' and not check.get('passed'):
            status = 'partial'
        if status == 'partial' and check.get('issues'):
            summary += '\nThe last check still lists problems; see the run record.'
        if status in ('completed', 'partial'):
            report = summary + ('\n' + '\n'.join(unresolved) if unresolved else '')
            (store.path(job_id) / 'outputs' / 'processing_summary.txt').write_text(report, encoding='utf-8')
        tokens = record.get('usage') or {}
        parts = [*tokens.get('lead', []), *tokens.get('scouts', []), *tokens.get('reviews', [])]
        updates = {
            'status': status,
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
        if status != 'waiting':
            updates['question'] = None
        store.update(job_id, **updates)
        level = 'ERROR' if status == 'failed' else 'INFO'
        message = {'failed': error or 'The run failed.', 'waiting': 'Waiting for your answer.'}.get(status, summary)
        store.event(job_id, 'job.' + status, message or status, type='job', level=level)
