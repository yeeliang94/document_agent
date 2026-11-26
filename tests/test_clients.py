"""Clients: notes per client with versions and Undo, followed by a run on top of its playbook, lessons kept for a
client or a playbook, the same client's earlier runs first, and the client suggested from folder and file names."""

from test_feedback import CELL, LINE, job_with_outputs
from test_lessons import RULE, delivered, finish
from test_sandbox_engine import FakeRun, client_for, finished, write_output
from test_skill_engine import REMOTE, spec_with_skills

from app import clients, feedback, lessons
from app.engine import service
from app.engine.goal import goal_card
from app.engine.jobspec import JobSpec
from app.store import Store

from helpers import wait_for_job, workbook_bytes

TEMPLATE = ('template', ('template.xlsx', workbook_bytes(), 'application/octet-stream'))
SOURCE = ('files', ('HR/employees.txt', b'EMP001, Alex Example, 4200', 'text/plain'))


def test_a_client_keeps_its_notes_as_versions_and_an_earlier_one_can_be_restored(tmp_path):
    with client_for(tmp_path) as client:
        made = client.post('/api/clients', json={'name': '  Acme  ', 'notes': 'Dates as dd/mm/yyyy.'})
        assert made.status_code == 201 and (made.json()['name'], made.json()['version']) == ('Acme', 1)
        acme = made.json()['id']
        assert client.post('/api/clients', json={'name': 'ACME'}).json()['detail'] == (
            'A client with this name already exists'
        )

        def save(text, based_on):
            return client.post(f'/api/clients/{acme}/versions', json={'text': text, 'based_on': based_on})

        assert save('Dates as yyyy-mm-dd.', 1).json()['version'] == 2
        assert save('Dates as yyyy-mm-dd. Amounts in MYR.', 2).json()['version'] == 3
        stale = save('An edit from an old tab.', 2)
        assert stale.status_code == 409 and stale.json()['detail']['version'] == 3
        undone = client.post(f'/api/clients/{acme}/restore', json={'version': 1, 'based_on': 3}).json()
        assert (undone['version'], undone['text']) == (4, 'Dates as dd/mm/yyyy.')
        history = client.get(f'/api/clients/{acme}/versions').json()
        assert [(v['version'], v['source']) for v in history] == [
            (4, 'restored'),
            (3, 'edited'),
            (2, 'edited'),
            (1, 'created'),
        ]
        assert client.patch(f'/api/clients/{acme}', json={'name': 'Acme Sdn Bhd'}).json()['name'] == 'Acme Sdn Bhd'
        assert [c['name'] for c in client.get('/api/clients').json()] == ['Acme Sdn Bhd']
        assert [e['name'] for e in client.get(f'/api/clients/{acme}/events').json()][0] == 'client.renamed'
        assert client.get('/api/clients/nobody').status_code == 404


def test_undo_from_a_run_brings_back_the_notes_before_and_is_refused_once_they_moved_on(tmp_path):
    # The run's Undo beside a remembered comment or a lesson is a restore of the version before, based on the
    # version that added it (web/comments.js forget).
    with client_for(tmp_path) as client:
        acme = client.post('/api/clients', json={'name': 'Acme', 'notes': 'Amounts in MYR.'}).json()['id']
        client.post(f'/api/clients/{acme}/versions', json={'text': 'Amounts in MYR.\n\nTotals in bold.', 'based_on': 1})
        undone = client.post(f'/api/clients/{acme}/restore', json={'version': 1, 'based_on': 2})
        assert undone.status_code == 200 and undone.json()['text'] == 'Amounts in MYR.'
        client.post(f'/api/clients/{acme}/versions', json={'text': 'Later work.', 'based_on': 3})
        late = client.post(f'/api/clients/{acme}/restore', json={'version': 1, 'based_on': 2})
        assert late.status_code == 409 and late.json()['detail']['version'] == 4
        assert client.get(f'/api/clients/{acme}').json()['text'] == 'Later work.'


def new_run(client, client_id=''):
    data = {'playbook_id': 'payroll', 'instructions': 'Fill the sheet.', 'client_id': client_id}
    made = client.post('/api/jobs', data=data, files=[TEMPLATE, SOURCE])
    assert made.status_code == 201, made.text
    return made.json()


