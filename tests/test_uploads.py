"""Upload handling and run history."""

import io
from zipfile import ZipFile

from fastapi.testclient import TestClient

from app.config import Settings
from app.files import add_zip, is_junk
from app.main import create_app

from helpers import LOCAL, create_job, workbook_bytes


def test_failed_upload_leaves_no_job_behind(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, api_key='')), base_url=LOCAL) as client:
        response = client.post(
            '/api/jobs',
            data={'module': 'payroll', 'period': '2026-10'},
            files=[('template', ('template.xlsx', b'not a workbook')), ('files', ('a.txt', b'x'))],
        )
        assert response.status_code == 400
        assert client.get('/api/jobs').json() == []


def test_os_clutter_is_skipped_and_clutter_only_upload_is_refused(tmp_path):
    assert is_junk('__MACOSX/a/._report.xlsx') and is_junk('Team/.DS_Store') and is_junk('Thumbs.db')
    assert not is_junk('Team/report.xlsx')
    data = io.BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('HR/report.txt', 'real')
        archive.writestr('__MACOSX/HR/._report.txt', 'clutter')
        archive.writestr('HR/.DS_Store', 'clutter')
    manifest = []
    add_zip(tmp_path, 'packet.zip', data.getvalue(), manifest, 10, 10_000)
    assert [f['path'] for f in manifest] == ['inputs/packet/HR/report.txt']
    with TestClient(create_app(Settings(data_dir=tmp_path, api_key='')), base_url=LOCAL) as client:
        response = client.post(
            '/api/jobs',
            data={'module': 'payroll', 'period': '2026-10'},
            files=[('template', ('template.xlsx', workbook_bytes())), ('files', ('Team/.DS_Store', b'clutter'))],
        )
        assert response.status_code == 400
        assert 'source files' in response.text


def test_job_list_omits_bulky_fields(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, api_key='')), base_url=LOCAL) as client:
        job = create_job(client)
        listed = client.get('/api/jobs').json()
        assert [j['id'] for j in listed] == [job['id']]
        assert set(listed[0]) == {'id', 'module', 'period', 'status', 'created_at', 'updated_at'}
