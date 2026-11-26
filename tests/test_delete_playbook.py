"""Deleting a playbook removes it and everything kept with it; its runs stay and still open (plan Step 57)."""

from test_sandbox_engine import client_for


def test_a_deleted_playbook_is_gone_with_its_folder_and_its_runs_still_open(tmp_path):
    with client_for(tmp_path) as client:
        store = client.app.state.store
        made = client.post('/api/playbooks', json={'name': 'Bank reconciliation'}).json()
        folder = tmp_path / 'playbooks' / made['id']
        (folder / 'cases').mkdir(parents=True)  # a past correction kept for it
        job = store.create(made['id'], '2026-10', 'Run', '', {'id': made['id'], 'name': made['name'], 'version': 1})
        hidden = store.create(
            made['id'], '2026-10', 'Replay', '', {'id': made['id'], 'name': made['name'], 'version': 1}
        )
        store.update(hidden['id'], replay={'playbook': made['id'], 'case': 'c1'})  # left by its last check
        gone = client.delete(f'/api/playbooks/{made["id"]}')
        assert gone.status_code == 200 and gone.json() == {
            'id': made['id'],
            'name': 'Bank reconciliation',
            'runs_kept': 1,
        }
        assert client.get(f'/api/playbooks/{made["id"]}').status_code == 404
        assert made['id'] not in [p['id'] for p in client.get('/api/playbooks').json()]
        assert not folder.exists()
        assert client.get(f'/api/jobs/{hidden["id"]}').status_code == 404  # its hidden replay run went with it
        assert client.get(f'/api/jobs/{job["id"]}').status_code == 200  # the run is kept and still opens


def test_the_only_playbook_and_a_missing_one_cannot_be_deleted(tmp_path):
    with client_for(tmp_path) as client:
        ids = [p['id'] for p in client.get('/api/playbooks').json()]
        for extra in ids[1:]:
            assert client.delete(f'/api/playbooks/{extra}').status_code == 200
        last = client.delete(f'/api/playbooks/{ids[0]}')
        assert last.status_code == 400 and last.json()['detail'] == 'The only playbook cannot be deleted.'
        assert client.delete('/api/playbooks/nope').status_code == 404


def test_a_deleted_built_in_playbook_is_not_made_again_at_the_next_start(tmp_path):
    with client_for(tmp_path) as client:
        assert client.delete('/api/playbooks/payroll').status_code == 200
    with client_for(tmp_path) as client:  # the app starts again on the same data
        assert 'payroll' not in [p['id'] for p in client.get('/api/playbooks').json()]
