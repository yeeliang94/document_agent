"""The automatic tidy: when it is due, and that it is saved only if the kept tests still pass."""

import time

from openpyxl import load_workbook
from test_feedback import CELL, workbook
from test_lessons import RULE, delivered, finish
from test_replay import app_with_a_case, fake_curator, learned_case, names, wait_until
from test_sandbox_engine import FakeRun, client_for, finished
from test_tuning import fake_model, proposal

from app import cases, lessons, replay, tidy, tuning
from app.engine import service

TIDIED = f'All rules in one place. {RULE}'


def more_lessons(store, job_id, count, start=2):
    for number in range(start, start + count):
        delivered(store, store.get(job_id), [CELL], round_id=f'round-00{number:02d}-abcd')
        lessons.note(store, job_id, f'{number}.1', f'Lesson number {number}.')
        finish(store, store.get(job_id), basic=1000 + number)


def test_a_tidy_is_due_only_after_enough_lessons_and_never_without_tests(tmp_path, monkeypatch):
    store, case = learned_case(tmp_path)
    assert tidy.due(store, 'payroll') is False  # one lesson so far
    more_lessons(store, case['job_id'], 3)
    assert tidy.due(store, 'payroll') is False
    more_lessons(store, case['job_id'], 1, start=5)
    assert tidy.due(store, 'payroll') is True  # five lessons
    # Long instructions bring it forward; the Settings page can switch it off; no tests, no tidy.
    monkeypatch.setattr(tidy, 'TIDY_AFTER', 99)
    assert tidy.due(store, 'payroll') is False
    monkeypatch.setattr(tidy, 'TIDY_CHARS', 10)
    assert tidy.due(store, 'payroll') is True
    store.save_settings({'replay_tests': 'never'})
    assert tidy.due(store, 'payroll') is False
    store.save_settings({'replay_tests': 'large'})
    for kept in cases.cases(store, 'payroll'):
        cases.retire(store, 'payroll', kept['id'], 'for the test')
    assert tidy.due(store, 'payroll') is False
    assert tidy.due(store, 'ap') is False


def run_app(tmp_path, monkeypatch, follows):
    """The app with a pretend engine whose agent gets the corrected cell right only when follows(context) says so."""

    async def behaviour(outputs, **_):
        outputs.mkdir(parents=True, exist_ok=True)
        workbook(outputs / 'upload.xlsx', basic=1960 if follows(fake.calls[-1].spec.context) else 4200)
        book = load_workbook(outputs / 'upload.xlsx')
        book['Employees']['C3'] = 4700  # what the second lesson's test expects
        book.save(outputs / 'upload.xlsx')
        return finished()

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    monkeypatch.setattr(tidy, 'TIDY_AFTER', 2)
    fake_curator(monkeypatch, lambda *_: [])
    return fake


def second_lesson(store, case):
    """One more learned lesson on the same job, which makes a tidy due (TIDY_AFTER is 2 in these tests)."""
    delivered(store, store.get(case['job_id']), [{**CELL, 'cell': 'C3'}], round_id='round-0002-abcd')
    lessons.note(store, case['job_id'], '2.1', 'Check the second person too.')
    book = store.path(case['job_id']) / 'outputs' / 'upload.xlsx'
    wb = load_workbook(book)
    wb['Employees']['C3'] = 4700
    wb.save(book)
    record = {'status': 'complete', 'final_check': {'passed': True, 'issues': []}}
    client_settle(store, case['job_id'], record)


def client_settle(store, job_id, record):
    from types import SimpleNamespace

    service.SandboxEngine(store, SimpleNamespace(api_key='', base_url='', model='m')).settle(job_id, record, False)


