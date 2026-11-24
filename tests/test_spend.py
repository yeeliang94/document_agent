"""The day's token limit: every run counts towards it, and a run that would start past it waits."""

import asyncio
import json

import pytest
from test_replay import app_with_a_case
from test_sandbox_engine import FakeRun, client_for, finished, write_output

from app import replay
from app.engine import service

from helpers import create_job, wait_for_job


def used(tokens, **record):
    """A finished run's record that used this many tokens."""
    lead = [{'requests': 3, 'input_tokens': tokens - 100, 'output_tokens': 100}]
    return {**finished(**record), 'usage': {'lead': lead, 'scouts': [], 'reviews': []}}


def test_runs_count_towards_the_day_and_stop_starting_once_it_is_used_up(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        return used(60_000)

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    with client_for(tmp_path) as client:
        store = client.app.state.store
        page = client.get('/api/settings').json()
        assert page['values']['daily_token_limit_thousands'] == 30000 and page['tokens_today'] == 0
        client.put('/api/settings', json={'values': {'daily_token_limit_thousands': 100}})
        first = create_job(client)
        assert client.post(f'/api/jobs/{first["id"]}/start').status_code == 200
        wait_for_job(client, first['id'], {'completed'})
        assert fake.calls[0].options['max_tokens'] == 100_000  # what is left of the day, not the larger run limit
        assert client.get('/api/settings').json()['tokens_today'] == 60_000
        # 40,000 are left: too little to finish even a small run, so the day counts as used up. The next run is
        # refused with the reason, and nothing reaches the agent.
        assert store.tokens_left_today() == 40_000 and store.day_used_up()
        second = create_job(client)
        refused = client.post(f'/api/jobs/{second["id"]}/start')
        assert refused.status_code == 409 and "Today's token limit is used up" in refused.json()['detail']
        assert client.post('/api/playbooks/payroll/tests/check').status_code == 409
        assert len(fake.calls) == 1
        # A run queued another way (a comment, an answer) waits as paused, says why, and can start later.
        asyncio.run(client.app.state.runner.engine.run(second['id']))
        held = store.get(second['id'])
        assert held['status'] == 'paused' and held['error'] == service.DAY_USED_UP and len(fake.calls) == 1
        assert store.events(second['id'])[-1]['level'] == 'WARN'  # the screen shows this message, not "Paused."
        # Raising the limit, or 0 for none, lets it go.
        client.put('/api/settings', json={'values': {'daily_token_limit_thousands': 0}})
        assert store.tokens_left_today() is None and not store.day_used_up()
        assert client.post(f'/api/jobs/{second["id"]}/start').status_code == 200
        wait_for_job(client, second['id'], {'completed'})
        assert fake.calls[1].options['max_tokens'] == 3_000_000 and store.spent_today() == 120_000


def test_a_run_the_day_limit_cut_short_says_so(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        return {**used(100_000, status='stopped', error='Reached the token limit of 100,000.'), 'cut_short': 'tokens'}

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        client.put('/api/settings', json={'values': {'daily_token_limit_thousands': 100}})
        job = create_job(client)
        client.post(f'/api/jobs/{job["id"]}/start')
        done = wait_for_job(client, job['id'], {'partial'})
        assert service.DAY_USED_UP in done['summary'] and done['cut_short'] == 'day'


def test_a_run_cut_off_before_it_settles_still_counts_towards_the_day(tmp_path, monkeypatch):
    async def cut_off(spec, endpoint, runs_root, *, run_id, **_):
        (runs_root / run_id).mkdir(parents=True)
        record = {'usage': {'lead': [{'input_tokens': 7000, 'output_tokens': 500}], 'scouts': [], 'reviews': []}}
        (runs_root / run_id / 'run.json').write_text(json.dumps(record), encoding='utf-8')
        raise asyncio.CancelledError  # the app is stopping

    monkeypatch.setattr(service, 'run_job', cut_off)
    with client_for(tmp_path) as client:
        store = client.app.state.store
        job = create_job(client)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(client.app.state.runner.engine.run(job['id']))
        assert store.spent_today() == 7500


def test_a_replayed_test_the_service_or_the_day_limit_stopped_counts_as_not_checked(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        store, case = app_with_a_case(client)
        check = replay.start(store, 'payroll', 'asked')
        job_id = check['cases'][0]['job_id']
        # The run ended partly done because the AI service failed. Its half-made output says nothing about the
        # instructions, so the test is not marked wrong.
        store.update(job_id, status='partial', cut_short='service')
        closed = replay.finished(store, store.get(job_id))
        entry = closed['cases'][0]
        assert (entry['status'], entry['note']) == ('error', 'the AI service stopped answering')
        assert replay.summary(store, 'payroll')['checked'] == 0
