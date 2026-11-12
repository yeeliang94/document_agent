"""Check, step by step, which ways of using OpenAI Code Interpreter an endpoint (for example a LiteLLM proxy) supports.
Sends only a generated two-row synthetic workbook. Costs a few cheap model calls.

Part A, explicit sandboxes: the harness creates the container, uploads into it, downloads from it and deletes it.
Part B, automatic sandboxes: OpenAI creates the container on the first code call. Files go in through the Files API,
and come out by download if the proxy allows it, otherwise as text the code prints, rebuilt here.

  Windows: check_proxy.bat        macOS/Linux: .venv/bin/python proxy_check.py

The endpoint, model and key come from OPENAI_BASE_URL, OPENAI_MODEL and OPENAI_API_KEY in the project .env, as for
the main app. sandbox_settings.json (optional) and flags such as --provider openai|azure and --model-on-create add the
LiteLLM options or override them for one run.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import re
import sys

from agents import Agent, CodeInterpreterTool, ModelSettings, Runner, set_tracing_disabled
from openai.types.shared import Reasoning
from openpyxl import Workbook, load_workbook

from sandbox_agent.config import Settings, add_endpoint_args, describe, load_settings, prepare_console_and_network

RESULTS: list[tuple[str, bool | None, str]] = []  # ok is None for a step that could not be tried


def record(step: str, ok: bool | None, detail: str = ''):
    RESULTS.append((step, ok, detail))
    label = 'SKIP' if ok is None else 'PASS' if ok else 'FAIL'
    print(f'{label}  {step}' + (f'  ({detail})' if detail else ''))


def skip(steps: list[str], needs: str):
    for step in steps:
        record(step, None, f'not tried: needs {needs}')


STRICT = ModelSettings(
    reasoning=Reasoning(effort='low'), store=False,
    response_include=['code_interpreter_call.outputs', 'reasoning.encrypted_content'],
)  # fmt: skip


def _agent(settings: Settings, container, model_settings) -> Agent:
    return Agent(
        name='check',
        model=settings.model_for(),
        instructions='Use the code interpreter to do exactly what the user asks, then reply in one short sentence.',
        tools=[CodeInterpreterTool(tool_config={'type': 'code_interpreter', 'container': container})],
        model_settings=model_settings,
    )


def _calls(result) -> list:
    return [
        i.raw_item
        for i in result.new_items
        if getattr(getattr(i, 'raw_item', None), 'type', '') == 'code_interpreter_call'
    ]


def _logs(calls) -> str:
    return '\n'.join(
        getattr(o, 'logs', '') or '' for call in calls for o in (call.outputs or []) if getattr(o, 'type', '') == 'logs'
    )


async def run_agent(settings: Settings, container_id, prompt, model_settings):
    return _calls(await Runner.run(_agent(settings, container_id, model_settings), prompt, max_turns=4))


def failed(step: str, error: Exception):
    record(step, False, f'{type(error).__name__}: {error}')


async def main() -> int:
    parser = argparse.ArgumentParser()
    add_endpoint_args(parser)
    args = parser.parse_args()
    # The same Settings and client the agent uses, so a pass here means the agent's calls will look the same.
    prepare_console_and_network()
    settings = load_settings(args)
    client = settings.client()
    set_tracing_disabled(True)
    print(describe(settings) + '\n')

    print('Part A: explicit sandboxes (what the agent uses today)')
    await part_a(settings, client)
    print('\nPart B: automatic sandboxes')
    await part_b(settings, client)
    return summary()


async def part_a(settings: Settings, client):
    step = 'A1 create a sandbox (POST /containers)'
    try:
        container = await client.containers.create(
            name='proxy-check', memory_limit='1g', expires_after={'anchor': 'last_active_at', 'minutes': 20},
            extra_body=settings.sandbox_create_extra,
        )  # fmt: skip
        record(step, True, container.id)
    except Exception as error:  # noqa: BLE001 - each step reports its own failure
        failed(step, error)
        return
    try:
        await steps(settings, client, container.id)
    finally:
        try:
            await client.containers.delete(container.id)
            record('A7 delete the sandbox', True)
        except Exception as error:  # noqa: BLE001
            failed('A7 delete the sandbox', error)


async def steps(settings: Settings, client, container_id: str):
    step = 'A2 upload a file into it'
    try:
        wb = Workbook()
        wb.active.append(['id', 'value'])
        wb.active.append(['A1', 41])
        buffer = io.BytesIO()
        wb.save(buffer)
        uploaded = await client.containers.files.create(container_id, file=('check.xlsx', buffer.getvalue()))
        record(step, True, uploaded.path)
    except Exception as error:  # noqa: BLE001
        failed(step, error)
        return
    task = (
        f'Open {uploaded.path} with openpyxl, add 1 to B2, save the workbook to /mnt/data/outputs/result.xlsx, '
        'and print the new value of B2.'
    )

    step = 'A3 run code in that sandbox (code_interpreter, explicit container)'
    try:
        calls = await run_agent(settings, container_id, task, ModelSettings())
        record(step, bool(calls), f'{len(calls)} code call(s)' if calls else 'the model answered without running code')
    except Exception as error:  # noqa: BLE001
        failed(step, error)

    step = 'A4 same with store=False and encrypted reasoning, returning code outputs'
    try:
        calls = await run_agent(settings, container_id, 'Print the sum of 2 and 3 with code.', STRICT)
        outputs = bool(calls and calls[0].outputs)
        record(step, outputs, 'outputs returned' if outputs else 'no code ran or no code outputs came back')
    except Exception as error:  # noqa: BLE001
        failed(step, error)

    step = 'A5 list files the model wrote'
    try:
        listing = [item async for item in client.containers.files.list(container_id, limit=100)]
        produced = [item for item in listing if item.path == '/mnt/data/outputs/result.xlsx']
        record(step, bool(produced), f'{len(listing)} file(s) listed')
    except Exception as error:  # noqa: BLE001
        failed(step, error)
        produced = []

    step = 'A6 download a file the model wrote'
    if not produced:
        record(step, False, 'nothing to download')
        return
    try:
        data = (await client.containers.files.content.retrieve(produced[0].id, container_id=container_id)).read()
        value = load_workbook(io.BytesIO(data)).active['B2'].value
        record(step, value == 42, f'B2 = {value!r}, expected 42')
    except Exception as error:  # noqa: BLE001
        failed(step, error)


async def part_b(settings: Settings, client):
    step = 'B1 upload a file through the Files API (POST /files)'
    try:
        uploaded = await client.files.create(file=('check.xlsx', _workbook(41)), purpose='user_data')
        record(step, True, uploaded.id)
    except Exception as error:  # noqa: BLE001
        failed(step, error)
        return
    try:
        await steps_b(settings, client, uploaded.id)
    finally:
        try:
            await client.files.delete(uploaded.id)
            record('B7 delete the uploaded file (DELETE /files)', True)
        except Exception as error:  # noqa: BLE001
            failed('B7 delete the uploaded file (DELETE /files)', error)


async def steps_b(settings: Settings, client, file_id: str):
    auto = {'type': 'auto', 'memory_limit': '1g', 'file_ids': [file_id]}
    agent = _agent(settings, auto, STRICT)
    step = 'B2 run code in an automatic sandbox that holds the uploaded file'
    task = (
        'Find check.xlsx under /mnt/data (its name may carry a prefix). Open it with openpyxl, add 1 to B2, save it '
        'to /mnt/data/outputs/result.xlsx and print the new value of B2.'
    )
    try:
        first = await Runner.run(agent, task, max_turns=4)
        calls = _calls(first)
        container_id = next((c.container_id for c in calls if getattr(c, 'container_id', None)), None)
        ok = bool(calls) and '42' in _logs(calls)
        record(
            step,
            ok,
            (f'sandbox {container_id}' if container_id else 'ran, but no sandbox id came back')
            if ok
            else f'{len(calls)} code call(s); printed: {_logs(calls)[-200:]!r}',
        )
        if not ok:
            skip(['B3', 'B4', 'B5', 'B6'], 'B2')
            return
    except Exception as error:  # noqa: BLE001
        failed(step, error)
        skip(['B3', 'B4', 'B5', 'B6'], 'B2')
        return

    step = 'B3 download a file the model wrote (GET /containers/.../files)'
    try:
        listing = [item async for item in client.containers.files.list(container_id, limit=100)]
        produced = [item for item in listing if item.path == '/mnt/data/outputs/result.xlsx']
        if not produced:
            record(step, False, f'result.xlsx is not in the listing ({len(listing)} file(s))')
        else:
            data = (await client.containers.files.content.retrieve(produced[0].id, container_id=container_id)).read()
            value = load_workbook(io.BytesIO(data)).active['B2'].value
            record(step, value == 42, f'B2 = {value!r}, expected 42')
    except Exception as error:  # noqa: BLE001
        failed(step, error)

    step = 'B4 next request reuses the same sandbox and its files'
    follow_up = first.to_input_list() + [
        {
            'role': 'user',
            'content': 'With code: print the base64 of /mnt/data/outputs/result.xlsx as text (use .decode()) between '
            'the lines BEGIN-B64 and END-B64, with nothing else on those lines.',
        }
    ]
    try:
        second = await Runner.run(agent, follow_up, max_turns=4)
        calls = _calls(second)
        same = container_id is not None and any(getattr(c, 'container_id', None) == container_id for c in calls)
        record(step, same, 'same sandbox' if same else f'sandboxes {[getattr(c, "container_id", None) for c in calls]}')
    except Exception as error:  # noqa: BLE001
        failed(step, error)
        skip(['B5', 'B6'], 'B4')
        return

    step = 'B5 get a file back as printed text'
    match = re.search(r"BEGIN-B64\s*\n\s*(?:b')?([A-Za-z0-9+/=\s]+?)'?\s*\n\s*END-B64", _logs(calls))
    try:
        value = load_workbook(io.BytesIO(base64.b64decode(''.join(match.group(1).split())))).active['B2'].value
        record(step, value == 42, f'B2 = {value!r}, expected 42')
    except Exception as error:  # noqa: BLE001
        failed(step, error if match else ValueError('no BEGIN-B64/END-B64 block in the printed output'))

    step = 'B6 measure how much printed text one code call returns'
    size = 400_000
    try:
        third = await Runner.run(
            agent,
            second.to_input_list() + [{'role': 'user', 'content': f"With code, run exactly: print('x' * {size})"}],
            max_turns=4,
        )
        returned = max((len(_logs([c])) for c in _calls(third)), default=0)
        record(
            step,
            returned > 0,
            f'{returned:,} characters per call, so about {returned * 3 // 4 // 1024} KB of file per call',
        )
    except Exception as error:  # noqa: BLE001
        failed(step, error)


def _workbook(value) -> bytes:
    wb = Workbook()
    wb.active.append(['id', 'value'])
    wb.active.append(['A1', value])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def summary() -> int:
    passed = {step.split()[0] for step, ok, _ in RESULTS if ok}
    untried = {step.split()[0] for step, ok, _ in RESULTS if ok is None}
    explicit = {'A1', 'A2', 'A3', 'A4', 'A5', 'A6'} <= passed
    auto_in = {'B1', 'B2'} <= passed
    auto_out = 'B3' in passed or 'B5' in passed
    print('\nSummary')
    print(f'  Explicit sandboxes (the agent today): {"work" if explicit else "do not work"}')
    print(f'  Automatic sandboxes, files in:        {"work" if auto_in else "do not work"}')
    print('  Automatic sandboxes, files out:       ' + (
        'by download' if 'B3' in passed else 'as printed text only' if 'B5' in passed
        else 'not tested' if {'B3', 'B5'} <= untried else 'do not work'))  # fmt: skip
    kept = 'yes' if 'B4' in passed else 'not tested' if 'B4' in untried else 'no'
    print(f'  Automatic sandbox kept between calls: {kept}')
    print('Send these lines back. FAIL lines carry the reason.')
    return 0 if explicit or (auto_in and auto_out) else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
