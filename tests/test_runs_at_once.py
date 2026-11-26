"""Several runs at once: up to the Settings-page number run side by side, the rest wait in line and say so, and
pause, resume and a restart touch each run on its own. The engine run is a fake that holds until the test lets it go."""

import asyncio
import time

from test_sandbox_engine import FakeRun, client_for, finished, write_output

from app.engine import service

from helpers import create_job, wait_for_job


class Held:
    """A fake run that keeps going until the test releases its job, and stops when the job is paused."""

    def __init__(self):
        self.going, self.released, self.most = set(), set(), 0

    async def __call__(self, control, outputs, **_):
        self.going.add(control.job_id)
        self.most = max(self.most, len(self.going))
        try:
            while control.job_id not in self.released:
                if control.stop_requested():
                    return finished(status='paused')
                await asyncio.sleep(0.01)
            write_output(outputs)
            return finished()
        finally:
            self.going.discard(control.job_id)


def held(monkeypatch):
    monkeypatch.setattr(service, 'POLL_SECONDS', 0)
    behaviour = Held()
    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    return behaviour, fake


def start(client):
    job = create_job(client)
    assert client.post(f'/api/jobs/{job["id"]}/start').status_code == 200
    return job['id']


def until(check, timeout=10):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, 'timed out'
        time.sleep(0.02)


def status(client, job_id):
    return client.get(f'/api/jobs/{job_id}').json()['status']


def names(client, job_id):
    return [e['name'] for e in client.get(f'/api/jobs/{job_id}/events').json()]


def test_runs_at_once_is_a_setting_of_two(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get('/api/settings').json()
        assert body['values']['runs_at_once'] == 2 and body['options']['runs_at_once']['label'] == 'Runs at once'
        assert client.put('/api/settings', json={'values': {'runs_at_once': 0}}).status_code == 400


def test_two_runs_go_side_by_side_and_a_third_waits_for_a_free_slot(tmp_path, monkeypatch):
    behaviour, _ = held(monkeypatch)
    with client_for(tmp_path) as client:
        a, b = start(client), start(client)
        until(lambda: behaviour.going == {a, b})
        c = start(client)
        waiting = client.get(f'/api/jobs/{c}').json()
        assert waiting['status'] == 'queued' and waiting['waiting_for_slot'] is True
        note = [e for e in client.get(f'/api/jobs/{c}/events').json() if e['name'] == 'job.waiting_for_slot']
        assert note[0]['message'] == 'Waiting for a free slot: 2 runs at once is the limit on the Settings page'
        time.sleep(0.2)
        assert c not in behaviour.going and status(client, c) == 'queued'
        behaviour.released.add(a)
        assert wait_for_job(client, a)['status'] == 'completed'
        until(lambda: c in behaviour.going)
        assert client.get(f'/api/jobs/{c}').json().get('waiting_for_slot') is None
        assert 'job.waiting_for_slot' not in names(client, a)  # the first two never waited
        behaviour.released.update({b, c})
        assert wait_for_job(client, b)['status'] == wait_for_job(client, c)['status'] == 'completed'
        assert behaviour.most == 2


def test_a_change_to_the_limit_applies_without_a_restart(tmp_path, monkeypatch):
    behaviour, _ = held(monkeypatch)
    with client_for(tmp_path) as client:
        client.put('/api/settings', json={'values': {'runs_at_once': 1}})
        a, b = start(client), start(client)
        until(lambda: behaviour.going == {a})
        assert client.get(f'/api/jobs/{b}').json()['waiting_for_slot'] is True
        client.put('/api/settings', json={'values': {'runs_at_once': 3}})
        behaviour.released.add(a)
        until(lambda: b in behaviour.going)
        c, d = start(client), start(client)
        until(lambda: behaviour.going == {b, c, d})
        behaviour.released.update({b, c, d})
        for job_id in (b, c, d):
            assert wait_for_job(client, job_id)['status'] == 'completed'


def test_pausing_and_resuming_one_run_leaves_the_other_going(tmp_path, monkeypatch):
    behaviour, fake = held(monkeypatch)
    with client_for(tmp_path) as client:
        a, b = start(client), start(client)
        until(lambda: behaviour.going == {a, b})
        assert client.post(f'/api/jobs/{a}/pause').status_code == 200
        assert wait_for_job(client, a, statuses=('paused',))['status'] == 'paused'
        assert status(client, b) == 'running' and b in behaviour.going
        until(lambda: not client.app.state.runner.is_running(a))
        assert client.app.state.runner.is_running(b)
        assert client.post(f'/api/jobs/{a}/resume').status_code == 200
        until(lambda: behaviour.going == {a, b})
        behaviour.released.update({a, b})
        assert wait_for_job(client, a)['status'] == wait_for_job(client, b)['status'] == 'completed'
        assert len(fake.calls) == 3


def test_a_job_queued_twice_never_runs_twice_at_once(tmp_path, monkeypatch):
    behaviour, fake = held(monkeypatch)
    with client_for(tmp_path) as client:
        a = start(client)
        until(lambda: a in behaviour.going)
        client.portal.call(client.app.state.runner.queue.put, a)  # a second place in line for a run that is going
        time.sleep(0.2)
        assert len(fake.calls) == 1 and behaviour.most == 1
        behaviour.released.add(a)
        assert wait_for_job(client, a)['status'] == 'completed'
        time.sleep(0.2)
        assert len(fake.calls) == 1  # looked at again once the run ended, and skipped: nothing queued it


def test_a_restart_with_two_runs_going_keeps_both_for_resuming(tmp_path, monkeypatch):
    behaviour, _ = held(monkeypatch)
    with client_for(tmp_path) as client:
        a, b = start(client), start(client)
        until(lambda: behaviour.going == {a, b})
        c = start(client)  # waiting in line when the app stops
    with client_for(tmp_path) as client:
        for job_id in (a, b, c):
            job = client.get(f'/api/jobs/{job_id}').json()
            assert job['status'] == 'paused' and not job.get('waiting_for_slot'), job
        assert 'job.paused' in names(client, a) and 'job.paused' in names(client, b)
        assert client.post(f'/api/jobs/{a}/resume').status_code == 200
        assert client.post(f'/api/jobs/{b}/resume').status_code == 200
        until(lambda: behaviour.going == {a, b})
        behaviour.released.update({a, b})
        assert wait_for_job(client, a)['status'] == wait_for_job(client, b)['status'] == 'completed'
