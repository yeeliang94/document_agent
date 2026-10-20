"""Killable tool execution using a fresh interpreter on Windows and Unix alike."""

import asyncio
import os
import pickle
import sys
from pathlib import Path

from .tools import job_lock

ISOLATED_TOOLS = {
    'inspect_source',
    'inspect_workbook',
    'read_sheet',
    'read_document',
    'view_page',
    'extract_dataset',
    'transform_dataset',
    'populate_template',
    'write_cells',
    'validate_output',
    'validate_job',
    'extract_region',
}
# Killing these cannot leave a half-applied change, so a deadline is an ordinary tool error.
READ_ONLY_TOOLS = {'inspect_workbook', 'read_sheet', 'read_document', 'view_page', 'validate_output', 'validate_job'}


async def execute_tool(ctx, name, kwargs):
    deps = ctx.deps
    lock = job_lock(deps.job_id)
    # Do not leave a pending thread acquiring a lock after cancellation.
    while not lock.acquire(blocking=False):
        deps.check_stop()
        await asyncio.sleep(0.05)
    process = None
    deadline = deps.settings.tool_timeout if deps.settings else 120
    try:
        deps.check_stop()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            '-m',
            'app.operation_worker',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).resolve().parent.parent),
            env=os.environ | {'DI_OPERATION_WORKER': '1'},
        )
        payload = pickle.dumps((deps.store.root, deps.job_id, deps.settings, deps.actor, deps.scope, name, kwargs))
        async with asyncio.timeout(deadline):
            output, errors = await process.communicate(payload)
        if process.returncode:
            raise RuntimeError('Isolated operation failed: ' + errors.decode(errors='replace')[-1000:])
        return pickle.loads(output)
    except TimeoutError:
        if name in READ_ONLY_TOOLS:
            raise TimeoutError(f'{name} exceeded its {deadline}s deadline; narrow the request') from None
        # A killed write may have prepared/committed its journal. Never report it as safely retried.
        deps.store.update(
            deps.job_id, status='pausing', error='Tool deadline exceeded; inspect saved operations before resuming.'
        )
        deps.store.event(
            deps.job_id,
            'tool.deadline',
            'Stopped isolated operation at its deadline.',
            type='tool',
            level='WARN',
            tool=name,
            error_class='uncertain_write',
        )
        raise InterruptedError('Tool deadline exceeded; saved journals require reconciliation') from None
    finally:
        if process and process.returncode is None:
            process.kill()
            # Reap before releasing the mutation lock, even on repeated cancellation.
            reaper = asyncio.create_task(process.wait())
            while not reaper.done():
                try:
                    await asyncio.shield(reaper)
                except asyncio.CancelledError:
                    continue
        lock.release()