def test_a_run_follows_the_notes_its_client_had_when_it_was_made(tmp_path, monkeypatch):
    async def done(outputs, **_):
        write_output(outputs)
        return finished()

    run = FakeRun(done)
    monkeypatch.setattr(service, 'run_job', run)
    with client_for(tmp_path) as client:
        acme = client.post('/api/clients', json={'name': 'Acme', 'notes': 'Name staff as SURNAME, Given.'}).json()
        job = new_run(client, acme['id'])
        assert job['client'] == {'id': acme['id'], 'name': 'Acme', 'version': 1}
        client.post(f'/api/clients/{acme["id"]}/versions', json={'text': 'Changed later.', 'based_on': 1})
        client.post(f'/api/jobs/{job["id"]}/start')
        wait_for_job(client, job['id'])
        spec = run.calls[-1].spec
        assert (spec.client, spec.client_notes, spec.known_clients) == ('Acme', 'Name staff as SURNAME, Given.', ())
        assert client.get('/api/jobs').json()[0]['client'] == 'Acme'
        unknown = {'playbook_id': 'payroll', 'instructions': 'x', 'client_id': 'nobody'}
        assert client.post('/api/jobs', data=unknown, files=[SOURCE]).json()['detail'] == 'Unknown client'
        # A run with no client is told only the names of the known clients.
        plain = new_run(client)
        client.post(f'/api/jobs/{plain["id"]}/start')
        wait_for_job(client, plain['id'])
        spec = run.calls[-1].spec
        assert (spec.client, spec.client_notes, spec.known_clients) == ('', '', ('Acme',))


def test_the_briefing_puts_the_client_notes_beside_the_house_rules_and_the_client_wins(tmp_path):
    spec = spec_with_skills(tmp_path, skills=())
    plain = goal_card(spec, REMOTE, 'row')
    assert 'Client notes' not in plain and 'Known clients' not in plain  # a run with no client is unchanged
    card = goal_card(JobSpec(**{**spec.__dict__, 'client': 'Acme', 'client_notes': 'Amounts in MYR.'}), REMOTE, 'row')
    assert '# Client notes for Acme\n' in card and card.rstrip().count('Amounts in MYR.') == 1
    assert 'follow the client notes and say in notes.md which rule you followed' in card
    assert card.index('# House rules') < card.index('# Client notes')
    asking = goal_card(JobSpec(**{**spec.__dict__, 'known_clients': ('Acme', 'Beta')}), REMOTE, 'row')
    assert 'The user keeps notes for these clients: Acme, Beta.' in asking and 'follow_client' in asking


def with_client(tmp_path, notes='Amounts in MYR.'):
    store, job = job_with_outputs(tmp_path)
    acme = store.clients.create('Acme', notes)
    store.update(job['id'], client={'id': acme['id'], 'name': 'Acme', 'version': 1}, client_notes=notes)
    return store, store.get(job['id']), acme['id']


def test_a_lesson_goes_to_the_client_or_the_playbook_and_each_can_be_undone(tmp_path):
    store, job, acme = with_client(tmp_path)
    delivered(store, job)
    control = service.AppControl(store, job['id'])
    assert control.remember('1.1', RULE, 'playbook').startswith('Noted. It is added to the playbook')
    assert control.remember('1.2', 'Acme wants totals in bold.', 'client').startswith(
        "Noted. It is added to Acme's notes"
    )
    (store.path(job['id']) / 'outputs' / 'notes.md').write_text(
        'First line\nTotal net pay is 24,000.00', encoding='utf-8'
    )
    done = finish(store, job)
    assert {(x['n'], x['scope'], x['status']) for x in done['lessons']} == {
        (1, 'playbook', 'saved'),
        (2, 'client', 'saved'),
    }
    assert store.playbooks.get('payroll')['text'].endswith(RULE)
    notes = store.clients.get(acme)
    assert (notes['version'], notes['text']) == (2, 'Amounts in MYR.\n\nAcme wants totals in bold.')
    saved = [e for e in store.events(job['id']) if e['name'] == 'lesson.saved']
    assert {(e['scope'], e.get('client'), e.get('playbook')) for e in saved} == {
        ('playbook', None, 'payroll'),
        ('client', acme, None),
    }
    shown = feedback.rounds(store, job['id'])[0]
    assert (shown['learned_version'], shown['learned_client_version']) == (2, 2)
    # Undo: the version before the lesson comes back, in each place.
    assert RULE not in store.playbooks.restore('payroll', 1, based_on=2)['text']
    assert store.clients.restore(acme, 1, based_on=2)['text'] == 'Amounts in MYR.'


