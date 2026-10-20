"""Instruction restoration uses bounded credit without resetting stalled business work."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage

from app.config import Settings
from app.context import ContextPolicy, request_retry
from app.runtime import Runner
from app.state import JobState
from app.store import Store
from app.supervisor import Supervisor
from app.tools import JobDeps

from helpers import run_tool


@pytest.mark.parametrize('idle_turns', [0, 4, 5])
def test_runtime_restores_long_applied_and_pending_instructions_before_stalling(tmp_path, idle_turns):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'Prepare the output', '')
    state = store.state(job['id'])
    state.instruction('Earlier binding rule. ' * 2400)
    state.apply_instructions([1], 'Still applies')
    state.instruction('Newest pending requirement.')
    supervisor = Supervisor(state)
    supervisor.instructions(2)
    for _ in range(idle_turns):
        supervisor.turn()
    recovery = state.get('supervision', 'main')['recovery']
    pages = [
        {'kind': 'instruction', 'offset': 0, 'limit': 1},
        {'kind': 'instruction', 'offset': 1, 'limit': 1},
        *[
            {'kind': 'instruction', 'key': '1', 'offset': offset, 'limit': 4000}
            for offset in range(0, len(state.instructions()[0]['text']), 4000)
        ],
        {'kind': 'instruction', 'key': '2', 'offset': 0, 'limit': 4000},
    ]
    calls = []

    def respond(messages, info):
        if len(calls) == len(pages):
            clock = state.get('supervision', 'main')
            assert clock['turn'] == idle_turns
            assert clock['recovery'] == recovery
        args = pages[len(calls)] if len(calls) < len(pages) else {'kind': 'snapshot'}
        calls.append(args)
        return ModelResponse(parts=[ToolCallPart('read_job_state', args, tool_call_id=str(len(calls)))])

    settings = Settings(data_dir=tmp_path, max_requests=60)
    asyncio.run(Runner(store, settings, FunctionModel(respond)).run(job['id']))
    assert calls[: len(pages)] == pages
    assert len(pages) < len(calls) <= len(pages) + settings.stall_turns + settings.recovery_turns
    assert store.get(job['id'])['status'] == 'paused'


@pytest.mark.parametrize('overlap', [False, True])
def test_runtime_repeated_or_shifted_instruction_reads_still_pause(tmp_path, overlap):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'Prepare the output', '')
    state = store.state(job['id'])
    state.instruction('Rule to reload. ' * 4000)
    calls = []

    def respond(messages, info):
        args = {'kind': 'instruction', 'key': '1', 'offset': len(calls) if overlap else 0, 'limit': 4000}
        calls.append(args)
        return ModelResponse(parts=[ToolCallPart('read_job_state', args, tool_call_id=str(len(calls)))])

    asyncio.run(Runner(store, Settings(data_dir=tmp_path), FunctionModel(respond)).run(job['id']))
    assert store.get(job['id'])['status'] == 'paused'
    assert len(calls) <= 7
    assert state.get('supervision', 'main')['restoration_turns'] == 1


def test_instruction_coverage_survives_restart_and_application(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'Prepare the output', '')
    state = store.state(job['id'])
    state.instruction('Original rule. ' * 1000)
    supervisor = Supervisor(state)
    supervisor.instructions(1)
    args = {'kind': 'instruction', 'key': '1', 'offset': 0, 'limit': 4000}
    run_tool(store, job['id'], 'read_job_state', args)
    assert supervisor.turn()[1]['turn'] == 0
    state.apply_instructions([1], 'Acknowledged')
    revision = state.revision()
    restarted = Supervisor(JobState(store.path(job['id'])))
    # Applied status and request range variations do not make already delivered text new.
    for offset in range(6):
        result = run_tool(
            store,
            job['id'],
            'read_job_state',
            args | {'offset': offset, 'limit': 4000 - offset},
        )
        assert result['value']['applied']
        decision, clock = restarted.turn()
    assert decision == 'pause'
    assert clock['restoration_turns'] == 1
    assert state.revision() == revision
    # Another normal resume cannot erase that stall history.
    assert not restarted.instructions(1)
    assert restarted.turn()[0] == 'pause'


def test_many_instruction_index_pages_have_finite_credit(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'Prepare the output', '')
    state = store.state(job['id'])
    for number in range(12):
        state.instruction(f'Binding rule {number}')
    supervisor = Supervisor(state)
    supervisor.instructions(12)
    for offset in range(12):
        run_tool(store, job['id'], 'read_job_state', {'kind': 'instruction', 'offset': offset, 'limit': 1})
        assert supervisor.turn()[0] == 'continue'
    assert state.get('supervision', 'main')['restoration_turns'] == 12
    # Different index page shapes, missing IDs, and empty text ranges cannot renew coverage.
    for args in [
        {'kind': 'instruction', 'offset': 0, 'limit': 12},
        {'kind': 'instruction', 'offset': 1, 'limit': 11},
        {'kind': 'instruction', 'offset': 2, 'limit': 10},
        {'kind': 'instruction', 'key': '999'},
        {'kind': 'instruction', 'key': '1', 'offset': 1000},
        {'kind': 'snapshot'},
    ]:
        run_tool(store, job['id'], 'read_job_state', args)
        decision, _ = supervisor.turn()
    assert decision == 'pause'


def test_runtime_follows_byte_limited_unicode_instruction_pages(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'Prepare the output', '')
    text = '🧾' * 900
    store.state(job['id']).instruction(text)
    chunks = []

    def respond(messages, info):
        previous = next((part.content for part in messages[-1].parts if isinstance(part, ToolReturnPart)), None)
        offset = 0
        if previous:
            assert not previous.get('truncated')
            chunks.append(previous['value']['text'])
            offset = previous['next_offset']
        if offset is None:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        'final_result',
                        {'summary': 'Restored instructions.', 'status': 'partial', 'unresolved': ['Test ends here']},
                    )
                ]
            )
        return ModelResponse(
            parts=[ToolCallPart('read_job_state', {'kind': 'instruction', 'key': '1', 'offset': offset, 'limit': 4000})]
        )

    settings = Settings(data_dir=tmp_path, tool_result_bytes=1024)
    asyncio.run(Runner(store, settings, FunctionModel(respond)).run(job['id']))
    assert len(chunks) > 10
    assert ''.join(chunks) == text
    assert store.get(job['id'])['status'] == 'partial'


def test_actual_context_rebuild_allows_one_restoration_pass_and_restart_retains_dedup(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'Prepare the output', '')
    state = store.state(job['id'])
    state.instruction('Binding rule. ' * 200)
    supervisor = Supervisor(state)
    supervisor.instructions(1)
    args = {'kind': 'instruction', 'key': '1', 'offset': 0, 'limit': 4000}
    original = run_tool(store, job['id'], 'read_job_state', args)
    assert supervisor.turn()[1]['turn'] == 0
    run_tool(store, job['id'], 'read_job_state', args | {'offset': 1})
    assert supervisor.turn()[1]['turn'] == 1
    for _ in range(4):
        supervisor.turn()
    before = state.get('supervision', 'main')
    settings = Settings(data_dir=tmp_path, context_tokens=10000, response_tokens=100)
    deps = JobDeps(store, job['id'], settings)
    history = [ModelRequest(parts=[UserPromptPart('Original task')])]
    for number in range(10):
        history.extend(
            [
                ModelResponse(parts=[ToolCallPart('read_job_state', args, tool_call_id=str(number))]),
                ModelRequest(parts=[ToolReturnPart('read_job_state', original, tool_call_id=str(number))]),
            ]
        )
    model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart('Summary')]))
    ctx = SimpleNamespace(deps=deps, messages=history.copy(), usage=RunUsage())
    request = ModelRequestContext(
        model=model, messages=history, model_settings={}, model_request_parameters=ModelRequestParameters()
    )
    request_retry(state, 'main')
    rebuilt = asyncio.run(ContextPolicy(Runner(store, settings, model)).before_model_request(ctx, request))
    assert len(rebuilt.messages) < len(history)
    assert state.get('context', 'main')['archive']
    run_tool(store, job['id'], 'read_job_state', args)
    decision, clock = supervisor.turn()
    assert decision == 'continue'
    assert clock['turn'] == before['turn']
    assert clock['recovery'] == before['recovery']
    restarted = Supervisor(JobState(store.path(job['id'])))
    run_tool(store, job['id'], 'read_job_state', args | {'offset': 2})
    assert restarted.turn()[0] == 'pause'
