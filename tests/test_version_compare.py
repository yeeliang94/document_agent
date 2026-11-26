import json

import pytest
from test_feedback import job_with_outputs, workbook
from test_sandbox_engine import FakeRun, client_for, finished, start_sandbox_job, wait_for_runner_idle

from app import feedback, preview
from app.engine import service, versions

from helpers import wait_for_job


def keep(store, job_id, step):
    root = store.path(job_id)
    return versions.keep(root / 'outputs', root / 'versions', 'run-1', step, f'Step {step}', True, 20)


def test_an_output_is_compared_with_any_earlier_version_and_an_older_one_can_be_read(tmp_path):
    store, job = job_with_outputs(tmp_path)
    outputs, root = store.path(job['id']) / 'outputs', store.path(job['id'])
    keep(store, job['id'], 1)  # version 1: basic pay 4200
    workbook(outputs / 'upload.xlsx', basic=1960)
    keep(store, job['id'], 2)  # version 2: 1960
    workbook(outputs / 'upload.xlsx', basic=2100)
    (outputs / 'fresh.txt').write_text('new', encoding='utf-8')

    since_one = feedback.changes_since(store, job['id'], 'outputs/upload.xlsx', 'Employees', since=1)
    assert since_one['since'] == 1 and since_one['cells']['C2']['value'] == 4200
    since_two = feedback.changes_since(store, job['id'], 'outputs/upload.xlsx', 'Employees', since=2)
    assert since_two['cells']['C2']['value'] == 1960
    # Version 2 itself, against version 1.
    old_pair = feedback.changes_since(store, job['id'], 'outputs/upload.xlsx', 'Employees', version=2, since=1)
    assert old_pair['cells']['C2']['value'] == 4200
    # An older version has no round of comments to compare with; a file the version did not have is new.
    assert feedback.changes_since(store, job['id'], 'outputs/upload.xlsx', 'Employees', version=2) == {'round': None}
    assert feedback.changes_since(store, job['id'], 'outputs/fresh.txt', since=1) == {'since': 1, 'new': True}
    with pytest.raises(FileNotFoundError):
        feedback.changes_since(store, job['id'], 'outputs/upload.xlsx', since=9)

    # The preview reads a file as the version kept it; only outputs are kept.
    shown = preview.preview(root, 'outputs/upload.xlsx', 'Employees', version=1)
    assert '4200' in json.dumps(shown) and '2100' not in json.dumps(shown)
    with pytest.raises(FileNotFoundError):
        preview.version_file(root, 1, 'inputs/source.txt')
    with pytest.raises(FileNotFoundError):
        preview.version_file(root, 1, 'outputs/version.json')
    with pytest.raises(ValueError):
        preview.version_file(root, 1, 'outputs/../../secret.txt')


def test_versions_kept_before_files_had_their_own_folder_can_still_be_read(tmp_path):
    store, job = job_with_outputs(tmp_path)
    root = store.path(job['id'])
    old = root / 'versions' / 'v0001'
    old.mkdir(parents=True)
    workbook(old / 'upload.xlsx', basic=999)
    (old / 'version.json').write_text(json.dumps({'number': 1, 'files': ['upload.xlsx']}), encoding='utf-8')
    assert preview.version_file(root, 1, 'outputs/upload.xlsx') == old / 'upload.xlsx'
    with pytest.raises(FileNotFoundError):
        preview.version_file(root, 1, 'outputs/version.json')  # the record is not an output


def test_the_api_compares_with_a_version_and_serves_its_files(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        workbook(outputs / 'template_output.xlsx')
        return finished()

    monkeypatch.setattr(service, 'run_job', FakeRun(behaviour))
    with client_for(tmp_path) as client:
        store = client.app.state.store
        job = start_sandbox_job(client)
        wait_for_job(client, job['id'])
        wait_for_runner_idle(client)
        root = store.path(job['id'])
        versions.keep(root / 'outputs', root / 'versions', 'run-1', 1, 'Step 1', True, 20)
        workbook(root / 'outputs' / 'template_output.xlsx', basic=10)
        url = f'/api/jobs/{job["id"]}'
        path = 'outputs/template_output.xlsx'
        changes = client.get(f'{url}/changes', params={'path': path, 'sheet': 'Employees', 'since': 1}).json()
        assert changes['since'] == 1 and changes['cells']['C2']['value'] == 4200
        shown = client.get(f'{url}/preview', params={'path': path, 'sheet': 'Employees', 'version': 1})
        assert shown.status_code == 200 and '4200' in shown.text
        assert client.get(f'{url}/file', params={'path': path, 'version': 1}).status_code == 200
        assert client.get(f'{url}/preview', params={'path': path, 'version': 7}).status_code == 404
        assert client.get(f'{url}/changes', params={'path': path, 'since': 7}).status_code == 404
