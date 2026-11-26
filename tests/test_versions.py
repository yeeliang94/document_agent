"""Runs that stop can be picked up: a version at every save, continuing from one, plain errors and progress."""

import asyncio
import json
from types import SimpleNamespace

from agents.tool_context import ToolContext
from test_feedback import ENGINE_DEFAULTS
from test_sandbox_engine import FakeRun, client_for, finished, start_sandbox_job, wait_for_runner_idle

from app.engine import agent as engine
from app.engine import service, versions
from app.engine.runstore import RunStore
from app.plain_errors import explain, field_errors, sentence

from helpers import wait_for_job


def save(outputs, text):
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / 'out.xlsx').write_text(text, encoding='utf-8')  # stands in for a workbook
    (outputs / 'notes.md').write_text(f'notes at {text}', encoding='utf-8')


def test_each_save_is_kept_as_a_version_newest_first_and_each_run_keeps_only_its_newest(tmp_path):
    outputs, folder = tmp_path / 'outputs', tmp_path / 'versions'
    for step in (3, 7, 12):
        save(outputs, f'step {step}')
        versions.keep(outputs, folder, 'run-01', step, f'Step {step}: saved out.xlsx; checks passed', True, cap=2)
    kept = versions.listed(folder)
    assert [(v['number'], v['step']) for v in kept] == [(3, 12), (2, 7)]  # the run's oldest went past the cap
    assert kept[0]['files'] == ['notes.md', 'out.xlsx'] and kept[0]['run'] == 'run-01' and kept[0]['at']
    # A kept version is a copy: a file changed in place in the outputs (saved from Excel) leaves it as it was.
    save(outputs, 'later')
    assert (folder / 'v0002' / 'files' / 'out.xlsx').read_text(encoding='utf-8') == 'step 7'
    # Another run's versions are counted on their own.
    versions.keep(outputs, folder, 'run-02', 1, 'Step 1', False, cap=2)
    assert [v['run'] for v in versions.listed(folder)] == ['run-02', 'run-01', 'run-01']


def test_a_version_says_in_a_line_what_was_saved_and_how_the_checks_went():
    check = {'passed': False, 'issues': ['a', 'b']}
    assert versions.describe(14, ['notes.md', 'pay.xlsx', 'document_map.json'], check) == (
        'Step 14: saved pay.xlsx; 2 problems to fix'
    )
    assert versions.describe(3, ['notes.md'], {'passed': True}) == 'Step 3: saved notes only; checks passed'


def test_the_save_tool_keeps_a_version_with_the_step_it_was_made_at(tmp_path):
    class Box:
        async def list_files(self):
            return [{'id': 'f1', 'path': '/mnt/data/outputs/notes.md'}]

        async def download_id(self, _):
            return b'notes'

    job = SimpleNamespace(
        main=Box(), store=RunStore(tmp_path / 'runs'), spec=SimpleNamespace(deliverables=(), rules=(), skills=()),
        files=[], proof='none', saved=False, control=engine.Control(),
        **{**ENGINE_DEFAULTS, 'versions': tmp_path / 'versions', 'turns_used': 10, 'calls': 4},
    )  # fmt: skip
    context = ToolContext(job, tool_name='save_outputs', tool_call_id='1', tool_arguments='{}')
    json.loads(asyncio.run(engine.save_outputs.on_invoke_tool(context, '{}')))
    (kept,) = versions.listed(tmp_path / 'versions')
    assert (kept['step'], kept['files'], kept['passed']) == (14, ['notes.md'], False)
    assert kept['description'].startswith('Step 14: saved notes only; ')
    events = (job.store.dir / 'events.jsonl').read_text(encoding='utf-8')
    assert '"outputs.versioned"' in events and '"run.progress"' in events  # the save moved the stage on


