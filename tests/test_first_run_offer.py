"""After a client's first run: what the agent found worth keeping is offered, and one click keeps it as the
client's notes, with Undo (plan Step 50)."""

from test_clients import REMOTE, spec_with_skills
from test_sandbox_engine import client_for

from app.engine.goal import goal_card
from app.engine.jobspec import JobSpec
from app.engine.report import from_answer

KEEP = ['The FINAL register wins over drafts.', 'Singapore uses the month-end FX rate.']


def test_the_agent_is_asked_for_what_to_keep_only_when_the_client_has_no_notes(tmp_path):
    spec = spec_with_skills(tmp_path, skills=())
    new = goal_card(JobSpec(**{**spec.__dict__, 'client': 'Acme', 'client_notes': ''}), REMOTE, 'row')
    known = goal_card(JobSpec(**{**spec.__dict__, 'client': 'Acme', 'client_notes': 'Amounts in MYR.'}), REMOTE, 'row')
    assert 'for_next_time' in new and 'for_next_time' not in known


def test_the_report_keeps_at_most_five_lines_to_keep():
    answer = {'headline': 'Done.', 'for_next_time': [f'Rule {n}.' for n in range(8)]}
    assert from_answer(answer)['for_next_time'] == [f'Rule {n}.' for n in range(5)]
    assert 'for_next_time' not in from_answer({'headline': 'Done.'})  # nothing offered when nothing was listed


def first_run(client, store):
    acme = store.clients.create('Acme', '')
    job = store.create('payroll', '2026-10', 'Run', '', {'id': 'payroll', 'name': 'Payroll', 'version': 1})
    store.update(job['id'], status='completed', client={'id': acme['id'], 'name': 'Acme', 'version': 1},
                 report={'headline': 'Done.', 'for_next_time': KEEP})  # fmt: skip
    return job['id'], acme['id']


def keep(client, job_id, action):
    return client.post(f'/api/jobs/{job_id}/keep-for-client', json={'action': action})


def test_saving_adds_exactly_the_listed_lines_as_a_version_and_undo_takes_them_out(tmp_path):
    with client_for(tmp_path) as client:
        store = client.app.state.store
        job_id, acme = first_run(client, store)
        saved = keep(client, job_id, 'save').json()
        notes = store.clients.get(acme)
        assert notes['text'] == '- The FINAL register wins over drafts.\n- Singapore uses the month-end FX rate.'
        assert saved['offer'] == {'saved': notes['version']} and notes['version'] == 2
        assert keep(client, job_id, 'save').json()['offer'] == {'saved': 2}  # a second click adds nothing
        assert store.clients.get(acme)['version'] == 2
        undone = keep(client, job_id, 'undo').json()
        assert undone['offer'] == {} and store.clients.get(acme)['text'] == ''  # the offer is back, the notes as before


def test_not_now_puts_the_offer_away_and_a_run_without_a_client_has_nothing_to_keep(tmp_path):
    with client_for(tmp_path) as client:
        store = client.app.state.store
        job_id, acme = first_run(client, store)
        assert keep(client, job_id, 'dismiss').json()['offer'] == {'dismissed': True}
        assert store.clients.get(acme)['version'] == 1  # nothing written
        loose = store.create('payroll', '2026-10', 'Run', '', {'id': 'payroll', 'name': 'Payroll', 'version': 1})
        assert keep(client, loose['id'], 'save').status_code == 400
        assert keep(client, job_id, 'undo').status_code == 400  # nothing saved to undo


def test_lines_are_kept_only_onto_the_notes_the_run_was_made_with(tmp_path):
    with client_for(tmp_path) as client:
        store = client.app.state.store
        job_id, acme = first_run(client, store)
        store.clients.save(acme, 'Amounts in MYR.', 'edited')  # a person wrote notes after the run began
        refused = keep(client, job_id, 'save')
        assert refused.status_code == 409 and store.clients.get(acme)['text'] == 'Amounts in MYR.'
        assert store.get(job_id)['offer'] == {}  # nothing is left claimed by the refused save


def test_a_run_continued_after_saving_does_not_offer_the_lines_again():
    from app.engine.service import without_offer

    report = {'headline': 'Done.', 'for_next_time': KEEP}
    assert without_offer(report, {'saved': 2}) == {'headline': 'Done.'}
    assert without_offer(report, {'dismissed': True}) == {'headline': 'Done.'}
    assert without_offer(report, {}) == report
