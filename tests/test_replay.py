"""Replaying a playbook's kept tests: the hidden jobs, the scoring, the check record, the list reviewer, and the
queue hooks that start both when instructions change."""

import asyncio
import shutil
import time
from types import SimpleNamespace

from test_feedback import CELL, LINE, job_with_outputs, workbook
from test_lessons import RULE, delivered, finish
from test_sandbox_engine import FakeRun, client_for, finished

from app import cases, curator, lessons, replay
from app.engine import service
from app.store import Store

from helpers import create_job


def learned_case(tmp_path, comments=(CELL,)):
    store, job = job_with_outputs(tmp_path)
    delivered(store, job, comments)
    lessons.note(store, job['id'], '1.1', RULE)
    finish(store, job)
    return store, cases.cases(store, 'payroll')[0]


def names(events):
    return [e['name'] for e in events]


def test_a_spot_is_right_when_it_holds_what_the_user_accepted_even_in_another_row(tmp_path):
    store, case = learned_case(tmp_path)
    root = tmp_path / 'again'
    (root / 'outputs').mkdir(parents=True)
    workbook(root / 'outputs' / 'upload.xlsx', basic=1960)
    assert replay.score(case, root) == {'passed': True, 'got': '1960'}
    workbook(root / 'outputs' / 'upload.xlsx', basic=4200)  # the old mistake is back
    assert replay.score(case, root) == {'passed': False, 'got': '4200'}
    # The same people in another order: the accepted row stands elsewhere in the sheet.
    from openpyxl import load_workbook

    book = load_workbook(root / 'outputs' / 'upload.xlsx')
    sheet = book['Employees']
    sheet.delete_rows(2, 2)
    sheet.append(['E1042', 'Nur Aisyah', 4600, 4254])
    sheet.append(['E1077', 'Raj Kumar', 1960, '=C2-462'])
    book.save(root / 'outputs' / 'upload.xlsx')
    assert replay.score(case, root) == {'passed': True, 'got': '1960', 'note': 'in another row'}
    # The file is not there at all.
    (root / 'outputs' / 'upload.xlsx').unlink()
    missing = replay.score(case, root)
    assert (missing['passed'], missing['got'], missing['note']) == (False, None, 'the file is not there')


def test_a_value_that_belongs_to_another_record_does_not_pass(tmp_path):
    from openpyxl import load_workbook

    store, case = learned_case(tmp_path)
    assert case['row_key'] == {'column': 1, 'value': 'E1077'}  # the correction was about this employee
    root = tmp_path / 'again'
    (root / 'outputs').mkdir(parents=True)
    workbook(root / 'outputs' / 'upload.xlsx', basic=1960)
    book = load_workbook(root / 'outputs' / 'upload.xlsx')
    sheet = book['Employees']
    # The rows swapped places: someone else now sits in C2 with 1960, and Raj still has the old mistake.
    sheet.delete_rows(2, 2)
    sheet.append(['E1042', 'Nur Aisyah', 1960, 4254])
    sheet.append(['E1077', 'Raj Kumar', 4200, '=C3-462'])
    book.save(root / 'outputs' / 'upload.xlsx')
    assert replay.score(case, root) == {'passed': False, 'got': '4200'}
    # His row elsewhere with the accepted value passes, whatever its other cells (a formula moves with its row).
    sheet['C3'] = 1960
    book.save(root / 'outputs' / 'upload.xlsx')
    assert replay.score(case, root) == {'passed': True, 'got': '1960', 'note': 'in another row'}
    # His record is gone from the sheet. The old cell now belongs to someone else, so holding the accepted value
    # there is not a pass: the correction could not be checked.
    sheet['A3'] = 'E9999'
    sheet['C2'] = 1960
    book.save(root / 'outputs' / 'upload.xlsx')
    lost = replay.score(case, root)
    assert (lost['passed'], lost['got'], lost['note']) == (None, None, 'its record was not found')
    sheet['C2'] = 4200
    # Two rows now carry his ID: which is the corrected record cannot be told, so it is neither a pass nor a fail.
    sheet['A3'] = 'E1077'
    sheet['A2'] = 'E1077'
    book.save(root / 'outputs' / 'upload.xlsx')
    unsure = replay.score(case, root)
    assert (unsure['passed'], unsure['note']) == (None, '2 rows now carry the key of its record')
    sheet['A2'] = 'E1042'
    sheet['A3'] = 'E9999'
    book.save(root / 'outputs' / 'upload.xlsx')
    # A correction to the key cell itself has no other key: the same cell decides.
    keyless = {**case, 'row_key': None, 'spot': {**case['spot'], 'cell': 'A2'}, 'expected': 'E1042'}
    assert replay.score(keyless, root) == {'passed': True, 'got': 'E1042'}


