"""What the agent learns from comments: noting a lesson, keeping it only when the run ends well, and the test case
kept with each lesson."""

import json
from types import SimpleNamespace

from test_feedback import CELL, LINE, PAGE, ROUND, job_with_outputs, workbook

from app import cases, feedback, lessons
from app.engine import service

RULE = 'Pay a joiner for the days worked in the first month, counted from the signed start date.'


def delivered(store, job, comments=(CELL, LINE), round_id=ROUND):
    """A round of comments that a run has read, as the app leaves it while the agent works."""
    sent = feedback.create_round(store, job['id'], round_id, list(comments))
    service.AppControl(store, job['id']).new_messages()
    return sent['number']


def finish(store, job, basic=1960, status='complete', passed=True):
    """The agent's fix, then the end of the run as service.settle sees it."""
    workbook(store.path(job['id']) / 'outputs' / 'upload.xlsx', basic=basic)
    record = {'status': status, 'final_check': {'passed': passed, 'issues': [] if passed else ['A problem']}}
    service.SandboxEngine(store, SimpleNamespace(api_key='', base_url='', model='m')).settle(job['id'], record, False)
    return store.get(job['id'])


def names(events):
    return [e['name'] for e in events]


def test_the_agent_notes_a_lesson_only_for_an_open_comment_and_in_few_words(tmp_path):
    store, job = job_with_outputs(tmp_path)
    assert 'no open round' in lessons.note(store, job['id'], '1.1', RULE)
    delivered(store, job, [CELL, {**LINE, 'remember': True}])
    assert lessons.note(store, job['id'], '1.1', f'  {RULE}\n').startswith('Noted.')
    assert 'The open comments are: 1.1, 1.2' in lessons.note(store, job['id'], '1.9', RULE)
    assert 'The open comments are: 1.1, 1.2' in lessons.note(store, job['id'], 'the first', RULE)
    assert 'is empty' in lessons.note(store, job['id'], '1.1', '   ')
    assert 'at most 600 characters' in lessons.note(store, job['id'], '1.1', 'x' * 601)
    assert 'already asked for this comment to be remembered' in lessons.note(store, job['id'], '1.2', RULE)
    # Noting again for the same comment replaces the lesson.
    lessons.note(store, job['id'], '1.1', 'A better wording.')
    assert store.get(job['id'])['lessons'] == [
        {'round': 1, 'n': 1, 'lesson': 'A better wording.', 'status': 'noted', 'scope': 'playbook'}
    ]
    assert names(store.events(job['id'])).count('lesson.noted') == 2
    # A job with no playbook has nowhere to keep one.
    store.mutate(job['id'], lambda j: j.update(playbook=None))
    assert 'no playbook' in lessons.note(store, job['id'], '1.1', RULE)


def test_the_message_asks_for_lessons_only_when_the_job_has_a_playbook(tmp_path):
    store, job = job_with_outputs(tmp_path)
    feedback.create_round(store, job['id'], ROUND, [CELL])
    message = store.get(job['id'])['pending_messages'][0]['text']
    assert 'call remember with the comment and the rule' in message and 'most comments are just fixes' in message
    store.mutate(job['id'], lambda j: j.update(playbook=None))
    feedback.create_round(store, job['id'], 'round-0002-efgh', [CELL])
    assert 'remember' not in store.get(job['id'])['pending_messages'][1]['text']
    control = service.AppControl(store, job['id'])
    assert control.can_remember is False


def test_a_lesson_is_kept_when_the_run_ends_well_and_gets_a_test(tmp_path):
    store, job = job_with_outputs(tmp_path)
    store.playbooks.save('payroll', 'Edited since the job started')  # the playbook moved on to version 2
    delivered(store, job)
    control = service.AppControl(store, job['id'])
    assert control.can_remember and control.remember('1.1', RULE).startswith('Noted.')
    done = finish(store, job)
    assert done['status'] == 'completed'
    assert done['lessons'] == [{'round': 1, 'n': 1, 'lesson': RULE, 'status': 'saved', 'scope': 'playbook', 'version': 3, 'case': done['lessons'][0]['case']}]  # fmt: skip
    book = store.playbooks.get('payroll')
    assert (book['version'], book['text']) == (3, f'Edited since the job started\n\n{RULE}')
    newest = store.playbooks.versions('payroll')[0]
    assert (newest['source'], newest['change']) == ('learned', RULE)
    assert newest['detail'] == {'job_id': job['id'], 'round': 1, 'round_id': ROUND, 'learned': True}
    shown = feedback.rounds(store, job['id'])[0]
    assert (shown['learned_version'], shown['learned']) == (3, [{'n': 1, 'lesson': RULE, 'scope': 'playbook'}])
    assert 'lesson.saved' in names(store.events(job['id']))
    # The test kept with it: the spot, what the user accepted there, and the run's files.
    case = cases.cases(store, 'payroll')[0]
    assert case['id'] == done['lessons'][0]['case'] and case['status'] == 'active'
    assert (case['lesson'], case['version'], case['job_id'], case['round'], case['n']) == (RULE, 3, job['id'], 1, 1)
    assert (case['was'], case['expected'], case['comment']) == ('4200', '1960', CELL['text'])
    assert case['spot'] == {'path': 'outputs/upload.xlsx', 'kind': 'cell', 'sheet': 'Employees', 'cell': 'C2',
                            'where': 'sheet "Employees", cell C2'}  # fmt: skip
    assert case['expected_row'] == ['E1077', 'Raj Kumar', '1960', '=C2-462']
    assert (case['goal'], case['period'], case['replays']) == ('Prepare payroll', '2026-11', [])
    assert (cases.folder(store, 'payroll', case['id']) / 'expected' / 'upload.xlsx').is_file()
    assert 'case.captured' in names(store.playbooks.events('payroll'))
    # Undoing the lesson is an ordinary restore; settling again adds nothing twice.
    lessons.settle(store, job['id'], 'passed')
    assert store.playbooks.get('payroll')['version'] == 3 and len(cases.cases(store, 'payroll')) == 1
    assert store.playbooks.restore('payroll', 2)['text'] == 'Edited since the job started'


