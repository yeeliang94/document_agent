"""The app side of the sandbox engine: settings, routing, questions, pauses and how a run settles. The engine run itself
is replaced by a fake here; prototype/sandbox_agent/tests covers the engine, and live runs cover OpenAI."""

import asyncio
import io
import json
import time
import zipfile
from types import SimpleNamespace

from fastapi.testclient import TestClient
from openpyxl import Workbook

import app.main as main_module
from app.config import Settings
from app.engine import service
from app.engine.runstore import RunStore
from app.engine.service import AppControl, resume_card
from app.main import create_app
from app.plain_errors import sentence
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


def test_a_script_is_described_while_it_streams_in_and_shown_once():
    from app.engine.agent import _log_stream

    class Recorder:
        def __init__(self):
            self.events = []

        def event(self, name, **data):
            self.events.append((name, data))

        def save_code(self, who, code, outputs):
            return '001-main.py'

    def raw(kind, **data):
        return SimpleNamespace(type='raw_response_event', data=SimpleNamespace(type=kind, **data))

    code = "# Check the bank file\nimport pandas\npandas.read_csv('/mnt/data/packet/Bank.csv')\n"
    item = SimpleNamespace(type='code_interpreter_call', id='ci_1', code=code, outputs=[])
    store, live, files = Recorder(), {}, [{'path': 'Bank.csv'}]
    stream = [
        raw('response.code_interpreter_call.in_progress', item_id='ci_1'),
        raw('response.code_interpreter_call_code.delta', item_id='ci_1', delta='# Check the ba'),
        raw('response.code_interpreter_call_code.delta', item_id='ci_1', delta='nk file\nimport pan'),
        raw('response.code_interpreter_call_code.delta', item_id='ci_1', delta='das\n'),
        raw('response.code_interpreter_call_code.done', item_id='ci_1', code=code),
        raw('response.output_text.delta', delta='ok'),
        raw('response.output_item.done', item=item),
        SimpleNamespace(type='run_item_stream_event', name='tool_called', item=SimpleNamespace(raw_item=item)),
    ]
    for event in stream:
        _log_stream(event, store, lambda *_: None, files, live)
    assert store.events == [
        ('code.started', {}),
        ('code.writing', {'summary': 'Check the bank file'}),  # once, as soon as its first line is written
        ('code.running', {'summary': 'Check the bank file', 'files': ['Bank.csv']}),
        ('code.finished', {'summary': 'Check the bank file', 'files': ['Bank.csv']}),
        ('code.ran', {'file': '001-main.py', 'chars': len(code), 'summary': 'Check the bank file',
                      'files': ['Bank.csv'], 'shown': True}),
    ]  # fmt: skip
    assert set(live) == {'ci_1'}  # other streamed events leave no trace


def test_the_feed_shows_each_script_live_and_once(tmp_path, monkeypatch):
    async def behaviour(outputs, on_event, **_):
        write_output(outputs)
        on_event('code.started', {})
        on_event('code.writing', {'summary': 'Check the bank file'})
        on_event('code.running', {'summary': '', 'files': ['Bank.csv']})
        on_event('code.finished', {'summary': 'Check the bank file', 'files': ['Bank.csv']})
        on_event('code.ran', {'file': '001-main.py', 'summary': 'Check the bank file', 'shown': True})
        on_event('code.finished', {'summary': '', 'files': []})
        return finished()

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'])
        events = [e for e in client.get(f'/api/jobs/{job["id"]}/events').json() if e['name'].startswith('engine.code')]
        shown = [(e['name'], e['message'], e['level'], e.get('files')) for e in events]
        assert shown == [
            ('engine.code.started', 'Running code in the sandbox', 'INFO', None),
            ('engine.code.writing', 'Check the bank file', 'INFO', None),
            ('engine.code.running', 'Reading', 'INFO', ['Bank.csv']),
            ('engine.code.finished', 'Check the bank file', 'INFO', ['Bank.csv']),
            ('engine.code.finished', 'Ran code in the sandbox', 'INFO', []),
        ]  # the end-of-reply copy of a script already shown is not repeated


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