def test_a_stopped_run_continues_from_an_earlier_version_with_those_outputs(tmp_path, monkeypatch):
    async def two_saves(outputs, **_):
        root = outputs.parent
        for step in (5, 9):
            save(outputs, f'step {step}')
            versions.keep(outputs, root / 'versions', 'run-01', step, f'Step {step}: saved out.xlsx', True, 20)
        return {**finished(status='stopped', passed=False, error='Reached the 60-turn limit.'), 'cut_short': 'turns'}

    seen = {}

    async def carry_on(outputs, **_):
        seen['outputs'] = (outputs / 'out.xlsx').read_text(encoding='utf-8')
        return finished()

    run = FakeRun(two_saves)
    monkeypatch.setattr(service, 'run_job', run)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        stopped = wait_for_job(client, job['id'])
        assert stopped['status'] == 'partial'
        listed = client.get(f'/api/jobs/{job["id"]}/versions').json()['versions']
        assert [(v['number'], v['step']) for v in listed] == [(2, 9), (1, 5)]
        wait_for_runner_idle(client)
        run.behaviour = carry_on
        # A plan from the later work, which going back to version 1 sets aside: the agent writes a new one.
        client.app.state.store.update(job['id'], plan={'steps': [{'step': 'Fill the template', 'status': 'done'}], 'why': '', 'run': 1})
        assert client.post(f'/api/jobs/{job["id"]}/versions/99/continue').status_code == 404
        assert client.post(f'/api/jobs/{job["id"]}/versions/1/continue').status_code == 200
        done = wait_for_job(client, job['id'], statuses=('completed',))
        assert seen['outputs'] == 'step 5'  # the run started from the first version, not the last save
        assert 'continue from version 1, saved at step 5 of run-01 (Step 5: saved out.xlsx)' in run.calls[-1].resume
        assert 'resume_from' not in done  # said once, by that run's card
        assert run.calls[-1].options['plan'] is None and 'Your plan from the earlier run' not in run.calls[-1].resume


def test_continuing_from_a_version_waits_until_the_run_has_stopped(tmp_path, monkeypatch):
    async def slow(outputs, control, **_):
        save(outputs, 'x')
        versions.keep(outputs, outputs.parent / 'versions', 'run-01', 1, 'Step 1', True, 20)
        while not control.stop_requested():
            await asyncio.sleep(0.02)
        return finished(status='paused')

    monkeypatch.setattr(service, 'run_job', FakeRun(slow))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'], statuses=('running',))
        refused = client.post(f'/api/jobs/{job["id"]}/versions/1/continue')
        assert refused.status_code == 409 and refused.json()['detail'] == 'Stop the run before continuing from a version.'
        client.post(f'/api/jobs/{job["id"]}/pause')
        wait_for_job(client, job['id'], statuses=('paused',))


def test_known_failures_read_as_plain_sentences_with_a_next_step():
    cases = {
        'Add OPENAI_API_KEY to .env and restart the application.': 'No API key is set.',
        "AuthenticationError: Error code: 401 - {'error': {'code': 'invalid_api_key'}}": 'did not accept the API key',
        'NotFoundError: Error code: 404 - Container not found': "workspace (its sandbox) could not be found",
        'PermissionDeniedError: Error code: 403 - forbidden': 'refused the request',
        service.DAY_USED_UP: "Today's token limit is used up.",
        "RateLimitError: Error code: 429 - {'code': 'insufficient_quota'}": 'run out of credit',
        'RateLimitError: Error code: 429 - slow down': 'busy',
        'APIConnectionError: Connection error.': 'could not reach the AI service',
        'Reached the 60-turn limit.': 'used all its steps',
    }
    for raw, wanted in cases.items():
        plain = explain(raw)
        assert wanted in plain.message and plain.next and plain.raw == raw, raw
        assert 'Error code' not in plain.sentence
    assert explain(None) is None and sentence('') is None
    assert explain(RuntimeError('odd')).raw == 'RuntimeError: odd'


def test_form_errors_and_the_connection_test_answer_in_sentences(tmp_path, monkeypatch):
    assert field_errors([{'type': 'missing', 'loc': ('body', 'answer')}]) == 'Answer is missing.'
    assert field_errors([{'type': 'less_than_equal', 'loc': ('query', 'recent'), 'ctx': {'le': 500}}]) == (
        'Recent must be at most 500.'
    )
    with client_for(tmp_path) as client:
        wrong = client.get('/api/playbooks/payroll/events?recent=900')
        assert wrong.status_code == 422 and wrong.json()['detail'] == 'Recent must be at most 500.'
        wrong = client.get('/api/playbooks/payroll/events?after=soon')
        assert wrong.json()['detail'] == 'After must be a number.'

        class Refusing:
            def __init__(self, **_):
                pass

            async def __aenter__(self):
                raise RuntimeError('Connection error.')

            async def __aexit__(self, *_):
                return False

        monkeypatch.setattr('app.main.AsyncOpenAI', Refusing)
        failed = client.post('/api/connection-check')
        assert failed.status_code == 502
        assert failed.json()['detail']['message'].startswith('The app could not reach the AI service.')
        assert failed.json()['detail']['raw'] == 'RuntimeError: Connection error.'


