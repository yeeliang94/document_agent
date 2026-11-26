"""Application contract tests; no external model requests are performed."""

import io
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from helpers import LOCAL, create_job, workbook_bytes


def test_upload_nested_inputs_persists_job_and_serves_frontend(tmp_path):
    settings = Settings(data_dir=tmp_path, api_key='')
    with TestClient(base_url=LOCAL, app=create_app(settings)) as client:
        assert client.get('/').status_code == 200
        job = create_job(client)
        assert job['status'] == 'ready'
        assert client.get(f'/api/jobs/{job["id"]}').json()['instructions'] == 'Fill Employees A2:D2.'
        assert list((tmp_path / 'jobs' / job['id'] / 'inputs').rglob('employees.txt'))
    with TestClient(base_url=LOCAL, app=create_app(settings)) as client:
        assert client.get(f'/api/jobs/{job["id"]}').json()['period'] == '2026-10'


def test_start_without_key_has_actionable_error(tmp_path):
    with TestClient(base_url=LOCAL, app=create_app(Settings(data_dir=tmp_path, api_key=''))) as client:
        job = create_job(client)
        response = client.post(f'/api/jobs/{job["id"]}/start')
        assert response.status_code == 503
        assert 'OPENAI_API_KEY' in response.text
        assert client.get(f'/api/jobs/{job["id"]}').json()['status'] == 'ready'


@pytest.mark.parametrize('filename', ['../escape.txt', '/escape.txt', 'C:/escape.txt', 'safe/../../escape.txt'])
def test_uploaded_path_traversal_is_rejected(tmp_path, filename):
    with TestClient(base_url=LOCAL, app=create_app(Settings(data_dir=tmp_path, api_key=''))) as client:
        response = client.post(
            '/api/jobs',
            data={'playbook_id': 'payroll', 'period': '2026-10'},
            files=[
                ('template', ('template.xlsx', workbook_bytes())),
                ('files', (filename, b'unsafe')),
            ],
        )
        assert response.status_code == 400, response.text
        assert not (tmp_path / 'escape.txt').exists()


def test_zip_preserves_nested_files_and_rejects_zip_slip(tmp_path):
    for entry, expected_status in [('nested/team/employees.txt', 201), ('../escape.txt', 400)]:
        buffer = io.BytesIO()
        with ZipFile(buffer, 'w') as archive:
            archive.writestr(entry, 'Synthetic employees')
        with TestClient(base_url=LOCAL, app=create_app(Settings(data_dir=tmp_path, api_key=''))) as client:
            response = client.post(
                '/api/jobs',
                data={'playbook_id': 'payroll', 'period': '2026-10'},
                files=[
                    ('template', ('template.xlsx', workbook_bytes())),
                    ('files', ('packet.zip', buffer.getvalue(), 'application/zip')),
                ],
            )
            assert response.status_code == expected_status, response.text
            if expected_status == 201:
                assert (tmp_path / 'jobs' / response.json()['id'] / 'inputs' / 'packet' / entry).exists()
    assert not (tmp_path / 'escape.txt').exists()


def test_config_publishes_the_status_groups_the_api_enforces(tmp_path):
    from app import jobstatus

    with TestClient(base_url=LOCAL, app=create_app(Settings(data_dir=tmp_path, api_key=''))) as client:
        statuses = client.get('/api/config').json()['statuses']
    assert statuses == jobstatus.as_json()
    assert 'running' in statuses['active'] and 'partial' in statuses['startable']


def test_a_run_is_renamed_and_deleted_but_not_while_it_is_going(tmp_path):
    with TestClient(base_url=LOCAL, app=create_app(Settings(data_dir=tmp_path, api_key=''))) as client:
        kept, going = create_job(client), create_job(client)
        renamed = client.patch(f'/api/jobs/{kept["id"]}', json={'name': '  October   payroll '})
        assert renamed.status_code == 200 and renamed.json()['name'] == 'October payroll'
        assert next(j for j in client.get('/api/jobs').json() if j['id'] == kept['id'])['name'] == 'October payroll'
        assert client.patch(f'/api/jobs/{kept["id"]}', json={'name': 'x' * 81}).status_code == 400
        assert client.patch(f'/api/jobs/{kept["id"]}', json={'name': ''}).json()['name'] is None  # back to its goal
        assert client.patch('/api/jobs/' + 'a' * 32, json={'name': 'Nope'}).status_code == 404

        client.app.state.store.update(going['id'], status='running')
        refused = client.delete(f'/api/jobs/{going["id"]}')
        assert refused.status_code == 409 and 'Stop it before you delete it' in refused.json()['detail']
        assert (tmp_path / 'jobs' / going['id']).exists()

        assert client.delete(f'/api/jobs/{kept["id"]}').status_code == 204
        assert client.get(f'/api/jobs/{kept["id"]}').status_code == 404
        assert not (tmp_path / 'jobs' / kept['id']).exists()
        assert [j['id'] for j in client.get('/api/jobs').json()] == [going['id']]
        assert client.delete(f'/api/jobs/{kept["id"]}').status_code == 404
