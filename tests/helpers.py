"""Shared fixtures: a synthetic workbook, job creation and polling."""

import io
import time

from openpyxl import Workbook

LOCAL = 'http://127.0.0.1'


def workbook_bytes():
    workbook = Workbook()
    workbook.active.title = 'Employees'
    workbook.active.append(['Employee ID', 'Name', 'Salary', 'Effective Date', 'Annual Salary'])
    workbook.active['E2'] = '=C2*12'
    workbook.create_sheet('Lookup').sheet_state = 'hidden'
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def create_job(client, files=None):
    response = client.post(
        '/api/jobs',
        data={
            'module': 'payroll',
            'period': '2026-10',
            'instructions': 'Fill Employees A2:D2.',
            'context': 'Synthetic test company',
        },
        files=[
            (
                'template',
                (
                    'template.xlsx',
                    workbook_bytes(),
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                ),
            ),
            *(
                files
                if files is not None
                else [('files', ('HR/Reports/employees.txt', b'EMP001, Alex Example, 4200, 2026-10-01', 'text/plain'))]
            ),
        ],
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def wait_for_job(client, job_id, statuses=('completed', 'partial', 'failed'), timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['status'] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f'Job did not reach {statuses}: {job}')
