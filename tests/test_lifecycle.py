"""Starting, continuing, timing out and finishing runs."""

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from app.config import Settings
from app.main import create_app

from helpers import LOCAL, apply_step, completion_checks, create_job, final, scripted, wait_for_job


def test_partial_job_can_continue_with_new_instruction(tmp_path):
    model, seen = scripted(
        [
            final('partial', ['Need October allowances']),
            apply_step(1),
            ('create_output', {'filename': 'out.xlsx'}),
            ('validate_output', {'path': 'outputs/out.xlsx'}),
            *completion_checks('outputs/out.xlsx'),
            final(),
        ]
    )
    with TestClient(
        create_app(Settings(data_dir=tmp_path, api_key='k'), model_override=model), base_url=LOCAL
    ) as client:
        job = create_job(client)
        client.post(f'/api/jobs/{job["id"]}/start')
        assert wait_for_job(client, job['id'])['status'] == 'partial'
        assert (
            client.post(f'/api/jobs/{job["id"]}/message', json={'message': 'Allowances are zero.'}).status_code == 200
        )
        assert client.get(f'/api/jobs/{job["id"]}').json()['instructions'] == 'Fill Employees A2:D2.'
        assert client.post(f'/api/jobs/{job["id"]}/resume').status_code == 200
        assert wait_for_job(client, job['id'])['status'] == 'completed'
    prompts = [
        p.content for m in seen[1] if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, UserPromptPart)
    ]
    assert any('Need October allowances' in p and 'Allowances are zero.' in p for p in prompts)


def test_message_arriving_after_final_step_keeps_job_partial(tmp_path):
    import threading

    entered, release = threading.Event(), threading.Event()
    calls = []

    async def respond(messages, info):
        calls.append(1)
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart('create_output', {'filename': 'out.xlsx'}, tool_call_id='late-1')])
        if len(calls) == 2:
            return ModelResponse(
                parts=[ToolCallPart('validate_output', {'path': 'outputs/out.xlsx'}, tool_call_id='late-2')]
            )
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return ModelResponse(parts=[ToolCallPart(*final(), tool_call_id='late-final')])

    with TestClient(
        create_app(Settings(data_dir=tmp_path, api_key='k'), model_override=FunctionModel(respond)), base_url=LOCAL
    ) as client:
        job = create_job(client)
        client.post(f'/api/jobs/{job["id"]}/start')
        assert entered.wait(timeout=3)
        client.post(f'/api/jobs/{job["id"]}/message', json={'message': 'Also add EMP002.'})
        release.set()
        done = wait_for_job(client, job['id'])
        assert done['status'] == 'partial', done
        assert 'Resume to apply them' in done['summary']


def test_job_timeout_reports_a_readable_error(tmp_path):
    async def respond(messages, info):
        await asyncio.sleep(5)

    settings = Settings(data_dir=tmp_path, api_key='k', job_timeout=1)
    with TestClient(create_app(settings, model_override=FunctionModel(respond)), base_url=LOCAL) as client:
        job = create_job(client)
        client.post(f'/api/jobs/{job["id"]}/start')
        failed = wait_for_job(client, job['id'], timeout=5)
        assert failed['status'] == 'failed'
        assert 'DI_JOB_TIMEOUT_SECONDS' in failed['error']


def test_completion_uses_the_validated_workbook_among_several(tmp_path):
    model, _ = scripted(
        [
            ('create_output', {'filename': 'a_draft.xlsx'}),
            ('create_output', {'filename': 'z_output.xlsx'}),
            ('validate_output', {'path': './outputs/z_output.xlsx'}),
            *completion_checks('outputs/z_output.xlsx'),
            final(),
        ]
    )
    with TestClient(
        create_app(Settings(data_dir=tmp_path, api_key='k'), model_override=model), base_url=LOCAL
    ) as client:
        job = create_job(client)
        client.post(f'/api/jobs/{job["id"]}/start')
        assert wait_for_job(client, job['id'])['status'] == 'completed'


@pytest.mark.parametrize('instruction_state', ['running', 'partial'])
def test_instructions_survive_model_failure_and_resume(tmp_path, instruction_state):
    entered, release = threading.Event(), threading.Event()
    seen = []
    instruction = 'Exclude EMP002 because their payment was cancelled.'

    async def respond(messages, info):
        seen.append(ModelMessagesTypeAdapter.dump_json(messages).decode())
        if len(seen) == 1:
            if instruction_state == 'partial':
                return ModelResponse(
                    parts=[ToolCallPart(*final('partial', ['Need a decision']), tool_call_id='initial')]
                )
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return ModelResponse(parts=[ToolCallPart('list_files', {}, tool_call_id='initial')])
        if len(seen) == 2:
            raise RuntimeError('Synthetic model connection failure')
        return ModelResponse(parts=[ToolCallPart(*final('partial', ['Test stops here']), tool_call_id='resumed')])

    app = create_app(Settings(data_dir=tmp_path, api_key='test'), model_override=FunctionModel(respond))
    with TestClient(app, base_url=LOCAL) as client:
        job = create_job(client)
        url = f'/api/jobs/{job["id"]}'
        client.post(url + '/start')
        if instruction_state == 'partial':
            assert wait_for_job(client, job['id'])['status'] == 'partial'
        else:
            assert entered.wait(timeout=3)
        assert client.post(url + '/message', json={'message': instruction}).status_code == 200
        if instruction_state == 'partial':
            assert client.post(url + '/resume').status_code == 200
        else:
            release.set()
        assert wait_for_job(client, job['id'])['status'] == 'failed'
        checkpoint = (tmp_path / 'jobs' / job['id'] / 'working/history.json').read_text()
        assert instruction in checkpoint
        assert client.post(url + '/resume').status_code == 200
        assert wait_for_job(client, job['id'])['status'] == 'partial'
    assert seen[-1].count(instruction) == 1


def test_other_timeouts_are_not_reported_as_the_job_limit(tmp_path):
    async def respond(messages, info):
        raise TimeoutError('upstream gateway timed out')

    settings = Settings(data_dir=tmp_path, api_key='k', job_timeout=60)
    with TestClient(create_app(settings, model_override=FunctionModel(respond)), base_url=LOCAL) as client:
        job = create_job(client)
        client.post(f'/api/jobs/{job["id"]}/start')
        failed = wait_for_job(client, job['id'], timeout=5)
        assert failed['status'] == 'failed'
        assert 'DI_JOB_TIMEOUT_SECONDS' not in failed['error']
        assert 'upstream gateway timed out' in failed['error']


def test_pause_during_final_answer_keeps_the_finished_result(tmp_path):
    entered, release, calls = threading.Event(), threading.Event(), []

    async def respond(messages, info):
        calls.append(len(calls))
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return ModelResponse(
            parts=[ToolCallPart('final_result', {'summary': 'All done.', 'status': 'partial', 'unresolved': ['x']})]
        )

    app = create_app(Settings(data_dir=tmp_path, api_key='k'), model_override=FunctionModel(respond))
    with TestClient(app, base_url=LOCAL) as client:
        job = create_job(client)
        client.post(f'/api/jobs/{job["id"]}/start')
        assert entered.wait(timeout=3)
        assert client.post(f'/api/jobs/{job["id"]}/pause').status_code == 200
        release.set()
        finished = wait_for_job(client, job['id'], statuses=('partial', 'paused', 'failed'))
        assert finished['status'] == 'partial', finished
        assert finished['summary'].startswith('All done.')
        assert len(calls) == 1
