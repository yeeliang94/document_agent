"""Shared fixtures: synthetic workbooks, job creation, polling and scripted models."""

import asyncio
import hashlib
import io
import time

import pytest
from openpyxl import Workbook
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel

from app.harness_tools import register_harness_tools
from app.state import identity
from app.tools import JobDeps, register_tools

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


def create_job(client, files=None, *, legacy=True):
    """Existing scenarios exercise legacy resume compatibility; new region cases opt out."""
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
    job = response.json()
    if legacy:
        client.app.state.store.state(job['id']).put('schema', 'accounting', {'version': 1})
    return job


def wait_for_job(client, job_id, statuses=('completed', 'partial', 'failed'), timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['status'] in statuses:
            return job
        time.sleep(0.03)
    pytest.fail(f'Job did not reach {statuses}: {job}')


def scripted(steps):
    """FunctionModel that plays (tool, args) steps in order and records each prompt it saw."""
    seen = []

    def respond(messages, info):
        seen.append(messages)
        name, args = steps[len(seen) - 1]
        return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f'step-{len(seen)}')])

    return FunctionModel(respond), seen


def final(status='complete', unresolved=()):
    return ('final_result', {'summary': 'Done.', 'status': status, 'unresolved': list(unresolved)})


def apply_step(*seqs):
    """The scripted agent settling user instructions that affect no earlier work."""
    return ('apply_instructions', {'instructions': list(seqs), 'reason': 'Applies to remaining work only'})


def completion_checks(path):
    source = identity(
        'inputs/HR/Reports/employees.txt', hashlib.sha256(b'EMP001, Alex Example, 4200, 2026-10-01').hexdigest()
    )
    return [
        ('read_document', {'path': 'inputs/HR/Reports/employees.txt'}),
        (
            'record_decisions',
            {
                'decisions': [
                    {
                        'id': source,
                        'scope': 'source',
                        'status': 'reviewed',
                        'reason': 'Synthetic source reviewed for this test',
                        'evidence': 'inputs/HR/Reports/employees.txt',
                    }
                ]
            },
        ),
        ('validate_output', {'path': path}),
    ]


def run_tool(store, job_id, name, args):
    results = []

    def respond(messages, info):
        results.extend(
            p.content for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)
        )
        if results:
            return ModelResponse(parts=[TextPart('ok')])
        return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id='tool')])

    agent = Agent(FunctionModel(respond), deps_type=JobDeps)
    register_tools(agent)
    register_harness_tools(agent)
    asyncio.run(agent.run('go', deps=JobDeps(store, job_id)))
    return results[0]