def test_a_question_reaches_the_app_with_its_choices_and_evidence(tmp_path, monkeypatch):
    from app.engine.agent import Choice, Evidence, ask

    choices = [Choice(label=f'Option {n}', detail='', suggested=n == 1) for n in range(1, 7)]
    evidence = [Evidence(value='4,200', file='HR/Reports/employees.txt', where='line 1')]

    async def behaviour(answerer, outputs, store=None, **_):
        job = SimpleNamespace(
            store=SimpleNamespace(event=lambda *a, **k: None), answerer=answerer, waits=0, clock=None, held=()
        )
        reply = await ask(job, 'Which salary is right for EMP001?', choices, evidence)
        write_output(outputs)
        assert reply == 'Option 2'
        return finished()

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    monkeypatch.setattr(service, 'POLL_SECONDS', 0.02)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        question = wait_for_job(client, job['id'], statuses=('waiting',))['question']
        assert [c['label'] for c in question['choices']] == [
            'Option 1',
            'Option 2',
            'Option 3',
            'Option 4',
        ]  # at most 4
        assert question['choices'][0]['suggested'] is True
        assert question['evidence'] == [{'value': '4,200', 'file': 'HR/Reports/employees.txt', 'where': 'line 1'}]
        assert client.post(f'/api/jobs/{job["id"]}/answer', json={'answer': 'Option 2'}).status_code == 200
        assert wait_for_job(client, job['id'])['status'] == 'completed'


def test_an_unanswered_question_parks_the_job_and_the_answer_resumes_it(tmp_path, monkeypatch):
    async def first(answerer, control, outputs, **_):
        write_output(outputs)
        reply = await answerer.answer('Which salary is right for EMP001?')
        assert 'not answered yet' in reply and control.stop_requested() == 'waiting'
        assert 'notes.md' in reply and 'save_outputs' in reply  # a fresh sandbox picks up from what was saved
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
    assert 'allow the /v1/containers routes' in sentence('PermissionDeniedError: Error code: 403')
    assert sentence('something else').startswith('The run stopped because of an unexpected error.')
    missing = "NotFoundError: Error code: 404 - Item with id 'ci_0134039a' not found."
    assert 'containers routes' not in sentence(missing)  # a 403 inside an item id is not a refusal