def test_a_line_is_right_when_the_accepted_line_stands_anywhere_in_the_file(tmp_path):
    store, job = job_with_outputs(tmp_path)
    delivered(store, job, [LINE])
    lessons.note(store, job['id'], '1.1', 'State the total to two decimals.')
    (store.path(job['id']) / 'outputs' / 'notes.md').write_text(
        'First\nTotal net pay is 21,640.25\nThird', encoding='utf-8'
    )
    finish(store, job)
    case = cases.cases(store, 'payroll')[0]
    root = tmp_path / 'again'
    (root / 'outputs').mkdir(parents=True)
    (root / 'outputs' / 'notes.md').write_text('A new first line\nFirst\nTotal net pay is 21,640.25', encoding='utf-8')
    assert replay.score(case, root) == {'passed': True, 'got': 'Total net pay is 21,640.25', 'note': 'on another line'}
    (root / 'outputs' / 'notes.md').write_text('First\nTotal net pay is 21640\nThird', encoding='utf-8')
    assert replay.score(case, root)['passed'] is False
    # A shorter file than the one the comment was made on: the accepted line is still looked for.
    (root / 'outputs' / 'notes.md').write_text('Total net pay is 21,640.25', encoding='utf-8')
    assert replay.score(case, root) == {'passed': True, 'got': 'Total net pay is 21,640.25', 'note': 'on another line'}
    (root / 'outputs' / 'notes.md').write_text('Nothing', encoding='utf-8')
    assert replay.score(case, root) == {'passed': False, 'got': None}


def test_the_key_of_a_record_is_a_value_no_other_row_shares(tmp_path):
    from openpyxl import Workbook
    from test_feedback import job_with_outputs

    store, job = job_with_outputs(tmp_path)
    book = Workbook()
    sheet = book.active
    sheet.title = 'Employees'
    for row in (['Dept', 'Period', 'ID', 'Pay'], ['Sales', '2026-11', 'E1', 100], ['Sales', '2026-11', 'E2', 200]):
        sheet.append(row)
    path = store.path(job['id']) / 'outputs' / 'upload.xlsx'
    book.save(path)
    delivered(store, job, [{**CELL, 'cell': 'D3'}])
    lessons.note(store, job['id'], '1.1', RULE)
    sheet['D3'] = 250
    book.save(path)
    record = {'status': 'complete', 'final_check': {'passed': True, 'issues': []}}
    service.SandboxEngine(store, SimpleNamespace(api_key='', base_url='', model='m')).settle(job['id'], record, False)
    case = cases.cases(store, 'payroll')[0]
    assert case['row_key'] == {'column': 3, 'value': 'E2'}  # not the department or the period both rows share
    # Another Sales row holding 250 does not pass for E2.
    root = tmp_path / 'again'
    (root / 'outputs').mkdir(parents=True)
    sheet['D2'], sheet['D3'] = 250, 200
    book.save(root / 'outputs' / 'upload.xlsx')
    assert replay.score(case, root) == {'passed': False, 'got': '200'}
    # A row with nothing of its own has no key.
    sheet['C2'] = sheet['C3'] = 'E1'
    sheet['D2'] = sheet['D3'] = 5
    book.save(path)
    assert (
        cases.capture(store, store.get(job['id']), {'number': 9}, {**case['spot'], 'n': 1, 'text': 't'}, RULE, 2)[
            'row_key'
        ]
        is None
    )


