"""Cost per run (plan Step 51): a money figure from a run's tokens and the prices on the Settings page."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_replay import app_with_a_case
from test_sandbox_engine import client_for

from app import replay
from app.engine import service

from helpers import create_job

# $2.50 in, $0.25 cached in, $10 out, per million tokens, in the ten-thousandths of a dollar the setting keeps.
PRICES = {'price_input_per_million': 25_000, 'price_cached_per_million': 2_500, 'price_output_per_million': 100_000}


def record(lead, scouts=(), reviews=()):
    return {'status': 'complete', 'usage': {'lead': list(lead), 'scouts': list(scouts), 'reviews': list(reviews)}}


def part(input_tokens, cached, output_tokens):
    return {'requests': 1, 'input_tokens': input_tokens, 'cached_input_tokens': cached, 'output_tokens': output_tokens}


def settle(store, job_id, ended):
    service.SandboxEngine(store, SimpleNamespace(api_key='', base_url='', model='m')).settle(job_id, ended, False)


def test_a_run_costs_its_lead_scouts_and_reviews_priced_now(tmp_path):
    with client_for(tmp_path) as client:
        store = client.app.state.store
        job = create_job(client)
        lead = [part(600_000, 400_000, 20_000), part(200_000, 0, 5_000)]
        settle(store, job['id'], record(lead, [part(100_000, 0, 10_000)], [part(100_000, 100_000, 5_000)]))
        assert client.get(f'/api/jobs/{job["id"]}').json()['cost'] is None  # no prices yet: no figure
        client.put('/api/settings', json={'values': PRICES})
        cost = client.get(f'/api/jobs/{job["id"]}').json()['cost']
        assert {k: cost[k] for k in ('input_tokens', 'cached_tokens', 'output_tokens')} == {
            'input_tokens': 1_000_000,
            'cached_tokens': 500_000,
            'output_tokens': 40_000,
        }
        # 500,000 uncached in at $2.50, 500,000 cached at $0.25, 40,000 out at $10.
        assert cost['usd'] == pytest.approx(1.25 + 0.125 + 0.40)
        # A price changed later prices the same tokens again: no money is kept.
        client.put('/api/settings', json={'values': {'price_output_per_million': 0}})
        assert client.get(f'/api/jobs/{job["id"]}').json()['cost']['usd'] == pytest.approx(1.375)


def test_without_a_cached_price_cached_tokens_cost_what_other_input_does(tmp_path):
    with client_for(tmp_path) as client:
        store = client.app.state.store
        job = create_job(client)
        settle(store, job['id'], record([part(1_000_000, 800_000, 0)]))
        client.put('/api/settings', json={'values': {'price_input_per_million': 20_000}})
        assert client.get(f'/api/jobs/{job["id"]}').json()['cost']['usd'] == pytest.approx(2.0)


def test_every_engine_run_of_a_job_counts_one_cut_off_too(tmp_path, monkeypatch):
    async def cut_off(spec, endpoint, runs_root, *, run_id, **_):
        (runs_root / run_id).mkdir(parents=True)
        ended = record([part(300_000, 0, 30_000)])
        (runs_root / run_id / 'run.json').write_text(json.dumps({**ended, 'seconds': 12}), encoding='utf-8')
        raise asyncio.CancelledError  # the app is stopping before the run settles

    monkeypatch.setattr(service, 'run_job', cut_off)
    with client_for(tmp_path) as client:
        store = client.app.state.store
        client.put('/api/settings', json={'values': PRICES})
        job = create_job(client)
        settle(store, job['id'], record([part(100_000, 0, 10_000)]))  # the first run
        settle(store, job['id'], record([part(100_000, 100_000, 10_000)]))  # one an answer started
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(client.app.state.runner.engine.run(job['id']))  # one cut off, then resumed later
        body = client.get(f'/api/jobs/{job["id"]}').json()
        assert len(body['runs']) == 3 and body['runs'][-1]['seconds'] == 12
        assert body['usage']['input_tokens'] == 100_000  # the last settled run's own, as before
        cost = body['cost']
        assert (cost['input_tokens'], cost['cached_tokens'], cost['output_tokens']) == (500_000, 100_000, 50_000)
        assert cost['usd'] == pytest.approx(400_000 * 2.5e-6 + 100_000 * 0.25e-6 + 50_000 * 10e-6)


def test_a_playbook_totals_its_runs_and_its_replayed_tests_apart(tmp_path):
    with client_for(tmp_path) as client:
        store, _case = app_with_a_case(client)  # one payroll run already, which used no tokens
        first, second = create_job(client), create_job(client)
        settle(store, first['id'], record([part(400_000, 0, 10_000)]))
        settle(store, second['id'], record([part(600_000, 200_000, 30_000)]))
        other = store.create('ap', '2026-10', 'Pay suppliers.', 'rules')
        settle(store, other['id'], record([part(9_000_000, 0, 0)]))  # another playbook's run is not counted

        # A replayed test: its tokens count for the playbook, apart from its runs, and are kept after its hidden
        # job is removed by the next check.
        check = replay.start(store, 'payroll', 'asked')
        job_id = check['cases'][0]['job_id']
        settle(store, job_id, record([part(200_000, 0, 20_000)]))
        replay.finished(store, store.get(job_id))
        replay.finished(store, store.get(job_id))  # told twice, counted once
        replay.start(store, 'payroll', 'asked again')
        with pytest.raises(KeyError):
            store.get(job_id)

        assert client.get('/api/playbooks/payroll').json()['cost'] is None  # no prices: no figure
        client.put('/api/settings', json={'values': PRICES})
        body = client.get('/api/playbooks/payroll').json()
        assert body['name'] == 'Payroll'
        runs, replays = body['cost']['runs'], body['cost']['replays']
        assert runs['count'] == 3 and replays['count'] == 1  # the replays are not among the runs
        assert (runs['input_tokens'], runs['cached_tokens'], runs['output_tokens']) == (1_000_000, 200_000, 40_000)
        assert runs['usd'] == pytest.approx(800_000 * 2.5e-6 + 200_000 * 0.25e-6 + 40_000 * 10e-6)
        assert (replays['input_tokens'], replays['output_tokens']) == (200_000, 20_000)
        assert replays['usd'] == pytest.approx(200_000 * 2.5e-6 + 20_000 * 10e-6)
        assert client.get('/api/playbooks/ap').json()['cost']['replays'] == {
            'count': 0,
            'usd': 0,
            'input_tokens': 0,
            'cached_tokens': 0,
            'output_tokens': 0,
        }


def test_prices_of_zero_show_no_money_figure(tmp_path):
    with client_for(tmp_path) as client:
        store = client.app.state.store
        job = create_job(client)
        settle(store, job['id'], record([part(1_000_000, 0, 100_000)]))
        client.put('/api/settings', json={'values': PRICES})
        assert client.get(f'/api/jobs/{job["id"]}').json()['cost']['usd'] > 0
        # Back to 0 for input and output: no figure on the run or the playbook, whatever the cached price says.
        client.put('/api/settings', json={'values': {'price_input_per_million': 0, 'price_output_per_million': 0}})
        assert client.get(f'/api/jobs/{job["id"]}').json()['cost'] is None
        assert client.get('/api/playbooks/payroll').json()['cost'] is None


def test_a_price_is_a_whole_number_of_ten_thousandths_of_a_dollar(tmp_path):
    with client_for(tmp_path) as client:
        assert client.put('/api/settings', json={'values': {'price_input_per_million': -1}}).status_code == 400
        assert client.put('/api/settings', json={'values': {'price_input_per_million': 2.5}}).status_code == 400
        page = client.get('/api/settings').json()
        assert page['values']['price_output_per_million'] == 0
        assert page['options']['price_output_per_million']['high'] == 10_000_000  # $1,000 per million tokens