def test_the_place_the_user_chose_on_a_comment_wins_and_a_run_without_a_client_keeps_to_the_playbook(tmp_path):
    store, job, acme = with_client(tmp_path)
    delivered(store, job, [{**CELL, 'remember_to': 'client'}, {**LINE, 'remember': True, 'remember_to': 'client'}])
    assert store.clients.get(acme)['text'].endswith(LINE['text'])  # remembered at once, in the user's words
    assert store.playbooks.get('payroll')['version'] == 1  # the playbook is left as it was
    control = service.AppControl(store, job['id'])
    assert "Acme's notes" in control.remember('1.1', RULE, 'playbook')  # the comment said client
    plain_store, plain = job_with_outputs(tmp_path / 'plain')
    delivered(plain_store, plain)
    assert 'This run has no client' in lessons.note(plain_store, plain['id'], '1.1', RULE, 'client')
    try:
        feedback.create_round(
            plain_store, plain['id'], 'round-0009-zzzz', [{**CELL, 'remember': True, 'remember_to': 'client'}]
        )
        raise AssertionError('a client lesson on a run with no client was accepted')
    except ValueError as error:
        assert 'no client' in str(error)


def test_a_run_for_a_client_is_offered_that_clients_earlier_runs_and_never_another_clients(tmp_path):
    store = Store(tmp_path)
    made = {}
    for name, client in (('acme-old', 'Acme'), ('beta', 'Beta'), ('none', None), ('acme-new', 'Acme')):
        job = store.create('payroll', '2026-10', f'Run {name}', '', {'id': 'payroll', 'name': 'Payroll', 'version': 1})
        (store.path(job['id']) / 'outputs' / 'out.xlsx').write_bytes(b'x')
        store.update(job['id'], status='completed', client={'id': client, 'name': client} if client else None)
        made[name] = job['id']
    current = store.create('payroll', '2026-11', 'Now', '', {'id': 'payroll', 'name': 'Payroll', 'version': 1})
    store.update(current['id'], client={'id': 'Acme', 'name': 'Acme'})
    offered = service.choose_past_runs(store, store.get(current['id']))
    assert [r['id'] for r in offered] == [made['acme-new'], made['acme-old'], made['none']]
    assert [r.get('client') for r in offered] == ['Acme', 'Acme', None]
    loose = store.create('payroll', '2026-11', 'No client', '', {'id': 'payroll', 'name': 'Payroll', 'version': 1})
    assert len(service.choose_past_runs(store, store.get(loose['id']))) == 3  # as before: the newest three


def test_the_client_is_suggested_from_folder_and_file_names(tmp_path):
    with client_for(tmp_path) as client:
        for name in ('Acme', 'Blue River Sdn Bhd', 'Ace'):
            client.post('/api/clients', json={'name': name})

        def suggest(*paths):
            return (client.post('/api/clients/suggest', json={'paths': list(paths)}).json()['client'] or {}).get('name')

        assert suggest('Acme/October 2026/payroll.xlsx') == 'Acme'
        assert suggest('Payroll/acme_oct.xlsx', 'Payroll/bank.csv') == 'Acme'
        assert suggest('October/ACME Sdn Bhd/hr.xlsx') == 'Acme'
        assert suggest('BlueRiver/oct/pay.xlsx') == suggest('blue-river_payroll.xlsx') == 'Blue River Sdn Bhd'
        assert suggest('October/Payroll/staff.xlsx') is None  # an unrelated folder
        assert suggest('Acmeco/pay.xlsx', 'Spacey/x.csv') is None  # a name inside another word is not the name
        assert suggest('Acme/a.xlsx', 'Ace/b.xlsx') is None  # two clients as often: no guess


def test_a_run_with_no_client_follows_one_only_after_the_user_says_so(tmp_path):
    store, job = job_with_outputs(tmp_path)
    acme = store.clients.create('Acme', 'Amounts in MYR.')
    control = service.AppControl(store, job['id'])
    assert 'Ask the user with ask_user whether this run is for Acme first' in control.follow_client('Acme')['reply']
    assert control.follow_client('Zeta')['reply'] == 'There is no client "Zeta". The known clients are: Acme.'
    store.update(job['id'], engine_questions=[{'question': 'These payslips name ACME Sdn Bhd. Is this run for Acme?', 'answer': 'Yes'}])  # fmt: skip
    reply = control.follow_client('acme')['reply']
    assert reply.startswith('This run now follows the notes for Acme.') and reply.endswith('Amounts in MYR.')
    now = store.get(job['id'])
    assert now['client'] == {'id': acme['id'], 'name': 'Acme', 'version': 1, 'chosen_by': 'answer'}
    assert now['client_notes'] == 'Amounts in MYR.' and 'client.followed' in [
        e['name'] for e in store.events(job['id'])
    ]
    assert control.follow_client('Acme')['reply'] == 'This run already follows the notes for Acme.'
    # Once attached, the resumed run's briefing carries the notes like a client chosen at the start.
    spec = service.job_spec(store, {**now, 'files': [], 'past_runs': []})
    assert (spec.client, spec.known_clients) == ('Acme', ())