def end_replay(store, job_id, basic=None, status='completed'):
    """A hidden replay job ends, having written (or not) its output."""
    if basic is not None:
        workbook(store.path(job_id) / 'outputs' / 'upload.xlsx', basic=basic)
    store.update(job_id, status=status, usage={'input_tokens': 900, 'output_tokens': 100})
    return replay.finished(store, store.get(job_id))


def test_a_check_replays_every_test_in_a_hidden_job_and_keeps_the_results(tmp_path):
    store, case = learned_case(tmp_path)
    assert replay.start(Store(tmp_path / 'empty'), 'payroll', 'asked') is None  # nothing to replay
    check = replay.start(store, 'payroll', 'asked')
    assert (check['status'], check['version'], check['candidate'], len(check['cases'])) == ('running', 2, None, 1)
    job = store.get(check['cases'][0]['job_id'])
    assert job['replay'] == {'check': check['id'], 'case': case['id'], 'playbook': 'payroll'}
    assert (job['instructions'], job['period'], job['past_runs']) == ('Prepare payroll', '2026-11', [])
    assert job['context'] == store.playbooks.get('payroll')['text'] and RULE in job['context']
    assert job['playbook'] == {'id': 'payroll', 'name': 'Payroll', 'version': 2}
    # Hidden: not in the run list, and never offered to another run as an earlier run.
    assert [j['id'] for j in store.summaries()] == [case['job_id']]
    store.update(job['id'], status='completed')
    (store.path(job['id']) / 'outputs').mkdir(parents=True, exist_ok=True)
    (store.path(job['id']) / 'outputs' / 'x.txt').write_text('x')
    other = store.create('payroll', '2026-12', 'Next', 'Rules', {'id': 'payroll', 'name': 'Payroll', 'version': 2})
    assert [r['id'] for r in service.choose_past_runs(store, other)] == [case['job_id']]
    store.update(job['id'], status='queued')
    assert replay.start(store, 'payroll', 'asked') is None  # one check at a time
    done = end_replay(store, job['id'], basic=1960)
    assert (done['status'], done['passed'], done['passed_count'], done['total']) == ('done', True, 1, 1)
    assert done['cases'][0]['status'] == 'passed' and done['cases'][0]['tokens'] == 1000
    last = cases.read(store, 'payroll', case['id'])['replays'][-1]
    assert (last['passed'], last['got'], last['version'], last['check']) == (True, '1960', 2, check['id'])
    assert names(store.playbooks.events('payroll'))[-3:] == ['check.started', 'check.case', 'check.finished']
    assert replay.finished(store, store.get(job['id'])) is None  # scored once
    shown = replay.summary(store, 'payroll')
    assert (shown['right'], shown['checked'], shown['limit'], shown['running']) == (1, 1, 20, False)
    assert shown['cases'][0]['last']['passed'] is True and shown['cases'][0]['file'] == 'upload.xlsx'
    assert shown['last_check']['passed_count'] == 1 and shown['retired'] == []
    # The next check removes the hidden jobs of the last one, and a wrong answer is caught.
    again = replay.start(store, 'payroll', 'edited')
    assert job['id'] not in [j['id'] for j in store.jobs()] and not store.path(job['id']).exists()
    failed = end_replay(store, again['cases'][0]['job_id'], basic=4200)
    assert (failed['passed'], failed['cases'][0]['status'], failed['cases'][0]['got']) == (False, 'failed', '4200')
    assert replay.summary(store, 'payroll')['right'] == 0
    assert store.playbooks.events('payroll')[-1]['level'] == 'WARN'


