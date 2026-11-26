"""A run ends with a report: a headline, what was done, decisions made and what to check before sending."""

from test_sandbox_engine import FakeRun, client_for, finished, start_sandbox_job, write_output

from app.engine import service
from app.engine.agent import JobSummary
from app.engine.report import from_answer

from helpers import wait_for_job

ANSWER = {
    'status': 'complete',
    'headline': 'The October payroll is filled for 1 employee.',
    'done': ['Filled Employees from employees.txt.'],
    'decisions': ['Used the October rate, the one the memo signs off.'],
    'to_check': ["Alex Example's start date: the file gives 1 October."],
    'open_questions': ['Is the bonus taxable?'],
}


def test_a_run_ends_with_all_four_parts_of_the_report(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        return {**finished(), 'summary': ANSWER}

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        done = wait_for_job(client, job['id'])
        assert done['report'] == {k: v for k, v in ANSWER.items() if k not in ('status', 'open_questions')}
        assert done['stopped_because'] is None
        assert done['summary'] == ANSWER['headline'] + " Check before sending: Alex Example's start date: the file gives 1 October."  # fmt: skip
        assert 'processing_summary.txt' not in {a['name'] for a in done['artifacts']}  # the report is on the run only


def test_a_stopped_run_adds_why_it_stopped_after_the_headline(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        write_output(outputs)
        record = finished(status='stopped', passed=False, error='Reached the 60-turn limit.')
        return {**record, 'summary': {**ANSWER, 'status': 'partial'}, 'cut_short': 'turns'}

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        job = start_sandbox_job(client)
        done = wait_for_job(client, job['id'])
        assert done['status'] == 'partial'
        assert done['summary'].startswith(ANSWER['headline']) and '\nThe run used all its steps.' in done['summary']
        assert 'Still to fix: provenance.json' in done['summary']
        assert done['stopped_because'] == 'The run used all its steps. Give it more steps, or continue the run.'


def test_an_older_run_with_one_paragraph_still_reads():
    assert from_answer({'status': 'complete', 'summary': 'All rows filled.', 'open_questions': []}) == {
        'headline': 'All rows filled.', 'done': [], 'decisions': [], 'to_check': []}  # fmt: skip
    assert from_answer(None) is None and from_answer({'status': 'partial', 'summary': ''}) is None


def test_the_agent_is_asked_for_each_part():
    fields = JobSummary.model_json_schema()['properties']
    assert {'headline', 'done', 'decisions', 'to_check', 'open_questions'} <= set(fields)
    assert 'headline' in JobSummary.model_json_schema()['required']