def test_the_words_of_a_name_must_appear_in_a_row():
    assert clients.mentions('Acme', 'x/ACME_payroll.xlsx') and not clients.mentions('Acme', 'acmeco.xlsx')
    assert clients.mentions('Blue River Sdn Bhd', 'River blue? no; blue river yes')
    assert not clients.mentions('Blue River', 'blue/sky/river')


def test_a_no_or_another_client_in_the_answer_attaches_nothing(tmp_path):
    store, job = job_with_outputs(tmp_path)
    store.clients.create('Acme')
    store.clients.create('Beta')
    control = service.AppControl(store, job['id'])
    for answer in ('No, it is for Beta.', 'Beta', 'Not sure, check with HR'):
        store.update(job['id'], engine_questions=[{'question': 'Is this run for Acme or Beta?', 'answer': answer}])
        followed = control.follow_client('Acme')
        assert followed['client'] is None and 'does not say this run is for Acme' in followed['reply'], answer
        assert store.get(job['id']).get('client') is None
    assert clients.confirmed('Acme', 'Yes') and clients.confirmed('Acme', 'It is Acme.')
    assert clients.confirmed('华联', '华联') and clients.mentions('华联 Sdn Bhd', '华联/十月.xlsx')  # any script


def test_following_a_client_mid_run_drops_other_clients_earlier_runs_and_updates_the_run(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from agents.tool_context import ToolContext

    from app.engine import agent as engine
    from app.engine.runstore import RunStore

    store, job = job_with_outputs(tmp_path)
    store.clients.create('Acme', 'Amounts in MYR.')
    seen = {'title': 'Payroll', 'finished': '2026-10-30'}
    runs = [{'label': 'earlier-1', 'client': 'Beta', **seen}, {'label': 'earlier-2', 'client': 'Acme', **seen},
            {'label': 'earlier-3', **seen}]  # fmt: skip
    store.update(job['id'], past_runs=runs, engine_questions=[{'question': 'Is this for Acme?', 'answer': 'yes'}])
    spec = JobSpec(**{**spec_with_skills(tmp_path, skills=()).__dict__, 'context': '', 'known_clients': ('Acme',),
                      'past_runs': tuple(runs)})  # fmt: skip
    live = SimpleNamespace(
        spec=spec,
        control=service.AppControl(store, job['id']),
        remote=REMOTE,
        proof='row',
        card='',
        store=RunStore(tmp_path / 'run'),
    )
    context = ToolContext(live, tool_name='follow_client', tool_call_id='1', tool_arguments='{"client": "Acme"}')
    reply = asyncio.run(engine.follow_client.on_invoke_tool(context, '{"client": "Acme"}'))
    assert reply.startswith('This run now follows the notes for Acme.')
    assert (live.spec.client, live.spec.client_notes) == ('Acme', 'Amounts in MYR.')  # the rules check reads these
    assert [r['label'] for r in live.spec.past_runs] == ['earlier-2', 'earlier-3']
    assert [r['label'] for r in store.get(job['id'])['past_runs']] == ['earlier-2', 'earlier-3']
    assert '# Client notes for Acme' in live.card  # what the reviewer is given


def test_a_round_that_cannot_be_kept_in_one_place_changes_neither(tmp_path):
    store, job, acme = with_client(tmp_path, notes='x' * 29_990)
    comments = [{**CELL, 'remember': True}, {**LINE, 'remember': True, 'remember_to': 'client'}]
    try:
        feedback.create_round(store, job['id'], 'round-0007-full', comments)
        raise AssertionError('a round over the notes limit was kept')
    except ValueError as error:
        assert 'would pass 30,000 characters' in str(error)
    assert store.playbooks.get('payroll')['version'] == 1 and store.clients.get(acme)['version'] == 1


def test_only_a_clear_yes_confirms_a_client():
    for answer in ('Yes', 'Yes, it is Acme.', 'Acme', 'ACME Sdn Bhd', 'Correct'):
        assert clients.confirmed('Acme', answer), answer
    for answer in ('This is not Acme', "I don't think this is Acme.", 'Is Acme the company on the invoice?',
                   'Acme or Beta, not sure', 'No', ''):  # fmt: skip
        assert not clients.confirmed('Acme', answer), answer
