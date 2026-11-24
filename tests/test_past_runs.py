"""Earlier runs of the same playbook: which ones a job is offered, the goal card, the tool and the run record."""

import asyncio
import json
from types import SimpleNamespace

from agents.tool_context import ToolContext
from fastapi.testclient import TestClient
from test_skill_engine import REMOTE, FakeBox, events, spec_with_skills

from app.config import Settings
from app.engine import agent as engine
from app.engine import pastruns, service
from app.engine.endpoint import Settings as Endpoint
from app.engine.goal import goal_card
from app.engine.jobspec import JobSpec, load_job
from app.engine.runstore import RunStore
from app.main import create_app

from helpers import LOCAL, workbook_bytes


def spec_with_past_runs(tmp_path, count=2):
    spec = spec_with_skills(tmp_path, skills=())
    runs = []
    for number in range(1, count + 1):
        folder = tmp_path / 'past' / str(number)
        (folder / 'sub').mkdir(parents=True)
        (folder / 'out.xlsx').write_bytes(b'workbook %d' % number)
        (folder / 'notes.md').write_text('Dates as text.', encoding='utf-8')
        (folder / 'sub' / 'extra.csv').write_text('a,b\n', encoding='utf-8')
        runs.append({'label': f'earlier-{number}', 'id': f'job{number}', 'title': f'2026-0{number} - Payroll',
                     'finished': f'2026-0{number}-28', 'goal': 'Prepare payroll.', 'outputs': folder})  # fmt: skip
    return JobSpec(**{**spec.__dict__, 'past_runs': tuple(runs)})


def test_the_goal_card_lists_earlier_runs_and_says_they_are_examples(tmp_path):
    spec = spec_with_past_runs(tmp_path)
    card = goal_card(spec, REMOTE, 'row')
    assert '# Earlier runs\n' in card and 'open_past_run' in card
    assert '- earlier-1: 2026-01 - Payroll (finished 2026-01-28)\n- earlier-2: 2026-02 - Payroll' in card
    assert "every value you write comes from this job's own files" in card
    assert card.index('# House rules') < card.index('# Earlier runs') < card.index('# Deliverables')
    plain = goal_card(JobSpec(**{**spec.__dict__, 'past_runs': ()}), REMOTE, 'row')
    assert 'Earlier runs' not in plain and 'open_past_run' not in plain


def test_opening_an_earlier_run_copies_its_files_and_records_it(tmp_path, monkeypatch):
    spec = spec_with_past_runs(tmp_path)
    box, store = FakeBox(), RunStore(tmp_path / 'runs')
    job = SimpleNamespace(spec=spec, store=store, main=box, past_opened={})
    got = asyncio.run(pastruns.open_run(job, 'earlier-2'))
    assert [f['file'] for f in got['files']] == ['notes.md', 'out.xlsx', 'sub/extra.csv']
    assert got['files'][2]['path'] == '/mnt/data/earlier-2__sub__extra.csv'
    assert box.uploads['earlier-2__out.xlsx'] == b'workbook 2' and got['goal'] == 'Prepare payroll.'
    # Asking again copies nothing twice, and a wrong label is answered with the right ones.
    box.uploads.clear()
    assert asyncio.run(pastruns.open_run(job, 'earlier-2')) == got and not box.uploads
    assert 'The earlier runs are: earlier-1, earlier-2' in asyncio.run(pastruns.open_run(job, 'last month'))['error']
    opened = [e for e in events(store) if e['event'] == 'past_run.opened']
    assert [(e['run'], e['job'], e['files']) for e in opened] == [('earlier-2', 'job2', 3)]
    # A file that is too large is left out and named; a run whose files are gone says so.
    monkeypatch.setattr(pastruns, 'PAST_FILE_BYTES', 5)
    got = asyncio.run(pastruns.open_run(job, 'earlier-1'))
    assert got['left_out'] == ['notes.md', 'out.xlsx'] and [f['file'] for f in got['files']] == ['sub/extra.csv']
    gone = JobSpec(**{**spec.__dict__, 'past_runs': ({**spec.past_runs[0], 'outputs': tmp_path / 'nowhere'},)})
    job = SimpleNamespace(spec=gone, store=store, main=box, past_opened={})
    assert 'no longer on disk' in asyncio.run(pastruns.open_run(job, 'earlier-1'))['error']


def run_and_open(monkeypatch, spec, tmp_path, label='earlier-1'):
    FakeBox.made = {}
    seen = {}

    async def drive(agent, prompt, job, store, echo, control):
        seen['agent'] = agent
        tool = next((t for t in agent.tools if getattr(t, 'name', '') == 'open_past_run'), None)
        if tool is not None:
            raw = json.dumps({'run': label})
            context = ToolContext(job, tool_name='open_past_run', tool_call_id='1', tool_arguments=raw)
            seen['reply'] = json.loads(await tool.on_invoke_tool(context, raw))
        output = SimpleNamespace(
            status='partial', model_dump=lambda: {'status': 'partial', 'summary': '', 'open_questions': []}
        )
        return SimpleNamespace(final_output=output, to_input_list=lambda: [])

    monkeypatch.setattr(engine, 'Sandbox', FakeBox)
    monkeypatch.setattr(engine, '_drive', drive)
    store = asyncio.run(engine.run_job(spec, Endpoint(api_key='unused'), tmp_path / 'runs', echo=lambda _: None))
    return store, seen


