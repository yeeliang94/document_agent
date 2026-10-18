import asyncio

from app.config import Settings
from app.runtime import Runner
from app.store import Store


def test_restart_recovers_interrupted_jobs_without_rerunning_them(tmp_path):
    store = Store(tmp_path)
    jobs = {}
    for status in ('running', 'queued', 'pausing', 'waiting', 'cancelled', 'completed'):
        job = store.create('payroll', '2026-10', 'Persisted task', '')
        store.update(job['id'], status=status)
        jobs[status] = job['id']

    async def recover():
        runner = Runner(store, Settings(data_dir=tmp_path, api_key=''))
        await runner.start()
        await runner.close()

    asyncio.run(recover())
    for original in ('running', 'queued', 'pausing'):
        assert store.get(jobs[original])['status'] == 'paused'
        assert store.events(jobs[original])[-1]['name'] == 'job.recovered'
    for original in ('waiting', 'cancelled', 'completed'):
        assert store.get(jobs[original])['status'] == original
