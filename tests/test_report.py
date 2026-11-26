"""A run ends with a report: a headline, what was done, decisions made and what to check before sending."""

from test_sandbox_engine import FakeRun, client_for, finished, start_sandbox_job, write_output

from app.engine import service
from app.engine.agent import JobSummary
from app.engine.report import as_text, from_answer

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
        assert done['summary'] == ANSWER['headline'] + " Check before sending: Alex Example's start date: the file gives 1 October."  # fmt: skip
        text = client.get(f'/api/jobs/{job["id"]}/artifacts/processing_summary.txt').text
        assert text == (
            'The October payroll is filled for 1 employee.\n\n'
            'What was done:\n- Filled Employees from employees.txt.\n\n'
            'Decisions made:\n- Used the October rate, the one the memo signs off.\n\n'
            "Check before sending:\n- Alex Example's start date: the file gives 1 October.\n\n"
            'Still open:\n- Is the bonus taxable?'
        )


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
        text = client.get(f'/api/jobs/{job["id"]}/artifacts/processing_summary.txt').text
        assert text.startswith(ANSWER['headline'] + '\n\nThe run used all its steps.') and 'Decisions made:' in text


def test_an_older_run_with_one_paragraph_still_reads():
    assert from_answer({'status': 'complete', 'summary': 'All rows filled.', 'open_questions': []}) == {
        'headline': 'All rows filled.', 'done': [], 'decisions': [], 'to_check': []}  # fmt: skip
    assert from_answer(None) is None and from_answer({'status': 'partial', 'summary': ''}) is None
    assert as_text(None, ['Which rate?']) == 'Still open:\n- Which rate?'


def test_the_agent_is_asked_for_each_part():
    fields = JobSummary.model_json_schema()['properties']
    assert {'headline', 'done', 'decisions', 'to_check', 'open_questions'} <= set(fields)
    assert 'headline' in JobSummary.model_json_schema()['required']
