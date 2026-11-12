"""The app side of the sandbox engine: settings, routing, questions, pauses and how a run settles. The engine run itself
is replaced by a fake here; prototype/sandbox_agent/tests covers the engine, and live runs cover OpenAI."""

import json
import time
import zipfile
from types import SimpleNamespace

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.config import Settings
from app.engine import service
from app.engine.service import AppControl, resume_card, with_hint
from app.main import create_app
from app.store import Store

from helpers import LOCAL, create_job, wait_for_job


def client_for(tmp_path):
    return TestClient(base_url=LOCAL, app=create_app(Settings(data_dir=tmp_path, api_key='test-key')))


def test_settings_page_values_are_stored_and_checked(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get('/api/settings').json()
        assert body['values']['engine'] == 'classic' and body['options']['engine']['choices'] == ['classic', 'sandbox']
        saved = client.put('/api/settings', json={'values': {'engine': 'sandbox', 'max_scouts': 2}}).json()
        assert saved['values']['engine'] == 'sandbox' and saved['values']['max_scouts'] == 2
        bad = client.put('/api/settings', json={'values': {'max_scouts': 99}})
        assert bad.status_code == 400 and 'from 0 to 8' in bad.json()['detail']
        assert client.put('/api/settings', json={'values': {'colour': 'red'}}).status_code == 400
    with client_for(tmp_path) as client:  # survives a restart
        assert client.get('/api/settings').json()['values']['max_scouts'] == 2


def test_a_job_keeps_the_engine_it_was_created_with(tmp_path):
    with client_for(tmp_path) as client:
        client.put('/api/settings', json={'values': {'engine': 'sandbox'}})
        job = create_job(client, legacy=False)
        client.put('/api/settings', json={'values': {'engine': 'classic'}})
        assert client.get(f'/api/jobs/{job["id"]}').json()['engine'] == 'sandbox'
        assert create_job(client, legacy=False)['engine'] == 'classic'


class FakeRun:
    """Stands in for run_job: records what it was given, then does what the test scripts."""

    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    async def __call__(
        self, spec, endpoint, runs_root, *, run_id, outputs, on_event, control, resume, answerer, **opts
    ):
        self.calls.append(SimpleNamespace(spec=spec, endpoint=endpoint, resume=resume, options=opts))
        (runs_root / run_id).mkdir(parents=True)
        record = await self.behaviour(outputs=outputs, on_event=on_event, control=control, answerer=answerer)
        (runs_root / run_id / 'run.json').write_text(json.dumps(record), encoding='utf-8')
        return SimpleNamespace(record=record)


def finished(status='complete', passed=True, error=None):
    return {
        'status': status,
        'error': error,
        'final_check': {'passed': passed, 'issues': [] if passed else ['provenance.json has not been saved yet.']},
        'summary': {'status': 'complete', 'summary': 'All rows filled.', 'open_questions': []},
        'usage': {'lead': [{'requests': 3, 'input_tokens': 900, 'output_tokens': 100}], 'scouts': [], 'reviews': []},
    }


def write_output(outputs):
    Workbook().save(outputs / 'template_output.xlsx')
    (outputs / 'notes.md').write_text('Used employees.txt.', encoding='utf-8')


def start_sandbox_job(client):
    client.put('/api/settings', json={'values': {'engine': 'sandbox', 'max_scouts': 3, 'reviewer': True}})
    job = create_job(client, legacy=False)
    assert client.post(f'/api/jobs/{job["id"]}/start').status_code == 200
    return job


def test_a_sandbox_job_runs_on_the_engine_and_settles(tmp_path, monkeypatch):
    async def behaviour(outputs, on_event, **_):
        write_output(outputs)
        on_event('outputs.saved', {'passed': True})
        on_event('code.ran', {'file': '001-main.py'})  # kept in the run record only
        return finished()

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        done = wait_for_job(client, job['id'])
        assert done['status'] == 'completed' and done['request_count'] == 3
        assert {a['name']: a.get('status') for a in done['artifacts']} == {
            'notes.md': None, 'processing_summary.txt': None, 'template_output.xlsx': 'validated'}  # fmt: skip
        call = fake.calls[0]
        assert call.options['max_scouts'] == 3 and call.options['review_on'] is True and call.resume is None
        assert 'Fill Employees A2:D2.' in call.spec.goal and '2026-10' in call.spec.goal
        assert call.spec.deliverables[0].name == 'template_output.xlsx'
        assert call.spec.context == 'Synthetic test company'
        assert zipfile.ZipFile(call.spec.zip).namelist() == ['HR/Reports/employees.txt']
        messages = [e['message'] for e in client.get(f'/api/jobs/{job["id"]}/events').json()]
        assert 'Saved outputs; checks passed' in messages and not any('001-main' in m for m in messages)


def test_a_question_is_answered_in_the_app_while_the_sandbox_waits(tmp_path, monkeypatch):
    async def behaviour(answerer, outputs, **_):
        reply = await answerer.answer('Which salary is right for EMP001: 4,200 or 4,500?')
        write_output(outputs)
        assert reply == 'Use 4,500.'
        return finished()

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    monkeypatch.setattr(service, 'POLL_SECONDS', 0.02)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        waiting = wait_for_job(client, job['id'], statuses=('waiting',))
        assert waiting['question']['text'].startswith('Which salary')
        assert client.post(f'/api/jobs/{job["id"]}/answer', json={'answer': 'Use 4,500.'}).status_code == 200
        done = wait_for_job(client, job['id'])
        assert done['status'] == 'completed' and len(fake.calls) == 1  # answered in the same run, not resubmitted
        assert done['engine_questions'] == [{'question': waiting['question']['text'], 'answer': 'Use 4,500.'}]


def test_an_unanswered_question_parks_the_job_and_the_answer_resumes_it(tmp_path, monkeypatch):
    async def first(answerer, control, outputs, **_):
        write_output(outputs)
        reply = await answerer.answer('Which salary is right for EMP001?')
        assert 'not answered yet' in reply and control.stop_requested() == 'waiting'
        return finished(status='waiting')

    async def second(outputs, **_):
        return finished()

    runs = iter([first, second])

    async def behaviour(**kwargs):
        return await next(runs)(**kwargs)

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    monkeypatch.setattr(service, 'POLL_SECONDS', 0.01)
    monkeypatch.setattr(service, 'SECONDS_PER_MINUTE', 0.2)
    with client_for(tmp_path) as client:
        client.put('/api/settings', json={'values': {'question_wait_minutes': 1}})
        job = start_sandbox_job(client)
        parked = wait_for_job(client, job['id'], statuses=('waiting',), timeout=10)
        wait_for_runner_idle(client)
        assert parked['question']['text'] == 'Which salary is right for EMP001?'
        client.post(f'/api/jobs/{job["id"]}/answer', json={'answer': 'Use 4,500.'})
        done = wait_for_job(client, job['id'])
        assert done['status'] == 'completed' and len(fake.calls) == 2
        card = fake.calls[1].resume
        assert 'fresh sandbox' in card and 'Used employees.txt.' in card and 'A: Use 4,500.' in card


def wait_for_runner_idle(client, timeout=5):
    deadline = time.monotonic() + timeout
    while client.app.state.runner.active is not None and time.monotonic() < deadline:
        time.sleep(0.02)


def test_pause_cancel_and_new_instructions_reach_the_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'POLL_SECONDS', 0)
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'x', 'y')
    control = AppControl(store, job['id'])
    assert control.stop_requested() is None and control.new_messages() == []
    store.update(job['id'], pending_messages=['Use the November rate.'])
    assert control.new_messages() == ['Use the November rate.'] and control.new_messages() == []
    store.update(job['id'], status='pausing')
    assert control.stop_requested() == 'paused'
    store.update(job['id'], status='cancelled')
    assert control.stop_requested() == 'cancelled'