def test_a_run_offers_the_tool_only_when_there_are_earlier_runs_and_records_what_was_opened(tmp_path, monkeypatch):
    store, seen = run_and_open(monkeypatch, spec_with_past_runs(tmp_path), tmp_path)
    assert seen['reply']['run'] == 'earlier-1' and '# Earlier runs' in seen['agent'].instructions
    assert [r['label'] for r in store.record['past_runs']] == ['earlier-1', 'earlier-2']
    assert 'outputs' not in store.record['past_runs'][0] and store.record['past_runs_opened'] == ['earlier-1']
    (tmp_path / 'plain').mkdir()
    store, seen = run_and_open(monkeypatch, spec_with_skills(tmp_path / 'plain', skills=()), tmp_path / 'plain')
    assert 'reply' not in seen and 'Earlier runs' not in seen['agent'].instructions
    assert (store.record['past_runs'], store.record['past_runs_opened']) == ([], [])


def upload(client, playbook='payroll', goal='Fill the sheet.', period='2026-10'):
    return client.post(
        '/api/jobs',
        data={'period': period, 'playbook_id': playbook, 'instructions': goal},
        files=[('template', ('template.xlsx', workbook_bytes())), ('files', ('a.txt', b'EMP001'))],
    ).json()


def test_a_job_is_offered_finished_runs_of_its_own_playbook_and_keeps_that_list(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key=''))
    with TestClient(base_url=LOCAL, app=app) as client:
        store = app.state.store

        def finished(job, status='completed', files=True):
            if files:
                (store.path(job['id']) / 'outputs').mkdir(parents=True, exist_ok=True)
                (store.path(job['id']) / 'outputs' / 'out.xlsx').write_bytes(b'x')
            store.update(job['id'], status=status)

        old = [upload(client, goal=f'Run {n}', period=f'2026-0{n}') for n in range(1, 6)]
        for job in old:
            finished(job)
        finished(upload(client, playbook='ap'))  # another playbook
        finished(upload(client, goal='Half done'), status='partial')
        finished(upload(client, goal='No files'), files=False)
        job = upload(client, goal='This month')
        spec = service.job_spec(store, store.get(job['id']))
        assert [(r['label'], r['title']) for r in spec.past_runs] == [
            ('earlier-1', '2026-05 - Run 5'), ('earlier-2', '2026-04 - Run 4'), ('earlier-3', '2026-03 - Run 3'),
        ]  # fmt: skip
        assert spec.past_runs[0]['outputs'] == store.path(old[4]['id']) / 'outputs'
        assert spec.past_runs[0]['goal'] == 'Run 5'
        # The list is kept on the job: a run finished later does not shift the labels, a deleted run drops out.
        finished(upload(client, goal='Later'))
        store.delete(old[3]['id'])
        again = service.job_spec(store, store.get(job['id']))
        assert [r['label'] for r in again.past_runs] == ['earlier-1', 'earlier-3']
        # A run sent back with comments is work in progress: not offered until it is finished again.
        store.update(old[4]['id'], status='partial')
        assert [r['label'] for r in service.job_spec(store, store.get(job['id'])).past_runs] == ['earlier-3']
        store.update(old[4]['id'], status='completed')
        assert [r['label'] for r in service.job_spec(store, store.get(job['id'])).past_runs] == [
            'earlier-1',
            'earlier-3',
        ]
        # A job from before this feature, and the first run of a playbook, have none.
        first = upload(client, playbook=client.post('/api/playbooks', json={'name': 'New work'}).json()['id'])
        assert service.job_spec(store, store.get(first['id'])).past_runs == ()
    assert (
        service.MESSAGES['past_run.opened']({'title': '2026-05 - Run 5'})
        == 'Looking at an earlier run: 2026-05 - Run 5'
    )


def test_a_packet_can_name_earlier_runs_and_be_loaded_without_them(tmp_path):
    spec = spec_with_skills(tmp_path, skills=())
    (tmp_path / 'past' / 'october').mkdir(parents=True)
    (tmp_path / 'past' / 'october' / 'out.xlsx').write_bytes(b'x')
    job = {'id': 'p', 'goal': 'g', 'zip': spec.zip.name, 'deliverables': [{'name': 'o.xlsx', 'template': 't.xlsx'}],
           'past_runs': [{'title': 'October', 'finished': '2026-10-30', 'folder': 'past/october'}]}  # fmt: skip
    (tmp_path / 'job.json').write_text(json.dumps(job), encoding='utf-8')
    loaded = load_job(tmp_path)
    assert [(r['label'], r['title'], r['outputs']) for r in loaded.past_runs] == [
        ('earlier-1', 'October', tmp_path / 'past' / 'october')
    ]
    assert load_job(tmp_path, past_runs=False).past_runs == ()