def test_a_candidate_is_checked_without_touching_the_saved_score_and_a_broken_run_is_not_a_pass(tmp_path):
    store, case = learned_case(tmp_path)
    check = replay.start(store, 'payroll', 'tidy', text='A tidier wording.', based_on=2, extra={'tidy': 'abc'})
    job = store.get(check['cases'][0]['job_id'])
    assert (job['context'], job['playbook']['version'], check['version'], check['tidy']) == (
        'A tidier wording.',
        None,
        None,
        'abc',
    )
    done = end_replay(store, job['id'], status='failed')
    assert (done['passed'], done['cases'][0]['status'], done['cases'][0]['note']) == (
        False,
        'error',
        'the run ended as failed',
    )
    assert cases.read(store, 'payroll', case['id'])['replays'] == []
    assert replay.summary(store, 'payroll')['last_check'] is None
    # A replay the app's restart cut off counts as not checked.
    check = replay.start(store, 'payroll', 'asked')
    replay.abandon(store, store.get(check['cases'][0]['job_id']))
    assert replay.read(store, 'payroll', check['id'])['cases'][0]['status'] == 'error'


def fake_curator(monkeypatch, decide):
    calls = []

    async def call(endpoint, effort, text, listed):
        calls.append(SimpleNamespace(text=text, listed=listed))
        answer = decide(text, listed)
        if isinstance(answer, Exception):
            raise answer
        return curator.Curation(decisions=[curator.Decision(test=t, keep=k, reason=r) for t, k, r in answer]), 70

    monkeypatch.setattr(curator, '_call', call)
    return calls


def test_the_list_reviewer_retires_what_the_instructions_no_longer_need_and_nothing_on_failure(tmp_path, monkeypatch):
    store, first = learned_case(tmp_path)
    job = store.get(first['job_id'])
    delivered(store, job, [CELL], round_id='round-0002-abcd')
    lessons.note(store, job['id'], '2.1', 'Round pay to two decimals.')
    finish(store, job, basic=1961)
    endpoint = SimpleNamespace(model='m')
    calls = fake_curator(monkeypatch, lambda *_: RuntimeError('model down'))
    assert asyncio.run(curator.curate(store, endpoint, 'payroll')) == {'retired': [], 'kept': 2, 'failed': True}
    assert store.playbooks.events('payroll')[-1]['name'] == 'curator.failed'
    calls = fake_curator(monkeypatch, lambda *_: [('T1', False, 'The rule was   removed.'), ('T2', True, 'Still there.'),
                                                  ('T9', False, 'No such test.'), ('T1', False, 'Twice.')])  # fmt: skip
    result = asyncio.run(curator.curate(store, endpoint, 'payroll'))
    assert result == {'retired': [first['id']], 'kept': 1}
    assert f'- T1: {RULE}\n- T2: Round pay to two decimals.' == calls[0].listed and RULE in calls[0].text
    gone = cases.read(store, 'payroll', first['id'])
    assert (gone['status'], gone['retired_reason'], gone['retired_by']) == (
        'retired',
        'The rule was removed.',
        'curator',
    )
    assert names(store.playbooks.events('payroll'))[-2:] == ['case.retired', 'curator.ran']
    assert asyncio.run(curator.curate(Store(tmp_path / 'none'), endpoint, 'payroll')) == {'retired': [], 'kept': 0}
    # A review that queued behind one which used up the day makes no call of its own.
    calls = fake_curator(monkeypatch, lambda *_: [])
    monkeypatch.setattr(store, 'day_used_up', lambda: True)
    assert asyncio.run(curator.curate(store, endpoint, 'payroll')) == {'retired': [], 'kept': 1, 'skipped': True}
    assert calls == [] and store.playbooks.events('payroll')[-1]['name'] == 'check.not_started'


