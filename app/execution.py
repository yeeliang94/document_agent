"""Killable tool execution using a fresh interpreter on Windows and Unix alike."""

import asyncio
import os
import pickle
import sys
from dataclasses import dataclass
from pathlib import Path

from .tools import job_lock

ISOLATED_TOOLS = {
    'inspect_source',
    'read_sheet',
    'read_cell',
    'read_document',
    'view_page',
    'extract_dataset',
    'transform_dataset',
    'populate_template',
    'write_cells',
    'validate_output',
    'extract_region',
}
# Killing these cannot leave a half-applied change, so a deadline is an ordinary tool error.
READ_ONLY_TOOLS = {
    'read_cell',
    'read_sheet',
    'read_document',
    'view_page',
    'validate_output',
}


@dataclass
class Finished:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int


async def acquire_job_lock(deps):
    lock = job_lock(deps.job_id)
    # Do not leave a pending thread acquiring a lock after cancellation.
    while not lock.acquire(blocking=False):
        deps.check_stop()
        await asyncio.sleep(0.05)
    return lock


async def _bounded(stream, limit, tail):
    """Drain a stream fully but keep only `limit` bytes, so a chatty child cannot exhaust memory."""
    kept, total = bytearray(), 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        kept += chunk
        if len(kept) > limit:
            kept = kept[-limit:] if tail else kept[:limit]
    return bytes(kept), total


async def run_killable(argv, payload, deadline, *, cwd, env, limit=None):
    """Run a child with `payload` on stdin; it is killed and reaped before this returns or raises."""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        async with asyncio.timeout(deadline):
            if limit is None:
                output, errors = await process.communicate(payload)
                return Finished(process.returncode, output, errors, len(output), len(errors))

            async def feed():
                try:
                    process.stdin.write(payload)
                    await process.stdin.drain()
                    process.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The child exited without reading its input; its exit code reports why.

            _, (output, out_total), (errors, err_total) = await asyncio.gather(
                feed(), _bounded(process.stdout, limit, False), _bounded(process.stderr, limit, True)
            )
            await process.wait()
            return Finished(process.returncode, output, errors, out_total, err_total)
    finally:
        if process and process.returncode is None:
            process.kill()
            # Reap before the caller releases the mutation lock, even on repeated cancellation.
            reaper = asyncio.create_task(process.wait())
            while not reaper.done():
                try:
                    await asyncio.shield(reaper)
                except asyncio.CancelledError:
                    continue


async def execute_tool(ctx, name, kwargs):
    deps = ctx.deps
    lock = await acquire_job_lock(deps)
    deadline = deps.settings.tool_timeout if deps.settings else 120
    try:
        deps.check_stop()
        payload = pickle.dumps((deps.store.root, deps.job_id, deps.settings, deps.actor, deps.scope, name, kwargs))
        finished = await run_killable(
            [sys.executable, '-m', 'app.operation_worker'],
            payload,
            deadline,
            cwd=str(Path(__file__).resolve().parent.parent),
            env=os.environ | {'DI_OPERATION_WORKER': '1'},
        )
        if finished.returncode:
            raise RuntimeError('Isolated operation failed: ' + finished.stderr.decode(errors='replace')[-1000:])
        return pickle.loads(finished.stdout)
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
        lock.release()
