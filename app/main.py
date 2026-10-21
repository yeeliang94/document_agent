# Establish OS certificate trust before importing/constructing network clients.
import truststore

truststore.inject_into_ssl()
import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from openpyxl import load_workbook
from pydantic import BaseModel, Field

from . import jobstatus
from .config import ROOT, Settings, settings
from .context import estimate_text
from .files import add_file, add_zip, is_junk, within
from .runtime import Runner
from .store import DEFAULT_CONTEXT, MODULE_LABELS, Store, clean

VERSION = '0.2.0'
MAX_TEXT_CHARS = 30000


class TextInput(BaseModel):
    context: str = Field(default='', max_length=MAX_TEXT_CHARS)
    answer: str = Field(default='', max_length=MAX_TEXT_CHARS)
    message: str = Field(default='', max_length=MAX_TEXT_CHARS)


def mark_duplicates(manifest):
    """Link files with identical content to the first copy, so the agent reads it once."""
    first_seen = {}
    for entry in manifest:
        if entry['sha256'] in first_seen:
            entry['duplicate_of'] = first_seen[entry['sha256']]
        else:
            first_seen[entry['sha256']] = entry['path']


def last_event_id(request: Request) -> int:
    try:
        return int(request.headers.get('last-event-id', '0'))
    except ValueError:
        return 0


