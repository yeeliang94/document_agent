"""How a run shows its work (plan Phase 18): the agent's plan, summaries of its thinking, the stages the work folds
into and each action opened in full. The engine's own loop is covered in prototype/sandbox_agent/tests."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents import Agent
from openai import APIError
from test_feedback_messages import CondensingRun
from test_sandbox_engine import FakeRun, client_for, finished, start_sandbox_job, wait_for_runner_idle, write_output

from app.engine import agent, service
from app.engine import plan as plans
from app.engine.agent import _log_stream, _settings
from app.engine.runstore import RunStore

from helpers import wait_for_job

WEB = Path(__file__).resolve().parents[1] / 'web'


def steps(*pairs):
    return [{'step': text, 'status': status} for text, status in pairs]


def plan_job(tmp_path, plan=()):
    return SimpleNamespace(plan=list(plan), store=RunStore(tmp_path / 'runs'))


def events(job, name):
    lines = (job.store.dir / 'events.jsonl').read_text(encoding='utf-8').splitlines()
    return [e for e in map(json.loads, lines) if e['event'] == name]


def test_the_plan_is_written_ticked_and_kept_with_the_run(tmp_path):
    job = plan_job(tmp_path)
    first = steps(('Read the close rules', 'in_progress'), ('Build one employee list', 'pending'))
    said = plans.keep(job, first)
    assert said.startswith('Plan kept (0 of 2 done)') and job.plan == first
    kept = json.loads((job.store.dir / 'plan.json').read_text(encoding='utf-8'))
    assert kept['steps'] == first
    assert events(job, 'plan.updated')[0]['first'] is True
    ticked = steps(('Read the close rules', 'done'), ('Build one employee list', 'in_progress'))
    assert plans.keep(job, ticked).startswith('Plan kept (1 of 2 done)')
    last = events(job, 'plan.updated')[-1]
    assert last['first'] is False and last['added'] == last['changed'] == last['removed'] == []
    # The same plan again changes nothing and records nothing.
    assert plans.keep(job, ticked).startswith('No change') and len(events(job, 'plan.updated')) == 2


def test_a_changed_plan_says_what_is_new_reworded_or_dropped_and_asks_why(tmp_path):
    job = plan_job(tmp_path, steps(('Read the close rules', 'done'), ('Match each employee to the bank file', 'pending'),
                                   ('Check totals against the general ledger', 'pending'), ('Email the team', 'pending')))  # fmt: skip
    new = steps(('Read the close rules', 'done'), ('Convert Malaysian pay to US dollars', 'in_progress'),
                ('Match each employee to the bank file', 'pending'),
                ('Check totals against the general ledger, entity by entity', 'pending'))  # fmt: skip
    said = plans.keep(job, new)
    assert 'without saying why' in said  # a change with no reason is kept, and the agent is told to give one
    last = events(job, 'plan.updated')[-1]
    assert last['added'] == ['Convert Malaysian pay to US dollars']
    assert last['changed'] == [{'step': 'Check totals against the general ledger, entity by entity',
                                'was': 'Check totals against the general ledger'}]  # fmt: skip
    assert last['removed'] == ['Email the team']
    assert 'why' not in plans.keep(job, steps(('Read the close rules', 'done')), why='The rest is not needed.')


def test_a_step_that_only_moved_is_neither_new_nor_dropped():
    before = steps(('Read the rules', 'done'), ('Match the bank file', 'pending'), ('Fill the template', 'pending'))
    after = steps(('Read the rules', 'done'), ('Fill the template', 'pending'), ('Match the bank file', 'pending'))
    assert plans.changes(before, after) == {'added': [], 'changed': [], 'removed': []}


@pytest.mark.parametrize(
    'sent, problem',
    [
        ([], 'no steps'),
        (steps(('Read the rules', 'pending'), ('read the  rules', 'done')), 'twice'),
        (steps(('   ', 'pending')), 'no words'),
    ],
)
def test_a_plan_that_cannot_be_kept_is_refused_with_the_reason(tmp_path, sent, problem):
    job = plan_job(tmp_path)
    said = plans.keep(job, sent)
    assert said.startswith('The plan was not kept.') and problem in said and job.plan == []


def test_the_plan_is_given_back_after_the_history_is_condensed():
    job = SimpleNamespace(plan=steps(('Read the close rules', 'done'), ('Fill the template', 'in_progress')))
    assert plans.reminder(job) == (
        ' Your plan as it stands (keep it up to date with update_plan):\n'
        '1. [done] Read the close rules\n2. [doing now] Fill the template'
    )
    assert plans.reminder(SimpleNamespace(plan=[])) == '' and plans.reminder(SimpleNamespace()) == ''


def test_the_plan_is_kept_on_the_job_and_carried_into_a_continued_run_but_not_a_fresh_start(tmp_path, monkeypatch):
    plan = steps(('Read the close rules', 'done'), ('Fill the template', 'in_progress'))

    async def first(outputs, on_event, **_):
        write_output(outputs)
        on_event('stage.changed', {'stage': 'reading'})
        on_event('plan.updated', {'steps': plan, 'why': '', 'first': True, 'added': [], 'changed': [], 'removed': []})
        return {**finished(status='stopped', passed=False, error='Reached the 60-turn limit.'), 'cut_short': 'turns'}

    async def again(**_):
        return finished()

    run = FakeRun(first)
    monkeypatch.setattr(service, 'run_job', run)
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        url = f'/api/jobs/{job["id"]}'
        done = wait_for_job(client, job['id'])
        assert done['plan'] == {'steps': plan, 'why': '', 'run': 1}
        names = {e['name']: e for e in client.get(url + '/events').json()}
        assert names['engine.plan.updated']['message'] == 'Made a plan: 1 of 2 done'
        assert names['engine.stage.changed']['stage'] == 'reading'
        assert run.calls[0].options['plan'] is None and run.calls[0].options['thinking'] == 'short'
        wait_for_runner_idle(client)
        run.behaviour = again
        assert client.post(url + '/resume').status_code == 200
        wait_for_job(client, job['id'], statuses=('completed',))
        wait_for_runner_idle(client)
        assert run.calls[1].options['plan'] == plan
        assert '1. [done] Read the close rules\n2. [doing now] Fill the template' in run.calls[1].resume
        assert client.post(url + '/start-over').status_code == 200
        wait_for_job(client, job['id'], statuses=('completed',))
        assert run.calls[2].options['plan'] is None and 'Your plan from the earlier run' not in run.calls[2].resume
        assert client.get(url).json()['plan'] is None


def test_the_thinking_setting_reaches_the_run_and_asks_for_the_summary_it_names(tmp_path, monkeypatch):
    assert plans.SUMMARY == {'off': None, 'short': 'auto', 'detailed': 'detailed'}
    assert _settings('medium', summary='detailed').reasoning.summary == 'detailed'
    assert _settings('medium').reasoning.summary is None and _settings('medium').reasoning.effort == 'medium'
    assert _settings('none', summary='auto').reasoning is None

    async def behaviour(outputs, **_):
        write_output(outputs)
        return finished()

    run = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', run)
    with client_for(tmp_path) as client:
        assert client.put('/api/settings', json={'values': {'thinking_summaries': 'loud'}}).status_code == 400
        client.put('/api/settings', json={'values': {'thinking_summaries': 'off'}})
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'])
        assert run.calls[0].options['thinking'] == 'off'


def test_a_summary_of_the_thinking_is_shown_by_its_heading(tmp_path, monkeypatch):
    item = SimpleNamespace(type='reasoning', summary=[
        SimpleNamespace(text='**Deciding which Singapore file is final**\n\nThere are two files.\n\nThe draft has one more row.'),
        SimpleNamespace(text='No heading here. Just a thought.'),
        SimpleNamespace(text='   '),
    ])  # fmt: skip
    assert plans.thought_parts(item) == [
        {
            'title': 'Deciding which Singapore file is final',
            'text': 'There are two files.\n\nThe draft has one more row.',
        },
        {'title': 'No heading here.', 'text': 'No heading here. Just a thought.'},
    ]
    assert plans.thought_parts(SimpleNamespace(type='reasoning', summary=[])) == []
    recorded = []
    store = SimpleNamespace(event=lambda name, **data: recorded.append((name, data)))
    done = SimpleNamespace(type='raw_response_event', data=SimpleNamespace(type='response.output_item.done', item=item))
    _log_stream(done, store, lambda *_: None)
    assert recorded == [('thinking', {'parts': plans.thought_parts(item)})]
    recorded.clear()  # a reply with no summary (the setting off, or a service that sends none) shows nothing
    _log_stream(SimpleNamespace(type='raw_response_event', data=SimpleNamespace(
        type='response.output_item.done', item=SimpleNamespace(type='reasoning', summary=None))), store, print)  # fmt: skip
    assert recorded == []

    async def thinks(outputs, on_event, **_):
        write_output(outputs)
        on_event('thinking', {'parts': plans.thought_parts(item)})
        return finished()

    monkeypatch.setattr(service, 'run_job', FakeRun(thinks))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'])
        shown = [e for e in client.get(f'/api/jobs/{job["id"]}/events').json() if e['name'] == 'engine.thinking']
        assert shown[0]['message'] == 'Deciding which Singapore file is final' and shown[0]['detail'] == 'short'


def test_a_request_the_service_calls_malformed_may_be_the_summary_setting():
    class Refused(Exception):
        def __init__(self, status, text):
            super().__init__(text)
            self.status_code = status

    # Gateways word the refusal in many ways, so any malformed-request refusal gets one try without the summary.
    assert plans.summary_refused(Refused(400, "Unsupported parameter: 'reasoning.summary'"))
    assert plans.summary_refused(Refused(422, 'UnsupportedParamsError: reasoning'))
    assert not plans.summary_refused(Refused(500, 'summary')) and not plans.summary_refused(Refused(401, 'summary'))


def test_an_action_opens_with_its_code_and_what_it_printed(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        return finished()

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'])
        store = RunStore(tmp_path / 'jobs' / job['id'] / 'engine', 'run-01')
        store.save_code('main', '# Read the rules\nprint(1)', [{'logs': '1 file\n2 rules'}])
        store.save_code('scout-a', '# A helper', [])
        store.save_code('main', '# Save', [])
        url = f'/api/jobs/{job["id"]}/script'
        got = client.get(url, params={'run': 1, 'name': '001-main.py'}).json()
        assert got == {'name': '001-main.py', 'code': '# Read the rules\nprint(1)', 'output': '1 file\n2 rules'}
        # An older run names no script: its place among the lead's scripts finds it, when the counts agree.
        assert client.get(url, params={'run': 1, 'index': 2, 'of': 2}).json()['name'] == '003-main.py'
        assert client.get(url, params={'run': 1, 'index': 2, 'of': 3}).status_code == 404
        assert client.get(url, params={'run': 1, 'name': '../run.json'}).status_code == 404
        assert client.get(url, params={'run': 2, 'name': '001-main.py'}).status_code == 404


def test_the_settings_page_has_the_thinking_setting_with_no_helper_sentence():
    page = (WEB / 'pages.js').read_text(encoding='utf-8')
    assert "choice('thinking_summaries', \"Show the agent's thinking\", { off: 'Off', short: 'Short', detailed: 'Detailed' })" in page


@pytest.mark.skipif(shutil.which('node') is None, reason='needs Node.js to run the screen code')
def test_a_stage_folds_to_the_plan_steps_ticked_in_it_or_to_counts():
    script = """