def test_a_partial_run_keeps_its_workbook_as_a_draft(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        return {**finished(status='partial', passed=False), 'error': 'Reached the 60-turn limit.'}

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        done = wait_for_job(client, job['id'])
        assert done['status'] == 'partial' and 'The run used all its steps.' in done['summary']
        assert next(a for a in done['artifacts'] if a['name'] == 'template_output.xlsx')['status'] == 'draft'


def test_a_limit_stop_is_partial_and_can_continue(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        return {**finished(status='stopped', passed=False), 'error': 'Reached the token limit of 3,000,000.'}

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        done = wait_for_job(client, job['id'])
        assert done['status'] == 'partial' and 'all the AI usage it was allowed' in done['summary']
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
        upload = {'playbook_id': 'ap', 'period': '2026-11', 'instructions': 'Build the journal.'}
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


def test_a_resumed_run_reads_an_answer_with_the_choices_and_evidence_it_was_asked_with(tmp_path):
    from app.engine.service import take_answer

    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'x', 'y')
    question = {'text': 'Which month?', 'call_id': 'sandbox',
                'choices': [{'label': 'November', 'detail': 'Later approval', 'suggested': True}],
                'evidence': [{'value': '1 Nov', 'file': 'finance.txt', 'where': ''}]}  # fmt: skip
    store.update(job['id'], question=question, answer='You decide.')
    store.mutate(job['id'], take_answer)
    run = store.path(job['id']) / 'engine' / 'run-01'
    run.mkdir(parents=True)
    (run / 'run.json').write_text(json.dumps({'status': 'waiting'}), encoding='utf-8')
    card = resume_card(store, store.get(job['id']))
    assert '- Q: Which month?' in card and 'Evidence: 1 Nov (finance.txt)' in card
    assert 'Choice: November (you suggested this) - Later approval' in card and 'A: You decide.' in card


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
                assert client.post(f'/api/jobs/{job_id}/message', data={'message': 'Go on'}).status_code == 409
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
        assert client.post(url + '/message', data={'message': 'Hello'}).status_code == 409
        assert client.post(url + '/answer', json={'answer': 'Yes'}).status_code == 409
        assert client.post(url + '/start').status_code == 200
        wait_for_job(client, job['id'], statuses=('running',))
        assert client.post(url + '/start').status_code == 409  # already going
        assert client.post(url + '/message', data={'message': '  '}).status_code == 400
        nothing = client.post(url + '/interrupt')  # nothing waiting: a late click is not an error, and asks nothing
        assert nothing.status_code == 200 and not nothing.json().get('interrupt')
        assert client.post(url + '/message', data={'message': 'Leave out contractors.'}).status_code == 200
        [waiting] = client.get(url).json()['pending_messages']
        assert waiting['text'] == 'Leave out contractors.' and waiting['files'] == []
        assert client.post(url + '/pause').json()['status'] == 'pausing'
        release.set()
        assert wait_for_job(client, job['id'], statuses=('paused',))['status'] == 'paused'
        wait_for_runner_idle(client)
        assert client.post(url + '/cancel').json()['status'] == 'cancelled'
        assert client.post(url + '/pause').status_code == 409
        stopped = [e['message'] for e in client.get(url + '/events').json() if e['name'] == 'job.cancelled']
        assert stopped == ['Cancelled; existing output retained.']


def test_a_message_waits_until_a_cancelled_run_has_let_go(tmp_path, monkeypatch):
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
        assert client.post(url + '/start').status_code == 200
        wait_for_job(client, job['id'], statuses=('running',))
        assert client.post(url + '/cancel').json()['status'] == 'cancelled'
        # The engine has not seen the cancel yet: queuing now would undo it.
        refused = client.post(url + '/message', data={'message': 'Also check October.'})
        assert refused.status_code == 409 and 'still finishing' in refused.json()['detail']
        assert client.get(url).json()['status'] == 'cancelled'
        release.set()
        wait_for_runner_idle(client)
        assert client.get(url).json()['status'] == 'cancelled'
        assert client.post(url + '/message', data={'message': 'Also check October.'}).status_code == 200


def test_a_run_that_crashes_or_was_cut_off_by_a_restart_is_kept_for_resuming(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        raise RuntimeError('sandbox vanished, key sk-abcdefghijklmnop')

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        failed = wait_for_job(client, job['id'])
        # A plain sentence on the screen; the raw text, with any secret taken out, for the Technical log.
        assert failed['status'] == 'failed' and failed['error'].startswith('The run stopped because of an unexpected')
        assert 'sandbox vanished' in failed['error_detail'] and 'sk-abc' not in failed['error_detail']
        assert [a['name'] for a in failed['artifacts']] == ['notes.md', 'template_output.xlsx']  # drafts are kept
        wait_for_runner_idle(client)
        client.app.state.store.update(job['id'], status='running')  # as if the app stopped mid-run
    with client_for(tmp_path) as client:
        again = client.get(f'/api/jobs/{job["id"]}').json()
        assert again['status'] == 'paused' and not again.get('legacy')
        assert client.post(f'/api/jobs/{job["id"]}/resume').status_code == 200


def test_an_instruction_that_arrives_after_the_last_step_continues_the_run(tmp_path, monkeypatch):
    async def first(outputs, **_):
        write_output(outputs)
        return finished()  # the agent is done; it never reads the message below

    async def second(control, **_):
        assert control.new_messages() == []  # it opened the run instead; see the resume card below
        return finished()

    runs = iter([first, second])

    async def behaviour(**kwargs):
        run = next(runs)
        if run is first:
            store.update(job['id'], pending_messages=['Round to whole ringgit.'])  # sent during the final check
        return await run(**kwargs)

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    monkeypatch.setattr(service, 'POLL_SECONDS', 0)
    with client_for(tmp_path) as client:
        store = client.app.state.store
        job = start_sandbox_job(client)
        deadline = time.monotonic() + 10
        while len(fake.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        wait_for_job(client, job['id'], statuses=('completed', 'failed'))
        wait_for_runner_idle(client)
        done = client.get(f'/api/jobs/{job["id"]}').json()
        assert done['status'] == 'completed' and 'was not applied' not in done['summary']
        events = [e['message'] for e in client.get(f'/api/jobs/{job["id"]}/events').json()]
        assert 'Queued with your message' in events  # it went on by itself, with nobody pressing Continue
        # ...so the thread never tells the user to press Continue, nor shows the run as partly done.
        names = [e['name'] for e in client.get(f'/api/jobs/{job["id"]}/events').json()]
        assert 'job.partial' not in names and not any('Continue to apply' in m for m in events)
        # The run that continues opens with the instruction, marked as new, so it acts on it at once.
        card = fake.calls[1].resume
        assert 'New from the user since that run ended' in card and card.endswith('Round to whole ringgit.')
        assert 'Instructions the user gave during earlier runs' not in card


def test_the_prompt_cache_gets_a_stable_key_per_job_and_a_record_per_call():
    from app.engine.agent import _log_stream, _record_calls, _settings, cache_key
    from app.engine.endpoint import Settings as Endpoint

    official, gateway = Endpoint(api_key='k'), Endpoint(api_key='k', base_url='https://proxy.corp/v1')
    assert cache_key(official, 'job1', 'lead') == 'job1-lead' and cache_key(gateway, 'job1', 'lead') is None
    assert _settings('low', 'job1-lead').extra_args == {'prompt_cache_key': 'job1-lead'}
    assert _settings('low').extra_args is None

    events = []
    store = SimpleNamespace(event=lambda name, **data: events.append((name, data)))
    usage = SimpleNamespace(
        input_tokens=9000, output_tokens=300, input_tokens_details=SimpleNamespace(cached_tokens=8000)
    )
    done = SimpleNamespace(
        type='raw_response_event',
        data=SimpleNamespace(type='response.completed', response=SimpleNamespace(usage=usage)),
    )
    _log_stream(done, store, lambda *_: None)
    assert events == [
        ('model.usage', {'who': 'main', 'input_tokens': 9000, 'cached_tokens': 8000, 'output_tokens': 300})
    ]
    _record_calls(
        store, 'review', SimpleNamespace(raw_responses=[SimpleNamespace(usage=usage), SimpleNamespace(usage=None)])
    )
    assert events[1] == (
        'model.usage',
        {'who': 'review', 'input_tokens': 9000, 'cached_tokens': 8000, 'output_tokens': 300},
    )


def picture_bytes():
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.new('RGB', (4, 4), 'red').save(out, 'JPEG')
    return out.getvalue()


def test_a_message_to_a_finished_run_continues_it_with_the_files_it_brings(tmp_path, monkeypatch):
    seen = []

    async def first(outputs, **_):
        write_output(outputs)
        return finished()

    async def second(control, **_):
        seen.append(control.new_messages())  # taken at the start of the run, before this point
        return finished()

    runs = iter([first, second])

    async def behaviour(**kwargs):
        return await next(runs)(**kwargs)

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    monkeypatch.setattr(service, 'POLL_SECONDS', 0)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        url = f'/api/jobs/{job["id"]}'
        assert wait_for_job(client, job['id'])['status'] == 'completed'
        wait_for_runner_idle(client)
        sent = client.post(
            url + '/message',
            data={'message': 'Use the November rates too.'},
            files=[
                ('files', ('rates.csv', b'MYR,4.4', 'text/csv')),
                ('files', ('screen.jpg', picture_bytes(), 'image/jpeg')),
            ],
        )
        assert sent.status_code == 200, sent.text
        wait_for_runner_idle(client)
        done = wait_for_job(client, job['id'], statuses=('completed', 'failed'))
        assert done['status'] == 'completed' and len(fake.calls) == 2
        # The files sit in the packet's own top folder, so the earlier packet paths stay as they were.
        added = [f for f in done['files'] if f.get('added')]
        assert [f['path'] for f in added] == ['inputs/HR/Added/rates.csv', 'inputs/HR/Added/screen.jpg']
        names = zipfile.ZipFile(fake.calls[1].spec.zip).namelist()
        assert sorted(names) == ['HR/Added/rates.csv', 'HR/Added/screen.jpg', 'HR/Reports/employees.txt']
        card = fake.calls[1].resume
        assert 'Use the November rates too.' in card and 'Added/rates.csv, Added/screen.jpg' in card
        [picture] = fake.calls[1].options['resume_images']  # an added picture is shown to the agent too
        assert picture['name'] == 'screen.jpg' and picture['type'] == 'image/jpeg'
        [event] = [e for e in client.get(url + '/events').json() if e['name'] == 'user.instruction']
        assert event['message'] == 'Use the November rates too.' and [f['packet'] for f in event['files']] == [
            'Added/rates.csv', 'Added/screen.jpg']  # fmt: skip
        # A second file with the same name is kept beside the first.
        client.post(url + '/message', files=[('files', ('rates.csv', b'MYR,4.5', 'text/csv'))])
        wait_for_runner_idle(client)
        again = [f['path'] for f in client.get(url).json()['files'] if f.get('added')]
        assert again[-1] == 'inputs/HR/Added/rates (2).csv'


def test_files_sent_with_a_message_are_checked_before_anything_is_kept(tmp_path, monkeypatch):
    release = asyncio.Event()

    async def behaviour(outputs, **_):
        write_output(outputs)
        await release.wait()
        return finished()

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        url = f'/api/jobs/{job["id"]}'
        wait_for_job(client, job['id'], statuses=('running',))
        zipped = client.post(url + '/message', files=[('files', ('more.zip', b'PK', 'application/zip'))])
        assert zipped.status_code == 400 and 'one by one' in zipped.json()['detail']
        empty = client.post(
            url + '/message', files=[('files', ('a.csv', b'ok', 'text/csv')), ('files', ('b.csv', b'', 'text/csv'))]
        )
        assert empty.status_code == 400
        kept = client.get(url).json()
        assert not any(f.get('added') for f in kept['files']) and not kept.get('pending_messages')
        assert not (tmp_path / 'jobs' / job['id'] / 'inputs' / 'HR' / 'Added' / 'a.csv').exists()
        # A file that fails as it is being kept takes the ones kept before it in the same message with it.
        real_add, calls = main_module.add_file, []

        def add_then_fail(*args):
            calls.append(args)
            if len(calls) == 2:
                raise ValueError('Disk full')
            return real_add(*args)

        monkeypatch.setattr(main_module, 'add_file', add_then_fail)
        failed = client.post(
            url + '/message', files=[('files', ('a.csv', b'ok', 'text/csv')), ('files', ('b.csv', b'ok', 'text/csv'))]
        )
        assert failed.status_code == 400 and 'Disk full' in failed.json()['detail']
        monkeypatch.setattr(main_module, 'add_file', real_add)
        kept = client.get(url).json()
        assert not any(f.get('added') for f in kept['files']) and not kept.get('pending_messages')
        assert not (tmp_path / 'jobs' / job['id'] / 'inputs' / 'HR' / 'Added' / 'a.csv').exists()
        # While it runs, a message waits for the agent's next step; Send now asks for it to be read at once.
        assert (
            client.post(url + '/message', files=[('files', ('rates.csv', b'MYR,4.4', 'text/csv'))]).status_code == 200
        )
        assert client.get(url).json()['status'] == 'running'  # not queued again
        assert client.post(url + '/interrupt').json()['interrupt'] is True
        control = AppControl(client.app.state.store, job['id'])
        assert control.interrupt_requested()
        [message] = control.new_messages()
        assert message['text'] == 'Files the user added to the packet: Added/rates.csv.' and message['images'] == []
        assert message['files'][0]['local'].read_bytes() == b'MYR,4.4'
        assert not control.interrupt_requested() and not client.get(url).json()['interrupt']
        release.set()


def test_a_run_waiting_for_an_answer_takes_the_answer_first(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'x', 'y')
    store.update(job['id'], status='waiting', question={'text': 'Which month?'})
    with client_for(tmp_path) as client:
        refused = client.post(f'/api/jobs/{job["id"]}/message', data={'message': 'October'})
        assert refused.status_code == 409 and refused.json()['detail'] == 'Answer the question first'


def test_added_files_go_in_the_packets_top_folder_when_it_has_one():
    from app.files import added_name

    def manifest(*paths):
        return [{'path': p, 'role': 'inputs'} for p in paths]

    assert added_name(manifest('inputs/HR/a.txt', 'inputs/HR/b/c.txt'), 'x.pdf') == ('HR/Added/x.pdf', 'Added/x.pdf')
    assert added_name(manifest('inputs/a.txt', 'inputs/HR/c.txt'), 'x.pdf') == ('Added/x.pdf', 'Added/x.pdf')
    assert added_name(manifest('inputs/a.txt'), 'folder/x.pdf') == ('Added/x.pdf', 'Added/x.pdf')
    taken = manifest('inputs/HR/a.txt', 'inputs/HR/Added/X.pdf')
    assert added_name(taken, 'x.pdf') == ('HR/Added/x (2).pdf', 'Added/x (2).pdf')


def test_an_added_picture_is_shown_to_the_agent_no_larger_than_a_page(tmp_path):
    from PIL import Image

    from app import preview
    from app.engine.service import picture_of

    Image.new('RGB', (4000, 3000), 'white').save(tmp_path / 'photo.jpg', 'JPEG')
    shown = picture_of(tmp_path / 'photo.jpg')
    with Image.open(io.BytesIO(shown['data'])) as image:  # a photo travels as a JPEG: it is in every later request
        assert max(image.size) == preview.PAGE_PIXELS and image.format == 'JPEG' and shown['type'] == 'image/jpeg'
    Image.new('RGBA', (40, 30), (0, 0, 0, 0)).save(tmp_path / 'logo.png')
    clear = picture_of(tmp_path / 'logo.png')  # see-through parts stay see-through
    assert clear['type'] == 'image/png' and clear['data'][:4] == b'\x89PNG'
    from app.engine.control import user_content

    assert user_content('Look', [shown])[2]['image_url'].startswith('data:image/jpeg;base64,')
    (tmp_path / 'rates.csv').write_text('MYR,4.4')
    assert picture_of(tmp_path / 'rates.csv') is None  # only pictures are shown; every file is in the sandbox


def test_a_file_sent_as_a_run_restarts_is_in_its_packet(tmp_path, monkeypatch):
    import hashlib

    async def behaviour(outputs, **_):
        write_output(outputs)
        return finished()

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    real_take = service.AppControl.new_messages

    def file_arrives_first(self):
        # The file lands after the run read the job and before it takes the waiting messages.
        def add(job):
            if not any(f['path'].endswith('late.csv') for f in job['files']):
                path = self.store.path(self.job_id) / 'inputs' / 'HR' / 'Added' / 'late.csv'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'MYR,4.4')
                job['files'].append({'path': 'inputs/HR/Added/late.csv', 'size': 7, 'role': 'inputs', 'added': True,
                                     'sha256': hashlib.sha256(b'MYR,4.4').hexdigest(), 'supported': True})  # fmt: skip
                job['pending_messages'] = [*(job.get('pending_messages') or []),
                                           {'id': 'm2', 'text': '', 'files': [{'packet': 'Added/late.csv',
                                                                               'path': 'inputs/HR/Added/late.csv'}]}]  # fmt: skip

        if fake.calls:
            self.store.mutate(self.job_id, add)
        return real_take(self)

    monkeypatch.setattr(service.AppControl, 'new_messages', file_arrives_first)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'])
        assert client.post(f'/api/jobs/{job["id"]}/message', data={'message': 'Use the late rates.'}).status_code == 200
        deadline = time.monotonic() + 30
        while not (len(fake.calls) == 2 and client.get(f'/api/jobs/{job["id"]}').json()['status'] == 'completed'):
            assert time.monotonic() < deadline, 'the second run never finished'
            time.sleep(0.02)
        assert 'HR/Added/late.csv' in zipfile.ZipFile(fake.calls[1].spec.zip).namelist()


def test_more_steps_go_to_a_run_that_is_going_or_paused_and_are_taken_once(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'x', 'y')
    with client_for(tmp_path) as client:
        url = f'/api/jobs/{job["id"]}/more-steps'
        store.update(job['id'], status='completed')
        refused = client.post(url)
        assert refused.status_code == 409 and 'more steps' in refused.json()['detail']
        store.update(job['id'], status='paused')
        assert client.post(url).json()['more_turns'] == 20  # the Settings-page amount
        assert client.put('/api/settings', json={'values': {'more_steps': 30}}).status_code == 200
        assert client.post(url).json()['more_turns'] == 50
        control = AppControl(client.app.state.store, job['id'])
        assert control.more_turns() == 50 and control.more_turns() == 0  # given to the run once
        assert any(e['name'] == 'user.more_steps' for e in client.get(f'/api/jobs/{job["id"]}/events').json())


def test_condensing_is_asked_for_at_the_settings_size_and_noted_when_it_happens(tmp_path):
    from app.engine import agent
    from app.engine.service import MESSAGES

    assert agent._settings('low', None, 150_000).context_management == [
        {'type': 'compaction', 'compact_threshold': 150_000}
    ]
    assert agent._settings('low').context_management is None  # 0: never
    store = RunStore(tmp_path)
    usage = SimpleNamespace(input_tokens=23_000, output_tokens=10, input_tokens_details=None)
    condensed = SimpleNamespace(
        usage=usage, output=[SimpleNamespace(type='compaction'), SimpleNamespace(type='message')]
    )
    plain = SimpleNamespace(usage=usage, output=[SimpleNamespace(type='message')])
    agent._record_calls(store, 'review', SimpleNamespace(raw_responses=[plain, condensed]))
    lines = [json.loads(line) for line in (store.dir / 'events.jsonl').read_text().splitlines()]
    [event] = [e for e in lines if e['event'] == 'history.condensed']
    assert event['who'] == 'review' and event['input_tokens'] == 23_000
    assert MESSAGES['history.condensed'](event) == 'Condensed the older conversation to keep it small'


def test_every_setting_has_a_control_on_the_settings_page():
    from pathlib import Path

    from app.app_settings import OPTIONS

    page = (Path(__file__).parent.parent / 'web' / 'pages.js').read_text(encoding='utf-8')
    missing = [name for name in OPTIONS if f"'{name}'" not in page and f'"{name}"' not in page]
    assert missing == [], f'Settings with no control on the Settings page: {missing}'


def test_steps_given_too_late_for_a_run_do_not_carry_into_the_next(tmp_path):
    store = Store(tmp_path)
    engine = service.SandboxEngine(store, Settings())
    for ended, kept in (('complete', None), ('paused', 20)):
        job = store.create('payroll', '2026-10', 'x', 'y')
        (store.path(job['id']) / 'outputs').mkdir(parents=True, exist_ok=True)
        store.update(job['id'], status='running', more_turns=20)
        engine.settle(job['id'], {'status': ended}, waiting=False)
        assert store.get(job['id']).get('more_turns') == kept