def test_a_paused_engine_run_resumes_from_its_record(tmp_path, monkeypatch):
    async def first(control, outputs, **_):
        write_output(outputs)
        return {**finished(status='paused'), 'final_check': {'passed': False, 'issues': ['Row 3 is blank.']}}

    async def second(**_):
        return finished()

    runs = iter([first, second])

    async def behaviour(**kwargs):
        return await next(runs)(**kwargs)

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        assert wait_for_job(client, job['id'], statuses=('paused',))['status'] == 'paused'
        wait_for_runner_idle(client)
        client.post(f'/api/jobs/{job["id"]}/resume')
        assert wait_for_job(client, job['id'])['status'] == 'completed'
        assert '- Row 3 is blank.' in fake.calls[1].resume
        assert sorted(p.name for p in (tmp_path / 'jobs' / job['id'] / 'engine').iterdir()) == ['run-01', 'run-02']


def test_failures_explain_the_proxy_region_and_permission_errors(tmp_path, monkeypatch):
    async def behaviour(**_):
        return {
            **finished(status='failed', error='NotFoundError: Error code: 404 - container not found'),
            'summary': None,
        }

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        failed = wait_for_job(client, job['id'])
        assert failed['status'] == 'failed' and 'different regions' in failed['error']
    assert 'allow the /v1/containers routes' in with_hint('PermissionDeniedError: Error code: 403')
    assert with_hint('something else') == 'something else'


def test_a_complete_claim_with_failing_checks_is_partial(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        return finished(passed=False)

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        done = wait_for_job(client, job['id'])
        assert done['status'] == 'partial'
        assert next(a for a in done['artifacts'] if a['name'] == 'template_output.xlsx')['status'] == 'draft'


def test_resume_card_is_empty_before_any_run(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'x', 'y')
    assert resume_card(store, job) is None
