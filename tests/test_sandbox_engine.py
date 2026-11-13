"""The app side of the sandbox engine: settings, routing, questions, pauses and how a run settles. The engine run itself
is replaced by a fake here; prototype/sandbox_agent/tests covers the engine, and live runs cover OpenAI."""

import asyncio
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
        assert body['values']['max_scouts'] == 0 and body['options']['proof']['choices'] == ['row', 'none']
        saved = client.put('/api/settings', json={'values': {'reviewer': True, 'max_scouts': 2}}).json()
        assert saved['values']['reviewer'] is True and saved['values']['max_scouts'] == 2
        assert client.put('/api/settings', json={'values': {'engine': 'classic'}}).status_code == 400  # one engine now
        bad = client.put('/api/settings', json={'values': {'max_scouts': 99}})
        assert bad.status_code == 400 and 'from 0 to 8' in bad.json()['detail']
        assert client.put('/api/settings', json={'values': {'colour': 'red'}}).status_code == 400
    with client_for(tmp_path) as client:  # survives a restart
        assert client.get('/api/settings').json()['values']['max_scouts'] == 2


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
    client.put('/api/settings', json={'values': {'max_scouts': 3, 'reviewer': True}})
    job = create_job(client)
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


def test_the_feed_shows_what_a_script_was_for(tmp_path, monkeypatch):
    async def behaviour(outputs, on_event, **_):
        write_output(outputs)
        on_event(
            'code.ran', {'file': '001-main.py', 'summary': 'List the HR folder', 'files': ['HR/Reports/employees.txt']}
        )
        on_event('code.ran', {'file': '002-main.py', 'summary': ''})  # no opening comment, so nothing shown
        on_event('scout.started', {'folder': 'HR', 'files': 1})
        return finished()

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'])
        events = {e['name']: e for e in client.get(f'/api/jobs/{job["id"]}/events').json() if e['type'] == 'engine'}
        assert events['engine.code.ran']['message'] == 'List the HR folder'
        assert events['engine.code.ran']['files'] == ['HR/Reports/employees.txt']
        assert [e for e in events if e.startswith('engine.code')] == ['engine.code.ran']
        assert 'files' not in events['engine.scout.started']  # a count, not a list of files


def test_a_script_is_described_by_its_first_comment_and_the_files_it_names():
    from app.engine.agent import code_files, code_summary

    code = "#!/usr/bin/env python\n# Read the payroll register\nimport openpyxl\nopenpyxl.load_workbook('/mnt/data/packet/HR/register.xlsx')"
    assert code_summary(code) == 'Read the payroll register'
    assert code_summary('import os\n# list the folder\nprint(os.listdir())') == ''  # never a line of code
    assert code_summary('') == ''
    files = [{'path': 'HR/register.xlsx'}, {'path': 'HR/other.pdf'}, {'path': 'Old/ster.xlsx'}]
    assert code_files(code, files) == ['HR/register.xlsx']  # "ster.xlsx" is only part of another name


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
            'final_check': None,
        }

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        failed = wait_for_job(client, job['id'])
        assert failed['status'] == 'failed' and 'different regions' in failed['error']
        assert failed['engine_check'] is None  # never checked, so no green pills
    assert 'allow the /v1/containers routes' in with_hint('PermissionDeniedError: Error code: 403')
    assert with_hint('something else') == 'something else'