def test_a_tidy_that_keeps_every_past_correction_right_is_saved_by_itself(tmp_path, monkeypatch):
    run_app(tmp_path, monkeypatch, follows=lambda text: RULE in text)
    asked = fake_model(monkeypatch, [proposal(text=TIDIED, summary='Merged two rules.')])
    with client_for(tmp_path) as client:
        store, case = app_with_a_case(client)
        time.sleep(0.1)
        assert not asked  # one lesson: not due yet
        second_lesson(store, case)
        wait_until(lambda: 'tidy.applied' in names(store.playbooks.events('payroll')))
        book = store.playbooks.get('payroll')
        newest = store.playbooks.versions('payroll')[0]
        assert (book['text'], newest['change'], book['version']) == (TIDIED, 'Merged two rules.', 4)
        assert asked[0].ask == tidy.ASK and newest['detail']['check'] and newest['detail']['proposal_id']
        record = tuning.read(store, 'payroll', newest['detail']['proposal_id'])
        assert (record['status'], record['tidy'], record['version']) == ('accepted', True, 4)
        events = names(store.playbooks.events('payroll'))
        assert events.index('tidy.started') < events.index('check.started') < events.index('tidy.applied')
        check = replay.checks(store, 'payroll')[0]
        assert (check['reason'], check['passed'], check['total']) == ('tidy', True, 2)
        # The tidy was saved, so what its check found now speaks for the saved instructions: the score shows it.
        assert (check['candidate'], check['version']) == (None, 4)
        assert all([r['version'] for r in c['replays']] == [4] for c in cases.cases(store, 'payroll'))
        shown = replay.summary(store, 'payroll')
        assert (shown['right'], shown['checked'], shown['last_check']['version']) == (2, 2, 4)
        # A tidy is not due again straight away.
        assert tidy.due(store, 'payroll') is False
        # It can be undone like any version.
        assert client.post('/api/playbooks/payroll/restore', json={'version': 3, 'based_on': 4}).status_code == 200


def test_a_tidy_that_breaks_a_past_correction_is_dropped(tmp_path, monkeypatch):
    run_app(tmp_path, monkeypatch, follows=lambda text: RULE in text)
    fake_model(monkeypatch, [proposal(text='Shorter, and the rule got lost.', summary='Shortened.')])
    with client_for(tmp_path) as client:
        store, case = app_with_a_case(client)
        second_lesson(store, case)
        wait_until(lambda: 'tidy.dropped' in names(store.playbooks.events('payroll')))
        assert store.playbooks.get('payroll')['version'] == 3 and RULE in store.playbooks.get('payroll')['text']
        dropped = [e for e in store.playbooks.events('payroll') if e['name'] == 'tidy.dropped'][0]
        assert dropped['level'] == 'WARN' and len(dropped['failed']) >= 1
        assert tuning.read(store, 'payroll', dropped['proposal_id'])['status'] == 'discarded'
        assert tidy.due(store, 'payroll') is False  # not tried again until more lessons arrive


def test_a_tidy_is_dropped_when_the_instructions_changed_meanwhile_or_nothing_needed_tidying(tmp_path, monkeypatch):
    fake = run_app(tmp_path, monkeypatch, follows=lambda text: True)
    fake_model(monkeypatch, [proposal(text=TIDIED, summary='Merged.')])
    with client_for(tmp_path) as client:
        store, case = app_with_a_case(client)
        original = fake.behaviour

        async def slow(**kwargs):  # the user edits while the first replay runs
            if store.playbooks.get('payroll')['version'] == 3:
                store.playbooks.save('payroll', store.playbooks.get('payroll')['text'] + '\n\nAdded by hand.')
            return await original(**kwargs)

        fake.behaviour = slow
        second_lesson(store, case)
        wait_until(lambda: 'tidy.outdated' in names(store.playbooks.events('payroll')))
        book = store.playbooks.get('payroll')
        assert book['text'].endswith('Added by hand.') and store.playbooks.versions('payroll')[0]['source'] == 'edited'
    # A proposal that changes nothing, and one that cannot be made, replay nothing.
    for folder, reply, event in (('b', None, 'tidy.nothing'), ('c', RuntimeError('down'), 'tidy.failed')):
        fake = run_app(tmp_path / folder, monkeypatch, follows=lambda text: True)
        with client_for(tmp_path / folder) as client:
            store, case = app_with_a_case(client)
            fake_model(monkeypatch, [reply or proposal(text=store.playbooks.get('payroll')['text'] + '\n\nCheck the second person too.', summary='Nothing.'), RuntimeError('down')])  # fmt: skip
            second_lesson(store, case)
            wait_until(lambda: event in names(store.playbooks.events('payroll')))  # noqa: B023
            time.sleep(0.1)
            assert replay.checks(store, 'payroll') == [] and not fake.calls
            assert store.playbooks.get('payroll')['version'] == 3