import { stagesOf, stageSummary } from './stages.js';
const step = (seq, stage, files = [], secs) => ({ kind: 'step', seq, at: seq * 1000, stage, step: { seq, verb: 'x', files, secs } });
const entries = [
  step(1, 'reading', ['a.xlsx']), { kind: 'tick', seq: 2, at: 2000, stage: 'reading', steps: ['Read the close rules'] },
  step(3, 'reading', ['b.csv']),
  step(5, 'filling', ['a.xlsx'], 1), step(6, 'filling', ['a.xlsx']),
  // Ticked once filling in had begun, for a step worked on while reading: it counts towards reading.
  { kind: 'tick', seq: 6, at: 6500, stage: 'reading', steps: ['Pick the final file.'] },
  step(7, 'checking'),
];
const sections = stagesOf(entries);
console.log(JSON.stringify({
  stages: sections.map((s) => [s.stage, s.id, s.from, s.to]),
  reading: stageSummary(sections[0]),
  filling: stageSummary(sections[1]),
  live: stageSummary(sections[1], 'Fill the template'),
  checking: stageSummary(sections[2]),
  empty: stagesOf([]).length,
}));
"""
    out = subprocess.run(
        ['node', '--input-type=module', '-e', script], cwd=WEB, capture_output=True, text=True, check=True
    )
    got = json.loads(out.stdout)
    # A stage's time starts when its first action started (the 1-second action at 5 began at 4).
    assert got['stages'] == [['reading', 1, 1000, 3000], ['filling', 5, 4000, 6000], ['checking', 7, 7000, 7000]]
    assert got['reading'] == 'Read the close rules. Pick the final file.'  # the ticked steps, no AI asked
    assert got['filling'] == '2 actions on 1 file'  # no step ticked in it: how much work it held
    assert got['live'] == 'Fill the template.'  # going on now: the step the agent is on
    assert got['checking'] == '1 action' and got['empty'] == 0


def lead_job(store, **more):
    return agent.Job(settings=None, store=store, spec=None, main=None, zip_path=None, files=[], scout_model='',
                     effort='low', max_scouts=0, answerer=None, **more)  # fmt: skip


QUIET_CONTROL = SimpleNamespace(stop_requested=lambda: None, new_messages=lambda: [], more_turns=lambda: 0)


def test_the_plan_comes_back_with_the_note_that_the_history_was_condensed(tmp_path, monkeypatch):
    runs, prompts = [CondensingRun(True), CondensingRun(False)], []

    def run_streamed(agent_, prompt, **_):
        prompts.append(prompt)
        return runs.pop(0)

    monkeypatch.setattr(agent.Runner, 'run_streamed', run_streamed)
    store = RunStore(tmp_path)
    job = lead_job(store, plan=steps(('Read the close rules', 'done'), ('Fill the template', 'in_progress')))
    asyncio.run(agent._drive(None, 'go', job, store, lambda *_: None, QUIET_CONTROL))
    told = prompts[1][-1]['content']
    assert told.startswith(agent.CONDENSED_NOTE)
    assert told.endswith('1. [done] Read the close rules\n2. [doing now] Fill the template')


class Refusing(CondensingRun):
    new_items = []  # refused before any reply

    async def stream_events(self):
        error = APIError("Unsupported parameter: 'reasoning.summary'", None, body=None)
        error.status_code = 400
        raise error
        yield


def test_a_service_that_refuses_the_summary_setting_is_asked_again_without_it(tmp_path, monkeypatch):
    runs, prompts, waits = [Refusing(False), CondensingRun(False)], [], []

    def run_streamed(agent_, prompt, **_):
        prompts.append((prompt, agent_.model_settings.reasoning.summary))
        return runs.pop(0)

    real_sleep = asyncio.sleep

    async def sleep(seconds):
        if seconds != agent.INTERRUPT_POLL_SECONDS:  # the Send now watcher looking in is not a wait before a retry
            waits.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(agent.Runner, 'run_streamed', run_streamed)
    monkeypatch.setattr(agent.asyncio, 'sleep', sleep)
    store = RunStore(tmp_path)
    lead = Agent(name='lead', model_settings=_settings('low', summary='auto'))
    result = asyncio.run(agent._drive(lead, 'go', lead_job(store), store, lambda *_: None, QUIET_CONTROL))
    assert result.final_output == 'done'
    assert prompts == [('go', 'auto'), ('go', None)]  # the same request again, at once, without the summary
    assert waits == [] and lead.model_settings.reasoning.effort == 'low'
    lines = map(json.loads, (store.dir / 'events.jsonl').read_text(encoding='utf-8').splitlines())
    assert [e['event'] for e in lines if e['event'] == 'thinking.refused'] == ['thinking.refused']
    # Asked without the summary, the same refusal is about something else: it ends the run as any refusal does.
    runs[:] = [Refusing(False)]
    with pytest.raises(APIError):
        asyncio.run(agent._drive(lead, 'go', lead_job(store), store, lambda *_: None, QUIET_CONTROL))


@pytest.mark.skipif(shutil.which('node') is None, reason='needs Node.js to run the screen code')
def test_a_plan_step_counts_towards_the_stage_of_the_first_action_after_the_agent_started_it():
    # As in a live run: the agent says it is on "Fill the template" while the stage still says reading, and the stage
    # moves to filling only with its first write. The step belongs to filling, not reading.
    script = """
import { stepStages } from './stages.js';
const worked = stepStages();
worked.on(['Reconcile the records']); worked.acted('reading');
worked.on(['Fill the template']);  // said while the stage is still reading
worked.acted('filling');  // the first action after: a write, so the stage is filling
worked.on(['Fill the template']);  // said again: it stays with the stage it was first worked in
worked.acted('checking');
worked.on(['Save and review']);  // ticked before any action since: the stage it was ticked in
const save = worked.done('Save and review', 'checking');
worked.acted('later');  // and a later action no longer moves it
console.log(JSON.stringify([
  worked.done('Reconcile the records', 'x'), worked.done('Fill the template', 'x'),
  save, worked.done('Save and review', 'x'), worked.done('Never started', 'filling'),
]));
"""
    out = subprocess.run(
        ['node', '--input-type=module', '-e', script], cwd=WEB, capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout) == ['reading', 'filling', 'checking', 'checking', 'filling']