def create_app(settings_override: Settings | None = None, model_override=None):
    conf = settings_override or settings
    conf.validate()
    store = Store(conf.data_dir)
    runner = Runner(store, conf, model_override)

    @asynccontextmanager
    async def lifespan(app):
        await runner.start()
        yield
        await runner.close()

    api = FastAPI(title='Document Intelligence', lifespan=lifespan)
    api.state.store = store
    api.state.runner = runner
    api.state.settings = conf

    trusted_hosts = conf.trusted_hosts()
    allowed_origins = {o.rstrip('/') for o in conf.allowed_origins}

    @api.middleware('http')
    async def boundary(request: Request, call_next):
        # A DNS-rebinding page sends its own name as Host, so reject unknown hosts.
        try:
            host = urlparse('//' + request.headers.get('host', '')).hostname or ''
        except ValueError:
            host = ''
        if '*' not in trusted_hosts and host not in trusted_hosts:
            return JSONResponse({'detail': 'Unknown host name. Add it to DI_ALLOWED_HOSTS.'}, 400)
        if request.url.path.startswith('/api/'):
            origin = (request.headers.get('origin') or '').rstrip('/')
            if origin and origin != str(request.base_url).rstrip('/') and origin not in allowed_origins:
                return JSONResponse({'detail': 'Cross-origin requests are not allowed.'}, 403)
            if conf.access_token:
                token = request.headers.get('authorization', '').removeprefix('Bearer ')
                if not hmac.compare_digest(token.encode(), conf.access_token.encode()):
                    return JSONResponse({'detail': 'Access token required.'}, 401)
        return await call_next(request)

    def get(job_id):
        try:
            return store.get(job_id)
        except (KeyError, ValueError):
            raise HTTPException(404, 'Job not found') from None

    @api.get('/health/readiness')
    async def readiness():
        return {
            'app': 'document-intelligence',
            'ready': True,
            'openai_configured': bool(conf.api_key),
            'version': VERSION,
            'tokenizer_method': estimate_text('startup health check')[1],
            'index_version': 1,
            'pid': os.getpid(),
            'instance_id': hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()[:16],
        }

    @api.get('/api/config')
    async def config():
        return {
            'configured': bool(conf.api_key),
            'model': conf.model,
            'base_url': conf.base_url,
            'version': VERSION,
            'modules': [
                {'id': module, 'label': MODULE_LABELS[module], 'context': store.profile(module)}
                for module in DEFAULT_CONTEXT
            ],
            'statuses': jobstatus.as_json(),
            'harness': {
                'max_subagents': conf.max_subagents,
                'context_token_budget': conf.context_tokens,
                'tool_result_bytes': conf.tool_result_bytes,
                'tool_deadline_seconds': conf.tool_timeout,
                'tokenizer_method': estimate_text('capability diagnostics')[1],
                'accounting_version': 2,
                'index_version': 1,
            },
        }

    @api.get('/api/modules/{module}')
    async def module_get(module):
        if module not in DEFAULT_CONTEXT:
            raise HTTPException(404, 'Unknown module')
        return {'id': module, 'context': store.profile(module)}

    @api.put('/api/modules/{module}')
    async def module_update(module, body: TextInput):
        if module not in DEFAULT_CONTEXT:
            raise HTTPException(404, 'Unknown module')
        store.save_profile(module, body.context)
        return {'id': module, 'context': body.context}

    @api.get('/api/jobs')
    async def jobs():
        return store.summaries()

    @api.post('/api/jobs', status_code=201)
    async def upload(
        module: str = Form(...),
        period: str = Form(...),
        instructions: str = Form(''),
        context: str = Form(''),
        template: UploadFile = File(...),
        files: list[UploadFile] = File(...),
    ):
        if module not in DEFAULT_CONTEXT:
            raise HTTPException(400, 'Unknown module')
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
            raise HTTPException(400, 'Use a reporting month in YYYY-MM format')
        if not template.filename or not template.filename.lower().endswith('.xlsx'):
            raise HTTPException(400, 'Choose an .xlsx output template')
        if not files:
            raise HTTPException(400, 'Choose source files')
        if len(instructions) > MAX_TEXT_CHARS or len(context) > MAX_TEXT_CHARS:
            raise HTTPException(400, 'Instructions are too long')
        job_id = store.create(module, period, instructions, context or store.profile(module))['id']
        try:
            manifest = await save_uploads(store.path(job_id), template, files)
            mark_duplicates(manifest)
            store.update(job_id, files=manifest)
            store.state(job_id).put('schema', 'accounting', {'version': 2, 'index_version': 1})
            store.state(job_id).inventory(manifest)
            store.event(
                job_id, 'files.received', f'{len(manifest) - 1} source files received', type='job', files=manifest
            )
            return store.get(job_id)
        except Exception as error:
            # Nothing usable was stored, so leave no empty failed run in history.
            shutil.rmtree(store.path(job_id), ignore_errors=True)
            store.delete(job_id)
            raise HTTPException(400, clean(str(error))) from None
        finally:
            await template.close()
            for f in files:
                await f.close()

    async def save_uploads(root, template, files):
        """Write the template and source files (expanding ZIPs) into the job folder; returns the manifest."""
        manifest = []
        total = 0
        limit = conf.max_upload_mb * 1024 * 1024
        for upload, role in [(template, 'template')] + [(f, 'inputs') for f in files]:
            name = upload.filename or ''
            if role == 'inputs' and is_junk(name):
                continue
            data = await upload.read(limit - total + 1)
            total += len(data)
            if total > limit:
                raise ValueError(f'Upload exceeds {conf.max_upload_mb} MB')
            if not data:
                raise ValueError('Empty file: ' + (name or 'unnamed'))
            if role == 'inputs' and name.lower().endswith('.zip'):
                stored = sum(entry['size'] for entry in manifest)
                add_zip(root, name, data, manifest, conf.max_files, limit - stored)
            else:
                add_file(root, role, name or 'unnamed', data, manifest)
            if len(manifest) > conf.max_files or sum(entry['size'] for entry in manifest) > limit:
                raise ValueError('Expanded submission exceeds configured file or size limits')
        if not any(entry['role'] == 'inputs' for entry in manifest):
            raise ValueError('Choose source files')
        # Fail early for corrupt/encrypted/non-workbook template.
        load_workbook(root / manifest[0]['path'], read_only=True).close()
        return manifest

    @api.get('/api/jobs/{job_id}')
    async def job(job_id):
        return get(job_id)

    @api.post('/api/jobs/{job_id}/start')
    @api.post('/api/jobs/{job_id}/resume')
    async def start(job_id):
        j = get(job_id)
        if not conf.api_key and model_override is None:
            raise HTTPException(503, 'Add OPENAI_API_KEY to .env and restart the application.')
        if j['status'] not in jobstatus.STARTABLE:
            raise HTTPException(409, 'This job cannot start in its current state')
        if runner.active == job_id:
            raise HTTPException(409, 'The previous operation is still finishing')
        if not j['files']:
            raise HTTPException(400, 'No usable input files')
        # A partial run already ended; the next run needs a fresh prompt to continue.
        if j['status'] == 'partial':
            store.update(job_id, continue_run=True)
        await runner.submit(job_id)
        return get(job_id)

    @api.post('/api/jobs/{job_id}/pause')
    async def pause(job_id):
        j = get(job_id)
        if j['status'] not in jobstatus.PAUSABLE:
            raise HTTPException(409, 'Job is not running')
        store.mutate(job_id, lambda job: job.update(status='pausing' if job['status'] == 'running' else 'paused'))
        store.event(job_id, 'job.pause_requested', 'Pause requested; completing current operation.', type='job')
        return get(job_id)

    @api.post('/api/jobs/{job_id}/cancel')
    async def cancel(job_id):
        j = get(job_id)
        if j['status'] in jobstatus.FINISHED:
            raise HTTPException(409, 'Job has already finished')
        store.update(job_id, status='cancelled')
        store.event(job_id, 'job.cancelled', 'Cancelled; existing output retained.', type='job')
        return get(job_id)

    @api.post('/api/jobs/{job_id}/answer')
    async def answer(job_id, body: TextInput):
        j = get(job_id)
        if j['status'] != 'waiting' or not j.get('question'):
            raise HTTPException(409, 'Job is not awaiting an answer')
        if not body.answer.strip():
            raise HTTPException(400, 'Enter an answer')
        store.update(job_id, answer=body.answer)
        store.state(job_id).instruction(body.answer, 'answer')
        store.event(job_id, 'user.answer', body.answer, type='user')
        await runner.submit(job_id)
        return get(job_id)

    @api.post('/api/jobs/{job_id}/message')
    async def message(job_id, body: TextInput):
        j = get(job_id)
        if j['status'] not in jobstatus.ACCEPTS_MESSAGES:
            raise HTTPException(409, 'Start or resume a run to send instructions')
        if not body.message.strip():
            raise HTTPException(400, 'Enter an instruction')
        # Delivered once into the conversation; also adding it to the task would repeat it.
        store.mutate(job_id, lambda job: job.update(pending_messages=job.get('pending_messages', []) + [body.message]))
        store.state(job_id).instruction(body.message)
        store.event(job_id, 'user.instruction', body.message, type='user')
        return get(job_id)

    # Synchronous handlers run in the threadpool, so SQLite waits never block the event loop.
    @api.get('/api/jobs/{job_id}/inventory')
    def inventory(job_id, offset: int = 0, limit: int = 25):
        get(job_id)
        try:
            return store.state(job_id).page('inventory', offset, limit)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @api.get('/api/jobs/{job_id}/coverage')
    def coverage(job_id):
        get(job_id)
        return store.state(job_id).coverage()

    @api.get('/api/jobs/{job_id}/plan')
    def work_plan(job_id):
        get(job_id)
        state = store.state(job_id)
        return {
            'plan': state.get('plan', 'current'),
            'checkpoint': state.get('checkpoint', 'main'),
        }

    @api.get('/api/jobs/{job_id}/regions')
    def regions(job_id, offset: int = 0, limit: int = 25):
        get(job_id)
        try:
            return store.state(job_id).page('region', offset, limit)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @api.get('/api/jobs/{job_id}/tasks')
    def tasks(job_id, offset: int = 0, limit: int = 25):
        get(job_id)
        try:
            return store.state(job_id).page('task', offset, limit)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @api.get('/api/jobs/{job_id}/events')
    async def events(job_id, after: int = 0):
        get(job_id)
        return store.events(job_id, after)

    @api.get('/api/jobs/{job_id}/stream')
    async def stream(job_id, request: Request, after: int = 0):
        get(job_id)
        cursor = max(after, last_event_id(request))

        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                while True:
                    batch = store.events(job_id, cursor)
                    if not batch:
                        break
                    for event in batch:
                        cursor = event['seq']
                        yield f'id: {cursor}\nevent: activity\ndata: {json.dumps(event)}\n\n'
                    if await request.is_disconnected():
                        return
                j = get(job_id)
                yield f'event: state\ndata: {json.dumps(j)}\n\n'
                if j['status'] in jobstatus.IDLE:
                    break
                yield ': keepalive\n\n'
                await asyncio.sleep(1)

        return StreamingResponse(
            generate(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
        )

    @api.get('/api/jobs/{job_id}/logs')
    async def logs(job_id):
        get(job_id)

        def chunks():
            cursor = 0
            while True:
                batch = store.events(job_id, cursor)
                if not batch:
                    break
                for event in batch:
                    yield json.dumps(event, ensure_ascii=False) + '\n'
                cursor = batch[-1]['seq']

        return StreamingResponse(
            chunks(),
            media_type='application/x-ndjson',
            headers={'Content-Disposition': f'attachment; filename="{job_id}-events.jsonl"'},
        )

    @api.get('/api/jobs/{job_id}/artifacts/{filename}')
    async def artifact(job_id, filename):
        get(job_id)
        try:
            path = within(store.path(job_id) / 'outputs', filename)
        except ValueError:
            raise HTTPException(400, 'Invalid output name') from None
        if not path.is_file() or path.name.endswith('.pending'):
            raise HTTPException(404, 'Output not found')
        return FileResponse(path, filename=path.name)

    @api.post('/api/connection-check')
    async def connection_check():
        if not conf.api_key:
            raise HTTPException(503, 'Add OPENAI_API_KEY to .env and restart the application.')
        try:
            async with AsyncOpenAI(api_key=conf.api_key, base_url=conf.base_url, timeout=30, max_retries=0) as client:
                response = await client.responses.create(
                    model=conf.model, input='Reply with OK.', max_output_tokens=64, store=False
                )
            return {
                'ok': True,
                'model': conf.model,
                'route': conf.base_url,
                'response_id': response.id,
                'output': response.output_text,
                'usage': response.usage.model_dump() if response.usage else None,
            }
        except Exception as error:
            raise HTTPException(502, clean(str(error))) from None

    api.mount('/', StaticFiles(directory=ROOT / 'web', html=True), name='frontend')
    return api


app = create_app()