def test_a_long_line_and_a_word_document_are_compared_in_full_and_read_the_same_way(tmp_path):
    from docx import Document

    store, job = job_with_outputs(tmp_path)
    outputs = store.path(job['id']) / 'outputs'
    long = 'The total net pay for the month is 21,640.25' + ' and every figure agrees with the bank file' * 8

    def letter(second):
        document = Document()
        for paragraph in ('Dear team', second, 'Regards'):
            document.add_paragraph(paragraph)
        document.save(outputs / 'letter.docx')

    letter('The figures are attached.')
    delivered(store, job, [LINE, {'path': 'outputs/letter.docx', 'line': 2, 'text': 'Say that the figures agree.'}])
    lessons.note(store, job['id'], '1.1', 'State the total and that it agrees.')
    lessons.note(store, job['id'], '1.2', 'Say in the letter that the figures agree.')
    letter(long)  # the agent's correction, on both files
    (outputs / 'notes.md').write_text(f'First\n{long}\nThird', encoding='utf-8')
    finish(store, job)
    kept = cases.cases(store, 'payroll')
    assert len(long) > 300 and [c['expected'] for c in kept] == [long, long]  # not the excerpt the screen shows
    # The same work done again, unchanged, is right for both.
    for case in kept:
        assert replay.score(case, store.path(job['id'])) == {'passed': True, 'got': long}
    # A Word file that does not open is a wrong result, never an error that leaves the check open.
    (outputs / 'letter.docx').write_bytes(b'not a document')
    assert replay.score(kept[1], store.path(job['id'])) == {'passed': False, 'got': None, 'note': 'the file could not be read'}  # fmt: skip


def test_a_record_far_down_a_large_csv_is_still_found(tmp_path):
    from app import preview

    store, job = job_with_outputs(tmp_path)
    delivered(store, job, [{'path': 'outputs/list.csv', 'cell': 'B2', 'text': 'E1 is paid 12.'}])
    lessons.note(store, job['id'], '1.1', 'Pay E1 twelve.')
    (store.path(job['id']) / 'outputs' / 'list.csv').write_text('id,amount\nE1,12\n', encoding='utf-8')
    finish(store, job)
    case = cases.cases(store, 'payroll')[0]
    assert case['row_key'] == {'column': 1, 'value': 'E1'}
    # The same work done again, with E1 now after many other rows: past what the preview reads.
    filler = ''.join(f'F{n},{n}\n' for n in range(30_000))
    root = tmp_path / 'again'
    (root / 'outputs').mkdir(parents=True)
    (root / 'outputs' / 'list.csv').write_text(f'id,amount\n{filler}E1,12\n', encoding='utf-8')
    assert len(filler) > preview.MAX_CHARS
    assert replay.score(case, root) == {'passed': True, 'got': '12', 'note': 'in another row'}


def test_tests_are_saved_one_writer_at_a_time(tmp_path):
    import threading

    store, case = learned_case(tmp_path)
    seen, read = [], cases.read

    def slow_read(*args):
        found = read(*args)
        seen.append(threading.current_thread().name)
        time.sleep(0.05)  # long enough for the other writer to get in, were nothing holding it back
        return found

    cases.read = slow_read
    try:
        writers = [
            threading.Thread(target=cases.add_replay, args=(store, 'payroll', case['id'], {'passed': True})),
            threading.Thread(target=cases.retire, args=(store, 'payroll', case['id'], 'the rule is gone')),
        ]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join()
    finally:
        cases.read = read
    saved = cases.read(store, 'payroll', case['id'])
    assert saved['status'] == 'retired' and saved['replays'] == [{'passed': True}]  # neither undid the other


def test_a_change_saved_during_a_check_is_checked_when_that_check_ends(tmp_path, monkeypatch):
    fake_curator(monkeypatch, lambda *_: [('T1', True, 'Still there.')])
    gate = {'open': False}

    async def behaviour(outputs, **_):
        while not gate['open']:
            await asyncio.sleep(0.01)
        outputs.mkdir(parents=True, exist_ok=True)
        workbook(outputs / 'upload.xlsx', basic=1960)
        return finished()

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    with client_for(tmp_path) as client:
        store, _ = app_with_a_case(client)
        client.put('/api/settings', json={'values': {'replay_tests': 'every'}})
        text = store.playbooks.get('payroll')['text']
        first = store.playbooks.save('payroll', text + '\n\nA first edit.')['version']
        wait_until(lambda: len(fake.calls) == 1)  # its check is running
        second = store.playbooks.save('payroll', text + '\n\nA second edit.')['version']
        wait_until(lambda: client.app.state.runner.pending == {'payroll': 'edited'})
        gate['open'] = True
        wait_until(lambda: len(replay.checks(store, 'payroll')) == 2 and not replay.running(store, 'payroll'))
        newest, earlier = replay.checks(store, 'payroll')
        assert (earlier['version'], newest['version'], newest['passed']) == (first, second, True)
        assert client.app.state.runner.pending == {}


