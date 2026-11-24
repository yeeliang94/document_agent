"""Playbooks: instructions kept as versions, the routes around them, and their activity log."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.engine.goal import goal_card
from app.engine.service import job_spec
from app.main import create_app
from app.modules import DEFAULT_CONTEXT
from app.playbooks import StaleVersion
from app.store import Store

from helpers import LOCAL, create_job, workbook_bytes


def client_for(tmp_path):
    return TestClient(base_url=LOCAL, app=create_app(Settings(data_dir=tmp_path, api_key='')))


def names(events):
    return [e['name'] for e in events]


def test_the_earlier_modules_become_the_first_playbooks_with_their_edited_text(tmp_path):
    # A database from before playbooks: the user had edited the payroll instructions.
    with sqlite3.connect(tmp_path / 'jobs.db') as c:
        c.execute('CREATE TABLE profiles(module TEXT PRIMARY KEY, context TEXT NOT NULL)')
        c.execute('INSERT INTO profiles VALUES (?,?)', ('payroll', 'Edited payroll rules'))
    books = Store(tmp_path).playbooks
    assert [(p['id'], p['name'], p['version']) for p in books.list()] == [
        ('payroll', 'Payroll', 1),
        ('ap', 'Accounts payable', 1),
    ]
    assert books.get('payroll')['text'] == 'Edited payroll rules'
    assert books.get('ap')['text'] == DEFAULT_CONTEXT['ap']
    # Opening the data again changes nothing: no second copy, and a later edit is not overwritten.
    books.save('payroll', 'Newer rules')
    again = Store(tmp_path).playbooks
    assert len(again.list()) == 2
    assert again.get('payroll')['text'] == 'Newer rules' and again.get('payroll')['version'] == 2


def test_every_change_is_a_new_version_and_nothing_is_lost(tmp_path):
    books = Store(tmp_path).playbooks
    book = books.create('Bank reconciliation')
    assert (book['version'], book['text']) == (1, '')
    first = books.save(book['id'], 'First rules', based_on=1)
    assert (first['version'], first['text'], first['saved']) == (2, 'First rules', True)
    assert (
        books.save(book['id'], 'Second rules', 'remembered', 'Use the signed form.', job_id='j1', round=2)['version']
        == 3
    )
    # Saving the same text again adds nothing.
    same = books.save(book['id'], 'Second rules')
    assert (same['version'], same['saved']) == (3, False)
    restored = books.restore(book['id'], 2)
    assert (restored['version'], restored['text']) == (4, 'First rules')
    history = books.versions(book['id'])
    assert [(v['version'], v['source']) for v in history] == [
        (4, 'restored'),
        (3, 'remembered'),
        (2, 'edited'),
        (1, 'created'),
    ]
    assert history[0]['detail'] == {'restored_from': 2}
    assert history[1]['change'] == 'Use the signed form.' and history[1]['detail'] == {'job_id': 'j1', 'round': 2}
    assert [books.version(book['id'], n)['text'] for n in (1, 2, 3, 4)] == [
        '',
        'First rules',
        'Second rules',
        'First rules',
    ]


def test_a_save_based_on_an_old_version_is_refused_and_logged(tmp_path):
    books = Store(tmp_path).playbooks
    book = books.create('Expenses')
    books.save(book['id'], 'From the agent', 'tuned', based_on=1)
    with pytest.raises(StaleVersion) as refused:
        books.save(book['id'], 'From the user, who had version 1 open', based_on=1)
    assert refused.value.current == 2
    assert books.get(book['id'])['text'] == 'From the agent'
    with pytest.raises(StaleVersion):
        books.restore(book['id'], 1, based_on=1)
    log = books.events(book['id'])
    assert names(log) == [
        'playbook.created',
        'playbook.version_saved',
        'playbook.save_refused',
        'playbook.save_refused',
    ]
    assert (log[2]['level'], log[2]['based_on'], log[2]['version']) == ('WARN', 1, 2)


def test_names_are_checked_and_unknown_playbooks_are_errors(tmp_path):
    books = Store(tmp_path).playbooks
    book = books.create('  Month   end  ')
    assert book['name'] == 'Month end'
    for bad in ('', '   ', 'x' * 81, 'month END', 'Payroll'):
        with pytest.raises(ValueError):
            books.create(bad)
    assert books.rename(book['id'], 'Month end')['name'] == 'Month end'  # keeping its own name is fine
    with pytest.raises(ValueError):
        books.rename(book['id'], 'payroll')
    for call in (
        lambda: books.get('nope'),
        lambda: books.save('nope', 'x'),
        lambda: books.versions('nope'),
        lambda: books.version(book['id'], 9),
        lambda: books.restore(book['id'], 9),
        lambda: books.create('Copy', copy_of='nope'),
    ):
        with pytest.raises(KeyError):
            call()
    assert len(books.list()) == 3  # the failed copy left nothing behind


def test_a_copy_starts_from_the_latest_instructions(tmp_path):
    books = Store(tmp_path).playbooks
    books.save('ap', 'Latest AP rules')
    copy = books.create('AP for the UK entity', copy_of='ap')
    assert (copy['version'], copy['text']) == (1, 'Latest AP rules')
    assert books.versions(copy['id'])[0]['detail'] == {'copied_from': 'ap', 'copied_version': 2}


def test_the_activity_log_follows_one_playbook_and_keeps_secrets_out(tmp_path):
    books = Store(tmp_path).playbooks
    book = books.create('Expenses')
    books.rename(book['id'], 'Staff expenses')
    books.save(book['id'], 'Rules', 'remembered', 'key sk-abcdefghijklmnop', job_id='a' * 32, round=1, api_key='secret')
    log = books.events(book['id'])
    assert names(log) == ['playbook.created', 'playbook.renamed', 'playbook.version_saved']
    saved = log[-1]
    assert (saved['playbook_id'], saved['version'], saved['previous'], saved['source']) == (
        book['id'],
        2,
        1,
        'remembered',
    )
    assert (saved['detail']['job_id'], saved['detail']['round'], saved['chars']) == ('a' * 32, 1, 5)
    assert saved['detail']['api_key'] == '[redacted]' and 'sk-' not in saved['change']
    # The history keeps the same things out.
    kept = books.versions(book['id'])[0]
    assert kept['detail']['api_key'] == '[redacted]' and 'sk-' not in kept['change']
    assert books.events(book['id'], after=log[1]['seq']) == [saved]
    assert books.events('payroll') == []


def test_the_playbook_routes(tmp_path):
    with client_for(tmp_path) as client:
        assert [p['id'] for p in client.get('/api/playbooks').json()] == ['payroll', 'ap']
        made = client.post('/api/playbooks', json={'name': 'Bank reconciliation', 'copy_of': 'ap'})
        assert made.status_code == 201
        book = made.json()
        url = f'/api/playbooks/{book["id"]}'
        assert book['text'] == DEFAULT_CONTEXT['ap']
        assert client.patch(url, json={'name': 'Bank rec'}).json()['name'] == 'Bank rec'
        saved = client.post(url + '/versions', json={'text': 'New rules', 'based_on': 1}).json()
        assert (saved['version'], saved['saved']) == (2, True)
        stale = client.post(url + '/versions', json={'text': 'Other rules', 'based_on': 1})
        assert stale.status_code == 409 and stale.json()['detail']['version'] == 2
        assert client.get(url).json()['text'] == 'New rules'
        assert client.post(url + '/restore', json={'version': 1, 'based_on': 2}).json()['version'] == 3
        assert [v['version'] for v in client.get(url + '/versions').json()] == [3, 2, 1]
        assert client.get(url + '/versions/2').json()['text'] == 'New rules'
        assert names(client.get(url + '/events').json())[-1] == 'playbook.version_saved'
        config = client.get('/api/config').json()
        assert [p['name'] for p in config['playbooks']] == ['Payroll', 'Accounts payable', 'Bank rec']
        # Refusals the screen can show.
        assert client.post('/api/playbooks', json={'name': 'bank REC'}).status_code == 400
        assert client.post('/api/playbooks', json={'name': 'X', 'copy_of': 'nope'}).status_code == 404
        assert client.post(url + '/versions', json={'text': 'x' * 30001}).status_code == 422
        for missing in (
            '/api/playbooks/nope',
            '/api/playbooks/nope/versions',
            '/api/playbooks/nope/events',
            url + '/versions/9',
        ):
            assert client.get(missing).status_code == 404
        assert client.post(url + '/restore', json={'version': 9, 'based_on': 3}).status_code == 404


def test_the_module_routes_are_gone(tmp_path):
    with client_for(tmp_path) as client:
        assert client.get('/api/modules/payroll').status_code == 404
        assert client.put('/api/modules/payroll', json={'context': 'x'}).status_code in (404, 405)
        assert 'modules' not in client.get('/api/config').json()
        assert upload(client, module='payroll').status_code == 400  # the earlier field no longer names a playbook


def upload(client, **data):
    return client.post(
        '/api/jobs',
        data={'period': '2026-10', **data},
        files=[('template', ('template.xlsx', workbook_bytes())), ('files', ('a.txt', b'EMP001'))],
    )


def test_a_job_copies_the_playbook_it_was_started_with(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key=''))
    with TestClient(base_url=LOCAL, app=app) as client:
        store = app.state.store
        store.playbooks.save('payroll', 'Payroll rules, version 2')
        by_playbook = upload(client, playbook_id='payroll').json()
        assert by_playbook['playbook'] == {'id': 'payroll', 'name': 'Payroll', 'version': 2}
        assert (by_playbook['module'], by_playbook['context']) == ('payroll', 'Payroll rules, version 2')
        # A later edit to the playbook does not reach a job that already exists.
        store.playbooks.save('payroll', 'Payroll rules, version 3')
        assert client.get(f'/api/jobs/{by_playbook["id"]}').json()['context'] == 'Payroll rules, version 2'
        received = store.events(by_playbook['id'])[0]
        assert received['playbook'] == {'id': 'payroll', 'name': 'Payroll', 'version': 2}
        assert client.get('/api/jobs').json()[0]['playbook'] == 'Payroll'
        # A playbook made by the user works the same way.
        own = client.post('/api/playbooks', json={'name': 'Bank reconciliation'}).json()
        job = upload(client, playbook_id=own['id']).json()
        assert job['playbook'] == {'id': own['id'], 'name': 'Bank reconciliation', 'version': 1}
        assert upload(client, playbook_id='nope').status_code == 400
        assert upload(client).status_code == 400


def test_the_goal_card_and_run_record_name_the_playbook(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key=''))
    with TestClient(base_url=LOCAL, app=app) as client:
        store = app.state.store
        job = upload(client, playbook_id='payroll').json()
        spec = job_spec(store, store.get(job['id']))
        assert (spec.playbook, spec.playbook_id, spec.playbook_version) == ('Payroll', 'payroll', 1)
        card = goal_card(spec, {'template.xlsx': '/mnt/data/template.xlsx'}, 'row')
        assert '# House rules for Payroll\n' + DEFAULT_CONTEXT['payroll'] in card
        # A job from before playbooks has no playbook on it and still runs under its module.
        old = create_job(client)
        store.mutate(old['id'], lambda j: j.pop('playbook'))
        spec = job_spec(store, store.get(old['id']))
        assert (spec.playbook, spec.module) == ('', 'payroll')
        assert '# House rules for payroll\n' in goal_card(spec, {'template.xlsx': '/mnt/data/template.xlsx'}, 'row')


def test_a_job_names_a_version_only_when_it_used_that_exact_text(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key=''))
    with TestClient(base_url=LOCAL, app=app) as client:
        store = app.state.store
        store.playbooks.save('payroll', 'Saved rules')
        # The screen sends the text it shows. The same text as the playbook: the version is named.
        same = upload(client, playbook_id='payroll', context='Saved rules').json()
        assert same['playbook'] == {'id': 'payroll', 'name': 'Payroll', 'version': 2}
        # An unsaved edit or a stale tab: the text is used as sent, and no version claims it.
        edited = upload(client, playbook_id='payroll', context='Text from a stale tab').json()
        assert edited['context'] == 'Text from a stale tab'
        assert edited['playbook'] == {'id': 'payroll', 'name': 'Payroll', 'version': None, 'edited_from': 2}
        assert store.events(edited['id'])[0]['playbook'] == edited['playbook']
        assert job_spec(store, store.get(edited['id'])).playbook_version is None


def test_saves_and_restores_must_say_which_version_they_started_from(tmp_path):
    with client_for(tmp_path) as client:
        url = '/api/playbooks/payroll'
        assert client.post(url + '/versions', json={'text': 'No version named'}).status_code == 422
        assert client.post(url + '/restore', json={'version': 1}).status_code == 422
        # Numbers no database could hold are refused as bad input, not as a server error.
        huge = 10**30
        assert client.get(f'{url}/versions/{huge}').status_code == 422
        assert client.post(url + '/restore', json={'version': huge, 'based_on': 1}).status_code == 422
        assert client.get(f'{url}/events?after={huge}').status_code == 422
        assert client.get(url).json()['version'] == 1


def test_a_version_and_its_log_entry_are_written_together_whatever_the_detail_holds(tmp_path):
    books = Store(tmp_path).playbooks
    # Detail keys that share a name with the log's own fields must not break the save half way.
    saved = books.save('payroll', 'Rules', 'remembered', version=9, level='y', name='z', message='m')
    assert (saved['version'], saved['saved']) == (2, True)
    entry = books.events('payroll')[-1]
    assert (entry['name'], entry['version'], entry['source'], entry['level']) == (
        'playbook.version_saved',
        2,
        'remembered',
        'INFO',
    )
    assert entry['detail']['version'] == 9


def test_a_new_database_has_no_table_from_before_playbooks_and_an_old_one_is_left_alone(tmp_path):
    def tables(root):
        with sqlite3.connect(root / 'jobs.db') as c:
            return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    new = tmp_path / 'new'
    books = Store(new).playbooks
    assert 'profiles' not in tables(new)
    assert [(p['id'], p['name']) for p in books.list()] == [('payroll', 'Payroll'), ('ap', 'Accounts payable')]
    assert books.get('payroll')['text'] == DEFAULT_CONTEXT['payroll']
    old = tmp_path / 'old'
    old.mkdir()
    with sqlite3.connect(old / 'jobs.db') as c:
        c.execute('CREATE TABLE profiles(module TEXT PRIMARY KEY, context TEXT NOT NULL)')
        c.execute('INSERT INTO profiles VALUES (?,?)', ('payroll', 'Edited before playbooks'))
    Store(old).playbooks.save('payroll', 'Edited after the upgrade')
    assert Store(old).playbooks.get('payroll')['text'] == 'Edited after the upgrade'  # a restart does not undo it
    with sqlite3.connect(old / 'jobs.db') as c:
        assert c.execute('SELECT module, context FROM profiles').fetchall() == [('payroll', 'Edited before playbooks')]


def test_an_emptied_playbook_gives_no_house_rules_rather_than_the_built_in_ones(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key=''))
    with TestClient(base_url=LOCAL, app=app) as client:
        store = app.state.store
        store.playbooks.save('payroll', '')
        job = upload(client, playbook_id='payroll').json()
        card = goal_card(job_spec(store, store.get(job['id'])), {'template.xlsx': '/mnt/data/template.xlsx'}, 'row')
        assert 'House rules' not in card
