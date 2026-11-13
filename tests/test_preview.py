"""The Files panel: previews of uploads, the template and outputs."""

import io
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.config import Settings
from app.main import create_app

from helpers import LOCAL, create_job, workbook_bytes


def pdf_bytes():
    from PIL import Image

    out = io.BytesIO()
    Image.new('RGB', (200, 280), 'white').save(out, format='PDF')
    return out.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(base_url=LOCAL, app=create_app(Settings(data_dir=tmp_path, api_key=''))) as c:
        c.root = tmp_path
        yield c


def upload(client, name, data):
    job = create_job(client, files=[('files', (name, data))])
    return job, next(f['path'] for f in job['files'] if f['role'] == 'inputs')


def get(client, job, path, **more):
    return client.get(f'/api/jobs/{job["id"]}/preview', params={'path': path, **more})


def test_a_workbook_previews_as_rows_with_its_sheet_names(client):
    job = create_job(client)
    template = next(f['path'] for f in job['files'] if f['role'] == 'template')
    body = get(client, job, template).json()
    assert body['kind'] == 'sheet' and body['name'] == 'template.xlsx'
    assert body['sheet'] in [s['name'] for s in body['sheets']]
    assert body['columns'][0] == 'A' and all(len(row) <= len(body['columns']) for row in body['rows'])
    other = body['sheets'][-1]['name']
    assert get(client, job, template, sheet=other).json()['sheet'] == other


def test_a_formula_without_a_stored_result_shows_its_text_and_dates_read_plainly(client):
    from datetime import datetime

    book = Workbook()
    book.active.append(['Pay', 4200, datetime(2026, 10, 1)])
    book.active.append(['EPF', '=B1*0.11'])
    out = io.BytesIO()
    book.save(out)
    job, path = upload(client, 'HR/pay.xlsx', out.getvalue())
    assert get(client, job, path).json()['rows'] == [['Pay', 4200, '2026-10-01'], ['EPF', '=B1*0.11', None]]


def test_text_csv_pdf_and_image_files_each_get_a_fitting_preview(client):
    job, path = upload(client, 'notes.txt', b'Letters win over the register.')
    assert get(client, job, path).json() | {'size': 0} == {
        'path': path, 'name': 'notes.txt', 'size': 0, 'kind': 'text', 'text': 'Letters win over the register.', 'more': False}  # fmt: skip
    job, path = upload(client, 'bank.csv', b'id,net\nE1,3900\n')
    body = get(client, job, path).json()
    assert body['kind'] == 'sheet' and body['rows'] == [['id', 'net'], ['E1', '3900']] and body['columns'] == ['A', 'B']
    job, path = upload(client, 'letter.pdf', pdf_bytes())
    assert get(client, job, path).json()['pages'] == 1
    page = client.get(f'/api/jobs/{job["id"]}/page', params={'path': path, 'page': 1})
    assert page.status_code == 200 and page.headers['content-type'] == 'image/png' and page.content[:4] == b'\x89PNG'
    assert client.get(f'/api/jobs/{job["id"]}/page', params={'path': path, 'page': 2}).status_code == 400
    job, path = upload(client, 'scan.png', b'\x89PNG not really')
    assert get(client, job, path).json()['kind'] == 'image'
    raw = client.get(f'/api/jobs/{job["id"]}/file', params={'path': path})
    assert raw.status_code == 200 and raw.content == b'\x89PNG not really'


def test_a_damaged_or_unknown_file_says_so_instead_of_failing(client):
    job, path = upload(client, 'broken.xlsx', b'not a workbook')
    body = get(client, job, path).json()
    assert body['kind'] == 'none' and body['error']
    job, path = upload(client, 'archive.bin', b'123')
    assert get(client, job, path).json()['kind'] == 'none'


@pytest.mark.parametrize(
    'path', ['../jobs.db', 'working/state.db', 'engine/run-01/record.json', '/etc/passwd', 'inputs', '.']
)
def test_only_uploads_the_template_and_outputs_can_be_read(client, path):
    job = create_job(client)
    folder = client.root / 'jobs' / job['id']
    (folder / 'working').mkdir(exist_ok=True)
    (folder / 'working' / 'state.db').write_bytes(b'private')
    for route in ('preview', 'file', 'page'):
        assert client.get(f'/api/jobs/{job["id"]}/{route}', params={'path': path}).status_code in (400, 404)
    assert client.get('/api/jobs/missing/preview', params={'path': 'inputs/a.txt'}).status_code == 404


def test_an_output_sheet_names_the_uploaded_files_behind_each_row(client):
    job, source = upload(client, 'HR/Reports/employees.txt', b'EMP001')
    outputs = client.root / 'jobs' / job['id'] / 'outputs'
    outputs.mkdir(exist_ok=True)
    (outputs / 'filled.xlsx').write_bytes(workbook_bytes())
    sheet = get(client, job, 'outputs/filled.xlsx').json()['sheet']
    assert get(client, job, 'outputs/filled.xlsx').json()['row_sources'] == {}  # no record yet
    records = [
        {'file': 'filled.xlsx', 'sheet': sheet, 'rows': '2-3', 'sources': ['HR/Reports/employees.txt']},
        {'file': 'filled.xlsx', 'sheet': sheet, 'rows': 3, 'sources': ['/mnt/data/packet/elsewhere.pdf']},
        {
            'file': 'filled.xlsx',
            'sheet': sheet,
            'rows': '4-9999999999',
            'sources': ['/mnt/data/packet/HR/Reports/employees.txt'],
        },
        {'file': 'another.xlsx', 'sheet': sheet, 'rows': '9', 'sources': ['HR/Reports/employees.txt']},
        {'sheet': 'broken record'},
    ]
    (outputs / 'provenance.json').write_text(json.dumps(records), encoding='utf-8')
    found = get(client, job, 'outputs/filled.xlsx').json()['row_sources']
    assert found['2'] == [source] and found['3'] == [source, '/mnt/data/packet/elsewhere.pdf']
    # A path with the sandbox folder in front still finds the upload, and a huge range stops at the last row shown.
    assert found['4'] == [source] and found['500'] == [source] and len(found) == 499


def test_a_long_word_file_is_read_only_as_far_as_the_preview_shows(tmp_path, monkeypatch):
    from docx import Document

    from app import preview

    document = Document()
    for number in range(200):
        document.add_paragraph(f'Paragraph {number}')
    path = tmp_path / 'long.docx'
    document.save(str(path))
    monkeypatch.setattr(preview, 'MAX_CHARS', 100)
    text = preview._docx(path)
    assert text.startswith('Paragraph 0') and 100 < len(text) < 130
    monkeypatch.setattr(preview, 'MAX_DOCX_BYTES', 10)
    with pytest.raises(ValueError, match='too large'):
        preview._docx(path)