def test_the_job_carries_its_progress_for_the_screen(tmp_path, monkeypatch):
    async def stepping(on_event, **_):
        on_event('run.progress', {'steps': 14, 'max_steps': 60, 'minutes': 6, 'max_minutes': 30, 'stage': 'filling',
                                  'clock_paused': False})  # fmt: skip
        return finished()

    monkeypatch.setattr(service, 'run_job', FakeRun(stepping))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        done = wait_for_job(client, job['id'])
        assert done['progress'] == {'steps': 14, 'max_steps': 60, 'minutes': 6, 'max_minutes': 30, 'stage': 'filling',
                                    'clock_paused': False, 'run': 1}  # fmt: skip
        feed = client.get(f'/api/jobs/{job["id"]}/events').json()
        assert not any('progress' in e['name'] for e in feed)  # on the job, not a line in the feed


def test_each_step_of_the_lead_counts_and_the_clock_shows_minutes_used(tmp_path):
    async def go():
        store = RunStore(tmp_path / 'runs')
        job = SimpleNamespace(store=store, tools_running=[], **{**ENGINE_DEFAULTS, 'turns_used': 10, 'time_limit': 1800})
        async with asyncio.timeout(1800) as clock:
            job.clock = clock
            await engine.TOOL_WATCH.on_llm_start(SimpleNamespace(context=job), SimpleNamespace(name='lead'))
            await engine.TOOL_WATCH.on_llm_start(SimpleNamespace(context=job), SimpleNamespace(name='scout'))
        lines = [json.loads(line) for line in (store.dir / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        return [line for line in lines if line['event'] == 'run.progress']

    (only,) = asyncio.run(go())  # a scout's or reviewer's call is not a step of the run
    assert (only['steps'], only['max_steps'], only['minutes'], only['max_minutes'], only['stage']) == (
        11, 60, 0, 30, 'reading'
    )


def test_a_second_click_on_continue_is_refused_while_the_first_is_restoring(tmp_path, monkeypatch):
    async def stops(outputs, **_):
        save(outputs, 'v1')
        versions.keep(outputs, outputs.parent / 'versions', 'run-01', 1, 'Step 1', True, 20)
        return {**finished(status='stopped', passed=False, error='Reached the 60-turn limit.'), 'cut_short': 'turns'}

    monkeypatch.setattr(service, 'run_job', FakeRun(stops))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'])
        wait_for_runner_idle(client)
        store = client.app.state.store
        store.update(job['id'], status='queued')  # as the first click leaves it while it restores
        second = client.post(f'/api/jobs/{job["id"]}/versions/1/continue')
        assert second.status_code == 409 and second.json()['detail'] == 'Stop the run before continuing from a version.'


def test_a_sentence_the_app_wrote_for_people_is_shown_as_it_is():
    raw = 'ValueError: big.pdf is over 45 MB even compressed; it cannot be uploaded to the sandbox. Split or remove it.'
    assert sentence(raw) == raw.removeprefix('ValueError: ')


def test_an_output_named_like_the_versions_own_record_is_kept_as_it_was(tmp_path):
    outputs, folder = tmp_path / 'outputs', tmp_path / 'versions'
    outputs.mkdir()
    (outputs / 'version.json').write_text('{"the": "deliverable"}', encoding='utf-8')
    versions.keep(outputs, folder, 'run-01', 1, 'Step 1', True, cap=5)
    (outputs / 'version.json').write_text('changed', encoding='utf-8')
    versions.restore(folder, 1, outputs)
    assert (outputs / 'version.json').read_text(encoding='utf-8') == '{"the": "deliverable"}'


def test_starting_over_empties_the_outputs_keeps_the_versions_and_tells_the_agent_to_begin_again(tmp_path, monkeypatch):
    async def stops(outputs, **_):
        save(outputs, 'v1')
        versions.keep(outputs, outputs.parent / 'versions', 'run-01', 1, 'Step 1', True, 20)
        return {**finished(status='stopped', passed=False, error='Reached the 60-turn limit.'), 'cut_short': 'turns'}

    seen = {}

    async def again(outputs, **_):
        seen['outputs'] = sorted(p.name for p in outputs.iterdir())
        return finished()

    run = FakeRun(stops)
    monkeypatch.setattr(service, 'run_job', run)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'])
        wait_for_runner_idle(client)
        client.app.state.store.update(job['id'], resume_from={'number': 1, 'step': 1, 'run': 'run-01', 'description': 'x'})
        run.behaviour = again
        (tmp_path / 'jobs' / job['id'] / 'outputs' / 'out.xlsx').write_text('changed by hand', encoding='utf-8')
        assert client.post(f'/api/jobs/{job["id"]}/start-over').status_code == 200
        done = wait_for_job(client, job['id'], statuses=('completed',))
        assert seen['outputs'] == []  # nothing of the earlier work went into the new run
        card = run.calls[-1].resume
        assert card.startswith('You are starting this job over') and 'notes at v1' not in card and 'version 1' not in card
        # The files set aside (changed since version 1) are kept as a version first, so starting over loses nothing.
        kept = client.get(f'/api/jobs/{job["id"]}/versions').json()['versions']
        assert [(v['number'], v['description']) for v in kept] == [(2, 'Before starting over'), (1, 'Step 1')]
        assert 'start_over' not in done and 'resume_from' not in done


def test_a_version_kept_before_files_had_their_own_folder_can_be_restored(tmp_path):
    outputs, folder = tmp_path / 'outputs', tmp_path / 'versions'
    outputs.mkdir()
    old = folder / 'v0001'
    old.mkdir(parents=True)
    (old / 'upload.csv').write_text('old', encoding='utf-8')
    (old / 'version.json').write_text('{"number": 1, "files": ["upload.csv"]}', encoding='utf-8')
    versions.restore(folder, 1, outputs)
    assert (outputs / 'upload.csv').read_text(encoding='utf-8') == 'old'


def test_a_run_deleted_while_it_waited_in_the_queue_does_not_stop_the_worker(tmp_path, monkeypatch):
    async def done(outputs, **_):
        save(outputs, 'v1')
        return finished()

    monkeypatch.setattr(service, 'run_job', FakeRun(done))
    with client_for(tmp_path) as client:
        client.portal.call(client.app.state.runner.queue.put, 'a-run-that-was-deleted')
        job = start_sandbox_job(client)
        assert wait_for_job(client, job['id'])['status'] == 'completed'


def test_starting_over_waits_until_the_run_has_stopped(tmp_path, monkeypatch):
    async def slow(outputs, control, **_):
        while not control.stop_requested():
            await asyncio.sleep(0.02)
        return finished(status='paused')

    monkeypatch.setattr(service, 'run_job', FakeRun(slow))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'], statuses=('running',))
        refused = client.post(f'/api/jobs/{job["id"]}/start-over')
        assert refused.status_code == 409 and refused.json()['detail'] == 'Stop the run before starting over.'
        client.post(f'/api/jobs/{job["id"]}/pause')
        wait_for_job(client, job['id'], statuses=('paused',))


def test_going_back_to_a_version_first_keeps_newer_files_as_a_version(tmp_path, monkeypatch):
    async def stops(outputs, **_):
        save(outputs, 'v1')
        versions.keep(outputs, outputs.parent / 'versions', 'run-01', 1, 'Step 1', True, 20)
        save(outputs, 'v2')  # the run's last save keeps no version
        return {**finished(status='stopped', passed=False, error='Reached the 60-turn limit.'), 'cut_short': 'turns'}

    async def again(outputs, **_):
        return finished()

    run = FakeRun(stops)
    monkeypatch.setattr(service, 'run_job', run)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'])
        wait_for_runner_idle(client)
        run.behaviour = again
        assert client.post(f'/api/jobs/{job["id"]}/versions/1/continue').status_code == 200
        wait_for_job(client, job['id'], statuses=('completed',))
        kept = client.get(f'/api/jobs/{job["id"]}/versions').json()['versions']
        assert (kept[0]['number'], kept[0]['description']) == (2, 'Before going back to version 1')


def test_outputs_that_match_the_newest_version_are_not_kept_twice(tmp_path):
    outputs, folder = tmp_path / 'outputs', tmp_path / 'versions'
    outputs.mkdir()
    assert not versions.matches_latest(outputs, folder)  # nothing kept yet
    (outputs / 'a.csv').write_text('1', encoding='utf-8')
    versions.keep(outputs, folder, 'run-01', 1, 'Step 1', True, 20)
    assert versions.matches_latest(outputs, folder)
    (outputs / 'a.csv').write_text('2', encoding='utf-8')
    assert not versions.matches_latest(outputs, folder)
    (outputs / 'a.csv').write_text('1', encoding='utf-8')
    (outputs / 'b.csv').write_text('new', encoding='utf-8')
    assert not versions.matches_latest(outputs, folder)

