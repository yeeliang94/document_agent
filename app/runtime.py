"""The job queue. One worker runs jobs one at a time on the sandbox engine (app/engine).

It also looks after a playbook's kept tests when its instructions change: the list is reviewed (app/curator.py)
and, depending on the Settings page, replayed (app/replay.py). Replays are ordinary jobs in the same queue."""

import asyncio

from . import cases, curator, feedback, jobstatus, replay, tidy
from .config import Settings
from .engine.agent import MAX_QUESTIONS
from .engine.service import SandboxEngine
from .store import Store, clean


class JobTimeout(Exception):
    """The run went past the time limit on the Settings page."""


LEGACY_NOTE = 'This run was made by an earlier version of the app. It can be read but not continued.'


class Runner:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.queue = asyncio.Queue()
        self.worker = None
        self.active = None
        self.engine = SandboxEngine(store, settings)
        self.loop = None
        self.tasks = set()  # work started by a playbook change; kept so it is not dropped half way
        self.pending = {}  # playbook -> why: a change saved while its check was running, checked when that one ends

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.store.playbooks.on_change = self.playbook_changed
        for playbook in self.store.playbooks.list():  # retired tests are kept for a while, not for ever
            cases.sweep(self.store, playbook['id'])
        for job in self.store.jobs():
            if job.get('replay'):  # hidden jobs of a check: settled below, never continued
                continue
            feedback.recover(self.store, job)
            if not job.get('legacy') and (self.store.path(job['id']) / 'working' / 'history.json').exists():
                self.retire(job)
            elif job['status'] in jobstatus.ACTIVE:  # No worker survives a restart.
                self.store.update(job['id'], status='paused', error=None)
                self.store.event(
                    job['id'], 'job.recovered', 'Interrupted job retained. Resume when ready.', type='job', level='WARN'
                )
        # A check the last shutdown cut off gets a result for every case, so it does not block the next one.
        for check in replay.reconcile(self.store):
            if check.get('tidy'):
                tidy.conclude(self.store, check)
        self.worker = asyncio.create_task(self.work())

    def retire(self, job):
        """A run the earlier engine worked on keeps its conversation and files for reading. Its saved state means
        nothing to this engine, so it is closed for good rather than restarted without the user's earlier answers."""
        unfinished = job['status'] not in jobstatus.FINISHED | {'failed', 'cancelled'}
        self.store.update(job['id'], legacy=True, question=None, **({'status': 'cancelled'} if unfinished else {}))
        if unfinished:
            self.store.event(job['id'], 'job.cancelled', LEGACY_NOTE, type='job')

    async def close(self):
        self.store.playbooks.on_change = None
        for task in list(self.tasks):
            task.cancel()
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass

    def playbook_changed(self, playbook_id: str, version: int, source: str) -> None:
        """A version was saved. Called from whichever thread saved it; the follow-up runs on the app's loop."""

        def begin():
            task = asyncio.ensure_future(self.after_change(playbook_id, source))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(begin)

    async def after_change(self, playbook_id: str, source: str) -> None:
        """Review the playbook's tests against the new instructions, then replay them when the Settings page says
        so: "every" change, or only "large" ones (a rewrite the user accepted; a tidy is replayed before it is
        saved, see app/tidy.py). A lesson just learned changes no existing rule, so the list is not reviewed for it."""
        try:
            if not self.settings.api_key or not cases.cases(self.store, playbook_id):
                return
            if self.store.day_used_up():  # no review, tidy or replay on a day whose limit is used up
                self.store.playbooks.event(playbook_id, 'check.not_started', "Today's token limit is used up, so the tests were not reviewed or replayed", 'WARN')  # fmt: skip
                return
            if source != 'learned':
                await curator.curate(self.store, self.engine.endpoint(self.store.settings()), playbook_id)
            mode = self.store.settings()['replay_tests']
            if source in tidy.LESSONS and await asyncio.to_thread(tidy.due, self.store, playbook_id):
                # Lessons have piled up: propose a tidy and replay it as a candidate. It is saved only if it passes.
                wanted = await tidy.begin(self.store, self.engine.endpoint(self.store.settings()), playbook_id)
                if wanted:
                    await self.start_check(playbook_id, 'tidy', text=wanted['text'], based_on=wanted['based_on'],
                                           extra={'tidy': wanted['proposal']})  # fmt: skip
            elif source != 'tidied' and (mode == 'every' or (mode == 'large' and source == 'tuned')):
                # Noted before asking, so a check that closes at this very moment still finds it; a check that
                # starts here is for this change, and the note goes.
                self.pending[playbook_id] = source
                if await self.start_check(playbook_id, source) or not replay.running(self.store, playbook_id):
                    self.pending.pop(playbook_id, None)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a failed follow-up never touches the saved version
            self.store.playbooks.event(playbook_id, 'check.not_started', 'The tests could not be reviewed or replayed', 'WARN',
                                       reason=clean(f'{type(error).__name__}: {error}')[:300])  # fmt: skip

    async def start_check(self, playbook_id: str, reason: str, **candidate) -> dict | None:
        """Replay a playbook's tests; returns the check, or None when there are none or one is already running."""
        check = await asyncio.to_thread(replay.start, self.store, playbook_id, reason, **candidate)
        for entry in (check or {}).get('cases', []):
            if entry['status'] == 'queued':
                await self.submit(entry['job_id'])
        return check

    async def replay_ended(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job.get('replay'):
            check = await asyncio.to_thread(replay.finished, self.store, job)
            if check and check.get('tidy'):  # the last test of a tidy's check: keep the tidy or drop it
                await asyncio.to_thread(tidy.conclude, self.store, check)
            if check:
                await self.check_pending(check)

    async def check_pending(self, check: dict) -> None:
        """A check has closed. If the instructions were changed while it ran, check the latest version now, unless
        the check that just closed already speaks for it or today's token limit is used up."""
        playbook_id = check['playbook_id']
        reason = self.pending.pop(playbook_id, None)
        if not reason:
            return
        checked = replay.read(self.store, playbook_id, check['id'])['version']  # a saved tidy takes its version
        if checked == self.store.playbooks.get(playbook_id)['version']:
            return
        if self.store.day_used_up():
            self.store.playbooks.event(playbook_id, 'check.not_started', "Today's token limit is used up, so the latest instructions were not checked", 'WARN')  # fmt: skip
            return
        await self.start_check(playbook_id, reason)

    async def submit(self, job_id):
        self.store.update(job_id, status='queued', error=None)
        await self.enqueue(job_id)

    async def enqueue(self, job_id):
        """Queue a job whose status the caller has already set to queued, in the same write as what it queued for."""
        self.store.event(job_id, 'job.queued', 'Queued', type='job')
        await self.queue.put(job_id)

    async def work(self):
        while True:
            job_id = await self.queue.get()
            self.active = job_id
            try:
                # A job paused or cancelled while it waited in the queue is skipped.
                if self.store.get(job_id)['status'] == 'queued':
                    await self.run_with_limit(job_id)
            except asyncio.CancelledError:
                if self.store.get(job_id)['status'] in jobstatus.ACTIVE:
                    self.store.update(job_id, status='paused')
                    self.store.event(job_id, 'job.paused', 'Application stopped; checkpoint retained.', type='job')
                raise
            except JobTimeout as error:
                self.fail(job_id, str(error), 'TimeoutError')
            except Exception as error:
                # Do not include HTTP headers or credential-bearing exceptions in events.
                self.fail(job_id, clean(str(error) or type(error).__name__), type(error).__name__)
            finally:
                self.active = None
                self.queue.task_done()
                await self.resume_if_answered(job_id)
                try:
                    await self.replay_ended(job_id)
                except Exception as error:  # noqa: BLE001 - scoring a replay never stops the queue
                    self.store.event(
                        job_id, 'replay.not_scored', clean(f'{type(error).__name__}: {error}'), level='WARN', type='job'
                    )

    async def resume_if_answered(self, job_id):
        """Queue a job whose question was answered after its run stopped holding the sandbox, or whose comments or
        messages arrived while its run was finishing and so were never read. Safe to call from both the answer
        endpoint and the worker: only the first call that finds the answer, comments or message queues the job."""
        queued = []

        def claim(job):
            unread = any(r['status'] == 'sent' for r in job.get('feedback') or [])
            if job['status'] == 'waiting' and job.get('answer'):
                queued.append('answer')
            elif job['status'] in jobstatus.FINISHED and unread and self.settings.api_key:
                queued.append('comments')
            elif job['status'] in jobstatus.FINISHED and job.get('pending_messages') and self.settings.api_key:
                queued.append('message')  # sent as the run was finishing, so the agent never read it
            if queued:
                job.update(status='queued', error=None)

        self.store.mutate(job_id, claim)
        if queued:
            self.store.event(job_id, 'job.queued', f'Queued with your {queued[0]}', type='job')
            await self.queue.put(job_id)

    async def run_with_limit(self, job_id):
        # The engine stops itself at the Settings-page limit and saves; this outer limit is only a backstop.
        options = self.store.settings()
        minutes = options['time_limit_minutes']
        # Time spent waiting for answers does not count against the engine's limit, so the backstop allows for it.
        waits = options['question_wait_minutes'] * MAX_QUESTIONS
        deadline = asyncio.timeout((minutes + waits) * 60 + 300)
        try:
            async with deadline:
                await self.engine.run(job_id)
        except TimeoutError as error:
            # Other timeouts (a tool, a network call) are ordinary failures, not the job limit.
            if not deadline.expired():
                raise
            raise JobTimeout(
                f'Run exceeded the {minutes}-minute limit set on the Settings page. '
                'Saved progress is kept; resume to continue.'
            ) from error

    def fail(self, job_id, message, error_type):
        self.store.update(job_id, status='failed', error=message, artifacts=self.engine.artifacts(job_id, False))
        self.store.event(job_id, 'job.failed', message, level='ERROR', type='job', error_type=error_type)
