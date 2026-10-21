"""Long user instructions can be restored in full, and an identical reread loop still pauses."""

import asyncio

from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from app.config import Settings
from app.runtime import Runner
from app.store import Store


def test_identical_instruction_rereads_pause_the_job(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'Prepare the output', '')
    store.state(job['id']).instruction('Rule to reload. ' * 4000)
    calls = []

    def respond(messages, info):
        args = {'kind': 'instruction', 'key': '1', 'offset': 0, 'limit': 4000}
        calls.append(args)
        return ModelResponse(parts=[ToolCallPart('read_job_state', args, tool_call_id=str(len(calls)))])

    asyncio.run(Runner(store, Settings(data_dir=tmp_path), FunctionModel(respond)).run(job['id']))
    assert store.get(job['id'])['status'] == 'paused'
    assert len(calls) <= 6


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