def test_a_lesson_is_dropped_with_its_reason_when_it_did_not_earn_its_place(tmp_path):
    # The run did not pass its checks.
    store, job = job_with_outputs(tmp_path / 'a')
    delivered(store, job)
    lessons.note(store, job['id'], '1.1', RULE)
    done = finish(store, job, passed=False, status='partial')
    assert done['lessons'][0]['status'] == 'dropped' and 'checks passing' in done['lessons'][0]['reason']
    assert store.playbooks.get('payroll')['version'] == 1 and cases.cases(store, 'payroll') == []
    assert 'learned_version' not in feedback.rounds(store, job['id'])[0]
    # The spot the comment pointed at was not changed, and the instructions already say the other one.
    store, job = job_with_outputs(tmp_path / 'b')
    store.playbooks.save('payroll', f'Rules.\n{RULE.upper()}')
    delivered(store, job, [CELL, {**CELL, 'cell': 'C3', 'text': 'Same rule here.'}, PAGE])
    lessons.note(store, job['id'], '1.1', RULE)
    lessons.note(store, job['id'], '1.2', 'Never change the pay of someone who did not join this month.')
    lessons.note(store, job['id'], '1.3', 'Totals on the summary page follow the workbook.')
    done = finish(store, job)
    kept = {x['n']: (x['status'], x.get('reason', '')) for x in done['lessons']}
    assert kept[1] == ('dropped', 'the instructions already say this')
    assert kept[2] == ('dropped', 'the commented spot did not change')
    assert kept[3] == ('dropped', 'the commented spot did not change')  # the PDF was not touched
    assert store.playbooks.get('payroll')['version'] == 2
    assert names(store.events(job['id'])).count('lesson.dropped') == 3
    # A run that only stopped keeps the lesson noted for the run that continues.
    store, job = job_with_outputs(tmp_path / 'c')
    delivered(store, job)
    lessons.note(store, job['id'], '1.1', RULE)
    paused = finish(store, job, status='paused')
    assert paused['lessons'][0]['status'] == 'noted' and paused['feedback'][0]['status'] == 'delivered'
    assert finish(store, job)['lessons'][0]['status'] == 'saved'
    # The playbook was removed while the run worked: dropped, and the run still ends as it ended.
    store, job = job_with_outputs(tmp_path / 'd')
    store.mutate(job['id'], lambda j: j['playbook'].update(id='gone'))
    delivered(store, job)
    lessons.note(store, job['id'], '1.1', RULE)
    done = finish(store, job)
    assert (done['status'], done['lessons'][0]['reason']) == ('completed', "the run's playbook no longer exists")


def test_lessons_of_one_round_make_one_version_and_a_comment_without_a_scoreable_spot_gets_no_test(tmp_path):
    store, job = job_with_outputs(tmp_path)
    delivered(store, job, [CELL, LINE, {'path': 'inputs/source.txt', 'line': 1, 'text': 'This file is last year.'}])
    lessons.note(store, job['id'], '1.1', RULE)
    lessons.note(store, job['id'], '1.2', 'State totals to two decimals in the notes.')
    lessons.note(store, job['id'], '1.3', 'Ignore source files dated before the reporting year.')
    notes = store.path(job['id']) / 'outputs' / 'notes.md'
    notes.write_text('First line\nTotal net pay is 21,640.25\nThird line', encoding='utf-8')
    done = finish(store, job)
    assert [x['status'] for x in done['lessons']] == ['saved'] * 3 and {x['version'] for x in done['lessons']} == {2}
    assert store.playbooks.get('payroll')['text'].count('\n\n') == 3
    kept = cases.cases(store, 'payroll')
    assert [(c['spot']['kind'], c['expected']) for c in kept] == [
        ('cell', '1960'),
        ('line', 'Total net pay is 21,640.25'),
    ]
    assert kept[1]['expected_row'] is None and done['lessons'][2]['case'] is None
    skipped = [e for e in store.playbooks.events('payroll') if e['name'] == 'case.skipped']
    assert len(skipped) == 1 and 'not on one cell or one line of an output' in skipped[0]['message']
    assert json.loads(lessons.describe(done))[0]['lesson'] == RULE


