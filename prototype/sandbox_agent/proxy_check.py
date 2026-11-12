"""Check, step by step, whether an OpenAI-compatible endpoint (for example a LiteLLM proxy) supports everything the
sandbox agent needs. Sends only a generated two-row synthetic workbook. Costs one or two cheap model calls.

  Windows: check_proxy.bat        macOS/Linux: .venv/bin/python proxy_check.py

The endpoint and model come from sandbox_settings.json; flags such as --base-url, --model, --provider openai|azure
and --model-on-create override it for one run. The key comes from OPENAI_API_KEY in the project .env, or
PROXY_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys

from agents import Agent, CodeInterpreterTool, ModelSettings, Runner, set_tracing_disabled
from openai.types.shared import Reasoning
from openpyxl import Workbook, load_workbook

from sandbox_agent.config import Settings, add_endpoint_args, load_settings, prepare_console_and_network

RESULTS: list[tuple[str, bool, str]] = []


def record(step: str, ok: bool, detail: str = ''):
    RESULTS.append((step, ok, detail))
    print(f'{"PASS" if ok else "FAIL"}  {step}' + (f'  ({detail})' if detail else ''))


async def run_agent(settings: Settings, container_id, prompt, model_settings):
    agent = Agent(
        name='check',
        model=settings.model_for(),
        instructions='Use the code interpreter to do exactly what the user asks, then reply in one short sentence.',
        tools=[CodeInterpreterTool(tool_config={'type': 'code_interpreter', 'container': container_id})],
        model_settings=model_settings,
    )
    result = await Runner.run(agent, prompt, max_turns=4)
    return [i for i in result.new_items if getattr(getattr(i, 'raw_item', None), 'type', '') == 'code_interpreter_call']


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
    print(f'Endpoint {settings.base_url}, model {settings.model}\n')

    step = '1 create a sandbox (POST /containers)'
    try:
        container = await client.containers.create(
            name='proxy-check', memory_limit='1g', expires_after={'anchor': 'last_active_at', 'minutes': 20},
            extra_body=settings.sandbox_create_extra,
        )  # fmt: skip
        record(step, True, container.id)
    except Exception as error:  # noqa: BLE001 - each step reports its own failure
        failed(step, error)
        return summary()

    try:
        await steps(settings, client, container.id)
    finally:
        try:
            await client.containers.delete(container.id)
            record('7 delete the sandbox', True)
        except Exception as error:  # noqa: BLE001
            failed('7 delete the sandbox', error)
    return summary()


async def steps(settings: Settings, client, container_id: str):
    step = '2 upload a file into it'
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

    step = '3 run code in that sandbox (code_interpreter, explicit container)'
    try:
        calls = await run_agent(settings, container_id, task, ModelSettings())
        record(step, bool(calls), f'{len(calls)} code call(s)' if calls else 'the model answered without running code')
    except Exception as error:  # noqa: BLE001
        failed(step, error)

    step = '4 same with store=False and encrypted reasoning, returning code outputs'
    strict = ModelSettings(
        reasoning=Reasoning(effort='low'), store=False,
        response_include=['code_interpreter_call.outputs', 'reasoning.encrypted_content'],
    )  # fmt: skip
    try:
        calls = await run_agent(settings, container_id, 'Print the sum of 2 and 3 with code.', strict)
        outputs = bool(calls and calls[0].raw_item.outputs)
        record(step, outputs, 'outputs returned' if outputs else 'no code ran or no code outputs came back')
    except Exception as error:  # noqa: BLE001
        failed(step, error)

    step = '5 list files the model wrote'
    try:
        listing = [item async for item in client.containers.files.list(container_id, limit=100)]
        produced = [item for item in listing if item.path == '/mnt/data/outputs/result.xlsx']
        record(step, bool(produced), f'{len(listing)} file(s) listed')
    except Exception as error:  # noqa: BLE001
        failed(step, error)
        produced = []

    step = '6 download a file the model wrote'
    if not produced:
        record(step, False, 'nothing to download')
        return
    try:
        data = (await client.containers.files.content.retrieve(produced[0].id, container_id=container_id)).read()
        value = load_workbook(io.BytesIO(data)).active['B2'].value
        record(step, value == 42, f'B2 = {value!r}, expected 42')
    except Exception as error:  # noqa: BLE001
        failed(step, error)


def summary() -> int:
    failed = [step for step, ok, _ in RESULTS if not ok]
    print('\n' + ('All checks passed: the sandbox agent can use this endpoint.' if not failed else
                  f'{len(failed)} check(s) failed. The sandbox agent needs all of them.'))  # fmt: skip
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
