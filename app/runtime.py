"""The job queue. One worker runs jobs one at a time on the sandbox engine (app/engine)."""

import asyncio

from . import jobstatus
from .config import Settings
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

    async def start(self):
        for job in self.store.jobs():
            if not job.get('legacy') and (self.store.path(job['id']) / 'working' / 'history.json').exists():
                self.retire(job)
            elif job['status'] in jobstatus.ACTIVE:  # No worker survives a restart.
                self.store.update(job['id'], status='paused', error=None)
                self.store.event(
                    job['id'], 'job.recovered', 'Interrupted job retained. Resume when ready.', type='job', level='WARN'
                )
        self.worker = asyncio.create_task(self.work())

    def retire(self, job):
        """A run the earlier engine worked on keeps its conversation and files for reading. Its saved state means
        nothing to this engine, so it is closed for good rather than restarted without the user's earlier answers."""
        unfinished = job['status'] not in jobstatus.FINISHED | {'failed', 'cancelled'}
        self.store.update(job['id'], legacy=True, question=None, **({'status': 'cancelled'} if unfinished else {}))
        if unfinished:
            self.store.event(job['id'], 'job.cancelled', LEGACY_NOTE, type='job')

    async def close(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass

    async def submit(self, job_id):
        self.store.update(job_id, status='queued', error=None)
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

    async def resume_if_answered(self, job_id):
        """Queue a job whose question was answered after its run stopped holding the sandbox. Safe to call from
        both the answer endpoint and the worker: only the first call that finds the answer queues the job."""
        queued = []

        def claim(job):
            if job['status'] == 'waiting' and job.get('answer'):
                job.update(status='queued', error=None)
                queued.append(True)

        self.store.mutate(job_id, claim)
        if queued:
            self.store.event(job_id, 'job.queued', 'Queued with your answer', type='job')
            await self.queue.put(job_id)

    async def run_with_limit(self, job_id):
        # The engine stops itself at the Settings-page limit and saves; this outer limit is only a backstop.
        minutes = self.store.settings()['time_limit_minutes']
        deadline = asyncio.timeout(minutes * 60 + 300)
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