def wait_until(condition, timeout=15):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert condition()


def app_with_a_case(client):
    """A real job in the app whose corrected cell became a test of the Payroll playbook."""
    store = client.app.state.store
    job = create_job(client)
    workbook(store.path(job['id']) / 'outputs' / 'upload.xlsx')
    store.update(job['id'], status='completed')
    delivered(store, store.get(job['id']), [CELL])
    lessons.note(store, job['id'], '1.1', RULE)
    finish(store, store.get(job['id']))
    return store, cases.cases(store, 'payroll')[0]


def test_a_changed_playbook_is_reviewed_and_replayed_as_the_settings_page_says(tmp_path, monkeypatch):
    reviewed = fake_curator(monkeypatch, lambda *_: [('T1', True, 'Still there.')])

    async def behaviour(outputs, answerer, **_):
        # The pretend agent follows the rule only while the instructions hold it.
        spec = fake.calls[-1].spec
        assert spec.past_runs == () and await answerer.answer('Which date?', {}) == service.DEFAULT_ANSWER
        outputs.mkdir(parents=True, exist_ok=True)
        workbook(outputs / 'upload.xlsx', basic=1960 if RULE in spec.context else 4200)
        return finished()

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    with client_for(tmp_path) as client:
        store, case = app_with_a_case(client)
        url = '/api/playbooks/payroll/tests'
        assert client.get(url).json()['cases'][0]['lesson'] == RULE and not reviewed  # a new lesson needs no review
        # "large" (the default): an ordinary edit is reviewed but not replayed.
        text = store.playbooks.get('payroll')['text']
        store.playbooks.save('payroll', text + '\n\nAnother rule.')
        wait_until(lambda: len(reviewed) == 1)
        time.sleep(0.1)
        assert not fake.calls and replay.checks(store, 'payroll') == []
        # Asked for by hand: replayed whatever the setting; the rule is still there, so the test passes.
        started = client.post(url + '/check')
        assert started.status_code == 202 and started.json()['cases'] == 1
        wait_until(lambda: replay.checks(store, 'payroll')[0]['status'] == 'done')
        assert replay.checks(store, 'payroll')[0]['passed'] is True and len(fake.calls) == 1
        assert [j['id'] for j in client.get('/api/jobs').json()] == [case['job_id']]  # the replay is not listed
        # "every": an edit that drops the rule is replayed, and the test catches it.
        client.put('/api/settings', json={'values': {'replay_tests': 'every'}})
        store.playbooks.save('payroll', 'Only this rule now.')
        wait_until(
            lambda: len(replay.checks(store, 'payroll')) == 2 and replay.checks(store, 'payroll')[0]['status'] == 'done'
        )
        latest = replay.checks(store, 'payroll')[0]
        assert (latest['passed'], latest['reason'], latest['cases'][0]['got']) == (False, 'edited', '4200')
        shown = client.get(url).json()
        assert (shown['right'], shown['checked'], shown['last_check']['passed_count']) == (0, 1, 0)
        # "never": reviewed, not replayed. A retired test can be brought back from the screen.
        client.put('/api/settings', json={'values': {'replay_tests': 'never'}})
        reviewed.clear()
        fake_curator(monkeypatch, lambda *_: [('T1', False, 'The rule is gone.')])
        store.playbooks.save('payroll', 'A third wording.')
        wait_until(lambda: cases.cases(store, 'payroll') == [])
        assert len(replay.checks(store, 'payroll')) == 2
        assert client.post(url + '/check').status_code == 409
        assert client.get(url).json()['retired'][0]['reason'] == 'The rule is gone.'
        assert client.post(f'{url}/{case["id"]}/restore').json() == 'active'
        assert (
            client.post(url + '/nope/restore').status_code == 404
            and client.get('/api/playbooks/nope/tests').status_code == 404
        )
        bad = client.put('/api/settings', json={'values': {'replay_tests': 'sometimes'}})
        assert bad.status_code == 400