def test_a_playbook_holds_a_fixed_number_of_tests_and_retires_the_idle_ones_first(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, 'MAX_CASES', 3)
    store, job = job_with_outputs(tmp_path)
    made = []
    for number in range(1, 5):
        delivered(store, job, [CELL], round_id=f'round-000{number}-abcd')
        lessons.note(store, job['id'], f'{number}.1', f'Rule number {number}.')
        if number == 2:  # the oldest test has caught something before; the second never has
            cases.add_replay(store, 'payroll', made[0], {'passed': False, 'at': '2026-11-22'})
        made.append(finish(store, job, basic=1000 + number)['lessons'][-1]['case'])
    active = cases.cases(store, 'payroll')
    assert [c['lesson'] for c in active] == ['Rule number 1.', 'Rule number 3.', 'Rule number 4.']
    retired = cases.read(store, 'payroll', made[1])
    assert retired['status'] == 'retired' and 'never caught anything' in retired['retired_reason']
    assert 'case.retired' in names(store.playbooks.events('payroll'))
    # A retired test can be brought back; the limit then takes another, never the one just restored.
    assert cases.restore(store, 'payroll', made[1])['status'] == 'active'
    assert made[1] in [c['id'] for c in cases.cases(store, 'payroll')] and len(cases.cases(store, 'payroll')) == 3
    # Retired tests keep their files for a while, then go.
    assert cases.sweep(store, 'payroll', today='2026-12-01') == 0
    assert cases.sweep(store, 'payroll', today='2027-06-01') == 1
    assert len(cases.cases(store, 'payroll', None)) == 3


def test_the_agent_gets_the_remember_tool_only_when_the_caller_can_keep_a_lesson(tmp_path, monkeypatch):
    from test_skill_engine import run_with_fake_sandbox, spec_with_skills

    from app.engine import agent as engine

    said = []

    class Keeping(engine.Control):
        can_remember = True

        def remember(self, comment, lesson):
            said.append((comment, lesson))
            return 'Noted.'

    spec = spec_with_skills(tmp_path, skills=())
    _, seen, _ = run_with_fake_sandbox(monkeypatch, spec, tmp_path, control=Keeping())
    tool = next(t for t in seen['agent'].tools if getattr(t, 'name', '') == 'remember')
    assert tool.params_json_schema['required'] == ['comment', 'lesson', 'scope']
    _, seen, _ = run_with_fake_sandbox(monkeypatch, spec, tmp_path)
    assert 'remember' not in [getattr(t, 'name', '') for t in seen['agent'].tools]
    assert engine.Control().remember('1.1', 'x') == 'Nothing can be remembered in this run.'


def test_a_late_cancel_or_an_unread_instruction_decides_before_anything_is_learned(tmp_path):
    # The user cancelled while the run was finishing: the run reports complete, the cancel wins, nothing is learned.
    store, job = job_with_outputs(tmp_path / 'a')
    delivered(store, job)
    lessons.note(store, job['id'], '1.1', RULE)
    store.update(job['id'], status='cancelled')
    done = finish(store, job)
    assert (done['status'], done['lessons'][0]['status']) == ('cancelled', 'dropped')
    assert store.playbooks.get('payroll')['version'] == 1 and cases.cases(store, 'payroll') == []
    # An instruction arrived after the agent's last step: the run ends as it is and starts again by itself to read it
    # (runtime.resume_if_answered); the lesson waits for the run that reads it.
    store, job = job_with_outputs(tmp_path / 'b')
    delivered(store, job)
    lessons.note(store, job['id'], '1.1', RULE)
    store.update(job['id'], pending_messages=['Actually, pay him from the 18th.'])
    waiting = finish(store, job)
    assert (waiting['status'], waiting['lessons'][0]['status']) == ('completed', 'noted')
    assert waiting['feedback'][0]['status'] == 'done' and store.playbooks.get('payroll')['version'] == 1
    # That run reads it and ends well: now the lesson is kept, judged by its own round's report.
    service.AppControl(store, job['id']).new_messages()
    done = finish(store, job, basic=1820)
    assert (done['status'], done['lessons'][0]['status'], done['lessons'][0]['version']) == ('completed', 'saved', 2)
    assert cases.cases(store, 'payroll')[0]['expected'] == '1820'
    # And had that run ended with problems instead, the lesson would have been dropped.
    store, job = job_with_outputs(tmp_path / 'c')
    delivered(store, job)
    lessons.note(store, job['id'], '1.1', RULE)
    store.update(job['id'], pending_messages=['One more thing.'])
    finish(store, job)
    service.AppControl(store, job['id']).new_messages()
    assert finish(store, job, passed=False, status='partial')['lessons'][0]['status'] == 'dropped'