def test_a_partial_run_keeps_its_workbook_as_a_draft(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        return {**finished(status='partial', passed=False), 'error': 'Reached the 60-turn limit.'}

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        done = wait_for_job(client, job['id'])
        assert done['status'] == 'partial' and 'turn limit' in done['summary']
        assert next(a for a in done['artifacts'] if a['name'] == 'template_output.xlsx')['status'] == 'draft'


def test_a_limit_stop_is_partial_and_can_continue(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        return {**finished(status='stopped', passed=False), 'error': 'Reached the token limit of 3,000,000.'}

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        done = wait_for_job(client, job['id'])
        assert done['status'] == 'partial' and 'token limit' in done['summary']
        assert (tmp_path / 'jobs' / job['id'] / 'outputs' / 'processing_summary.txt').exists()


def test_an_answer_sent_while_the_run_winds_down_still_resumes_the_job(tmp_path, monkeypatch):
    """The question wait ends, the user answers while the run is still saving, and the job must not be stranded."""
    winding_down = {}

    async def first(answerer, outputs, **_):
        write_output(outputs)
        await answerer.answer('Which salary is right for EMP001?')  # times out
        winding_down['now'] = True
        while not winding_down.get('answered'):  # the user answers before this run has finished
            await asyncio.sleep(0.01)
        return finished(status='partial')

    async def second(**_):
        return finished()

    runs = iter([first, second])

    async def behaviour(**kwargs):
        return await next(runs)(**kwargs)

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    monkeypatch.setattr(service, 'POLL_SECONDS', 0.01)
    monkeypatch.setattr(service, 'SECONDS_PER_MINUTE', 0.1)
    with client_for(tmp_path) as client:
        client.put('/api/settings', json={'values': {'question_wait_minutes': 1}})
        job = start_sandbox_job(client)
        deadline = time.monotonic() + 10
        while not winding_down.get('now') and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.post(f'/api/jobs/{job["id"]}/answer', json={'answer': 'Use 4,500.'}).status_code == 200
        winding_down['answered'] = True
        done = wait_for_job(client, job['id'])
        assert done['status'] == 'completed' and len(fake.calls) == 2
        assert 'A: Use 4,500.' in fake.calls[1].resume


def test_a_cancel_during_the_last_turn_is_not_overwritten(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        store = Store(tmp_path)
        job_id = next(iter(j['id'] for j in store.jobs()))
        store.update(job_id, status='cancelled')  # the user cancels as the agent finishes
        return finished()

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        assert wait_for_job(client, job['id'], statuses=('cancelled', 'completed'))['status'] == 'cancelled'


def test_settings_show_the_endpoint_and_warn_about_regions(tmp_path):
    settings = Settings(data_dir=tmp_path, api_key='k', base_url='https://genai-emea.example.com/v1',
                        model='openai.global.gpt-6-luna')  # fmt: skip
    with TestClient(base_url=LOCAL, app=create_app(settings)) as client:
        connection = client.get('/api/settings').json()['connection']
        assert connection['model'] == 'openai.global.gpt-6-luna' and '"global"' in connection['warning']
        bad = client.put('/api/settings', json={'values': {'litellm_provider': 'openai\nX-Evil: 1'}})
        assert bad.status_code == 400


def test_a_format_upload_makes_a_built_workbook_deliverable(tmp_path, monkeypatch):
    fmt = {'sheets': [{'name': 'Journal', 'columns': [{'name': 'Ref', 'type': 'text', 'required': True}]}]}

    async def behaviour(outputs, **_):
        return finished()

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    with client_for(tmp_path) as client:
        files = [('files', ('a.csv', b'x', 'text/csv'))]
        upload = {'module': 'ap', 'period': '2026-11', 'instructions': 'Build the journal.'}
        broken = client.post('/api/jobs', data=upload, files=[('template', ('j.json', b'{"sheets": []}')), *files])
        assert broken.status_code == 400 and 'at least one sheet' in broken.json()['detail']
        job = client.post(
            '/api/jobs', data=upload, files=[('template', ('journal.json', json.dumps(fmt).encode())), *files]
        )
        assert job.status_code == 201
        client.post(f'/api/jobs/{job.json()["id"]}/start')
        wait_for_job(client, job.json()['id'])
        deliverable = fake.calls[0].spec.deliverables[0]
        assert deliverable.name == 'journal.xlsx' and deliverable.format == fmt


def test_resume_card_is_empty_before_any_run(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'x', 'y')
    assert resume_card(store, job) is None


def test_the_document_map_and_check_summary_reach_the_screens(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        (outputs / 'document_map.json').write_text(
            json.dumps({'files': [{'path': 'HR/a.txt', 'decision': 'Not relevant', 'reason': 'Other entity'}]}),
            encoding='utf-8',
        )
        record = finished()
        record['final_check']['summary'] = {'deliverable_problems': 0, 'files_decided': 1, 'files': 1}
        record['reviews'] = [{'verdict': 'doubts', 'doubts': [{'issue': 'x'}]}]
        return record

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        client.put('/api/settings', json={'values': {'max_scouts': 3, 'reviewer': True}})
        job = create_job(client)
        assert client.get(f'/api/jobs/{job["id"]}/document-map').json() == {'files': [], 'error': None}  # not yet
        client.post(f'/api/jobs/{job["id"]}/start')
        done = wait_for_job(client, job['id'])
        assert done['engine_check']['summary']['files_decided'] == 1
        assert done['engine_check']['reviews'] == 1 and done['engine_check']['doubts'] == 1
        assert done['engine_team'] == {'max_scouts': 3, 'reviewer': True}
        files = client.get(f'/api/jobs/{job["id"]}/document-map').json()['files']
        assert files == [{'path': 'HR/a.txt', 'decision': 'not_relevant', 'reason': 'Other entity'}]


def test_parallel_questions_reach_the_user_one_at_a_time(tmp_path, monkeypatch):
    answers = {}

    async def behaviour(answerer, outputs, **_):
        write_output(outputs)
        first, second = await asyncio.gather(
            answerer.answer('Which salary is right for EMP001?'), answerer.answer('Which rate applies to EMP002?')
        )
        answers.update(first=first, second=second)
        return finished()

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    monkeypatch.setattr(service, 'POLL_SECONDS', 0.01)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        for reply in ('Use 4,500.', 'Use the November rate.'):
            asked = wait_for_job(client, job['id'], statuses=('waiting',))['question']['text']
            client.post(f'/api/jobs/{job["id"]}/answer', json={'answer': reply})
            deadline = time.monotonic() + 5
            while client.get(f'/api/jobs/{job["id"]}').json().get('question', {}) and time.monotonic() < deadline:
                current = client.get(f'/api/jobs/{job["id"]}').json()
                if (current.get('question') or {}).get('text') != asked:
                    break
                time.sleep(0.01)
        done = wait_for_job(client, job['id'])
        assert [q['answer'] for q in done['engine_questions']] == ['Use 4,500.', 'Use the November rate.']
        assert answers == {'first': 'Use 4,500.', 'second': 'Use the November rate.'}


def test_a_time_limit_during_a_question_keeps_the_question_for_the_user(tmp_path, monkeypatch):
    async def behaviour(answerer, outputs, **_):
        write_output(outputs)
        try:
            await asyncio.wait_for(answerer.answer('Which salary is right for EMP001?'), 0.1)
        except TimeoutError:
            pass
        return {**finished(status='stopped', passed=False), 'error': 'Reached the 30-minute time limit.'}

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    monkeypatch.setattr(service, 'POLL_SECONDS', 0.01)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        parked = wait_for_job(client, job['id'], statuses=('waiting', 'partial'))
        assert parked['status'] == 'waiting' and parked['question']['text'] == 'Which salary is right for EMP001?'


def test_instructions_given_during_a_run_carry_into_a_resumed_run(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'POLL_SECONDS', 0)
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'x', 'y')
    store.update(job['id'], pending_messages=['Use the November FX rate.'])
    assert AppControl(store, job['id']).new_messages() == ['Use the November FX rate.']
    run = store.path(job['id']) / 'engine' / 'run-01'
    run.mkdir(parents=True)
    (run / 'run.json').write_text(json.dumps({'status': 'paused'}), encoding='utf-8')
    card = resume_card(store, store.get(job['id']))
    assert '- Use the November FX rate.' in card and 'override the goal' in card


def test_a_run_from_the_earlier_engine_can_be_read_but_not_continued(tmp_path):
    """Its saved answers and state mean nothing to this engine, so it must not quietly start over."""
    store = Store(tmp_path)
    old = store.create('payroll', '2026-10', 'Fill it.', 'ctx')
    store.update(old['id'], status='waiting', question={'call_id': 'c1', 'text': 'Confirm the period?'})
    (store.path(old['id']) / 'working' / 'history.json').write_text('[]')
    (store.path(old['id']) / 'outputs' / 'draft.xlsx').write_bytes(b'kept')
    done = store.create('payroll', '2026-09', 'Fill it.', 'ctx')
    store.update(done['id'], status='partial', summary='Two rows open.')
    (store.path(done['id']) / 'working' / 'history.json').write_text('[]')
    for _ in range(2):  # the second start changes nothing more
        with client_for(tmp_path) as client:
            job = client.get(f'/api/jobs/{old["id"]}').json()
            assert job['legacy'] and job['status'] == 'cancelled' and job['question'] is None
            kept = client.get(f'/api/jobs/{done["id"]}').json()
            assert kept['legacy'] and kept['status'] == 'partial' and kept['summary'] == 'Two rows open.'
            for job_id in (old['id'], done['id']):
                assert client.post(f'/api/jobs/{job_id}/resume').status_code == 409
                assert client.post(f'/api/jobs/{job_id}/answer', json={'answer': 'Yes'}).status_code == 409
                assert client.post(f'/api/jobs/{job_id}/message', json={'message': 'Go on'}).status_code == 409
            notes = [e['message'] for e in client.get(f'/api/jobs/{old["id"]}/events').json()]
            assert len([n for n in notes if 'earlier version' in n]) == 1
    assert (store.path(old['id']) / 'outputs' / 'draft.xlsx').read_bytes() == b'kept'


def test_pause_stop_and_messages_are_accepted_only_when_they_can_take_effect(tmp_path, monkeypatch):
    release = asyncio.Event()

    async def behaviour(control, outputs, **_):
        write_output(outputs)
        await release.wait()
        return finished(status=control.stop_requested() or 'complete')

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    monkeypatch.setattr(service, 'POLL_SECONDS', 0)
    with client_for(tmp_path) as client:
        job = create_job(client)
        url = f'/api/jobs/{job["id"]}'
        assert client.post(url + '/pause').status_code == 409  # nothing running yet
        assert client.post(url + '/message', json={'message': 'Hello'}).status_code == 409
        assert client.post(url + '/answer', json={'answer': 'Yes'}).status_code == 409
        assert client.post(url + '/start').status_code == 200
        wait_for_job(client, job['id'], statuses=('running',))
        assert client.post(url + '/start').status_code == 409  # already going
        assert client.post(url + '/message', json={'message': '  '}).status_code == 400
        assert client.post(url + '/message', json={'message': 'Leave out contractors.'}).status_code == 200
        assert client.get(url).json()['pending_messages'] == ['Leave out contractors.']
        assert client.post(url + '/pause').json()['status'] == 'pausing'
        release.set()
        assert wait_for_job(client, job['id'], statuses=('paused',))['status'] == 'paused'
        wait_for_runner_idle(client)
        assert client.post(url + '/cancel').json()['status'] == 'cancelled'
        assert client.post(url + '/pause').status_code == 409
        stopped = [e['message'] for e in client.get(url + '/events').json() if e['name'] == 'job.cancelled']
        assert stopped == ['Cancelled; existing output retained.']


def test_a_run_that_crashes_or_was_cut_off_by_a_restart_is_kept_for_resuming(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        raise RuntimeError('sandbox vanished, key sk-abcdefghijklmnop')

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        failed = wait_for_job(client, job['id'])
        assert (
            failed['status'] == 'failed' and 'sandbox vanished' in failed['error'] and 'sk-abc' not in failed['error']
        )
        assert [a['name'] for a in failed['artifacts']] == ['notes.md', 'template_output.xlsx']  # drafts are kept
        wait_for_runner_idle(client)
        client.app.state.store.update(job['id'], status='running')  # as if the app stopped mid-run
    with client_for(tmp_path) as client:
        again = client.get(f'/api/jobs/{job["id"]}').json()
        assert again['status'] == 'paused' and not again.get('legacy')
        assert client.post(f'/api/jobs/{job["id"]}/resume').status_code == 200


def test_an_instruction_that_arrives_after_the_last_step_keeps_the_run_open(tmp_path, monkeypatch):
    async def first(outputs, **_):
        write_output(outputs)
        return finished()  # the agent is done; it never reads the message below

    async def second(control, **_):
        assert control.new_messages() == ['Round to whole ringgit.']
        return finished()

    runs = iter([first, second])

    async def behaviour(**kwargs):
        run = next(runs)
        if run is first:
            store.update(job['id'], pending_messages=['Round to whole ringgit.'])  # sent during the final check
        return await run(**kwargs)

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    monkeypatch.setattr(service, 'POLL_SECONDS', 0)
    with client_for(tmp_path) as client:
        store = client.app.state.store
        job = start_sandbox_job(client)
        held = wait_for_job(client, job['id'])
        assert held['status'] == 'partial' and 'was not applied' in held['summary']
        assert [a['status'] for a in held['artifacts'] if a['name'].endswith('.xlsx')] == ['validated']
        wait_for_runner_idle(client)
        assert client.post(f'/api/jobs/{job["id"]}/resume').status_code == 200
        done = wait_for_job(client, job['id'], statuses=('completed', 'failed'))
        assert done['status'] == 'completed' and 'was not applied' not in done['summary']