def test_a_review_answered_for_an_older_version_retires_nothing(tmp_path, monkeypatch):
    store, case = learned_case(tmp_path)

    def decide(text, listed):
        store.playbooks.save('payroll', text + '\n\nThe rule is back while the model thinks.')
        return [('T1', False, 'The rule is gone.')]

    fake_curator(monkeypatch, decide)
    result = asyncio.run(curator.curate(store, SimpleNamespace(model='m'), 'payroll'))
    assert result == {'retired': [], 'kept': 1, 'outdated': True}
    assert store.spent_today() == 70  # the review was paid for, so it counts towards the day all the same
    assert cases.read(store, 'payroll', case['id'])['status'] == 'active'
    assert store.playbooks.events('payroll')[-1]['name'] == 'curator.outdated'


def test_a_check_cut_off_by_a_shutdown_is_closed_when_the_app_starts(tmp_path):
    store, case = learned_case(tmp_path)
    for number in (2, 3):  # two more tests, copies of the first
        copy = {**case, 'id': f'{number}' * 32, 'created_at': f'2026-12-0{number}T00:00:00Z'}
        shutil.copytree(cases.folder(store, 'payroll', case['id']), cases.folder(store, 'payroll', copy['id']))
        cases._write(store, copy)
    check = replay.start(store, 'payroll', 'tidy', text='Candidate.', based_on=4, extra={'tidy': 'abc'})
    ended, cut_off, gone = (entry['job_id'] for entry in check['cases'])
    # The app stopped: one job had ended but was never scored, one was running, one is gone altogether.
    workbook(store.path(ended) / 'outputs' / 'upload.xlsx', basic=1960)
    store.update(ended, status='completed')
    store.update(cut_off, status='running')
    store.delete(gone)
    closed = replay.reconcile(store)
    assert [c['id'] for c in closed] == [check['id']] and closed[0]['tidy'] == 'abc'
    done = replay.read(store, 'payroll', check['id'])
    assert (done['status'], done['passed']) == ('done', False)
    assert [c['status'] for c in done['cases']] == ['passed', 'error', 'error']
    assert done['cases'][2]['note'] == 'the replay was lost when the app stopped'
    assert store.get(cut_off)['status'] == 'cancelled'
    assert replay.reconcile(store) == [] and replay.start(store, 'payroll', 'asked') is not None  # no longer blocked


def test_the_activity_tab_reads_the_latest_entries_first_and_a_job_adds_up_its_runs(tmp_path):
    with client_for(tmp_path) as client:
        store, case = app_with_a_case(client)
        log = client.get('/api/playbooks/payroll/events', params={'recent': 2}).json()
        assert [e['name'] for e in log] == ['case.captured', 'playbook.version_saved'] and log[0]['seq'] > log[1]['seq']
        assert client.get('/api/playbooks/nope/events', params={'recent': 3}).status_code == 404
        oldest_first = client.get('/api/playbooks/payroll/events').json()
        assert oldest_first[0]['seq'] < oldest_first[-1]['seq']
        # Each run of a job is kept, so its whole time and usage can be shown.
        record = {**finished(), 'seconds': 12.5}
        service.SandboxEngine(store, SimpleNamespace(api_key='', base_url='', model='m')).settle(
            case['job_id'], record, False
        )
        runs = store.get(case['job_id'])['runs']
        assert len(runs) == 2 and runs[-1] == {
            'seconds': 12.5,
            'requests': 3,
            'input_tokens': 900,
            'output_tokens': 100,
        }
