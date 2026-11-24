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
from typing import Annotated
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from openpyxl import load_workbook
from pydantic import BaseModel, Field

from . import app_settings, cases, feedback, jobstatus, preview, replay, tuning
from .config import ROOT, Settings, settings
from .engine.checks import normal_decision, read_map
from .engine.rules import check_format_definition
from .engine.service import DAY_USED_UP
from .files import add_file, add_zip, is_junk, within
from .playbooks import MAX_NAME, MAX_TEXT, StaleVersion
from .runtime import Runner
from .skills import MAX_ZIP_BYTES
from .store import Store, clean

VERSION = '0.3.0'
MAX_TEXT_CHARS = MAX_TEXT
MAX_VERSION = 2**31


class TextInput(BaseModel):
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


def create_app(settings_override: Settings | None = None):
    conf = settings_override or settings
    conf.validate()
    store = Store(conf.data_dir)
    playbooks = store.playbooks
    runner = Runner(store, conf)

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
            'playbooks': playbook_rows(),
            'statuses': jobstatus.as_json(),
        }

    class PlaybookInput(BaseModel):
        name: str = Field(max_length=MAX_NAME * 2)
        copy_of: str | None = None

    class VersionInput(BaseModel):
        text: str = Field(max_length=MAX_TEXT_CHARS)
        based_on: int = Field(ge=1, le=MAX_VERSION)  # the version the editor started from
        proposal_id: str | None = Field(default=None, pattern='^[a-f0-9]{32}$')  # set when accepting a proposal

    class RestoreInput(BaseModel):
        version: int = Field(ge=1, le=MAX_VERSION)
        based_on: int = Field(ge=1, le=MAX_VERSION)

    def playbook_call(action):
        """Run one playbook action, turning its refusals into answers the screen can show."""
        try:
            return action()
        except KeyError:
            raise HTTPException(404, 'Playbook, version or skill not found') from None
        except StaleVersion as error:
            raise HTTPException(409, {'message': str(error), 'version': error.current}) from None
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    def playbook_rows():
        """Every playbook with how many skills it holds, for the list and the New run page."""
        return [{**p, 'skills': len(playbooks.skills.list(p['id']))} for p in playbooks.list()]

    @api.get('/api/playbooks')
    def playbook_list():
        return playbook_rows()

    @api.post('/api/playbooks', status_code=201)
    def playbook_create(body: PlaybookInput):
        return playbook_call(lambda: playbooks.create(body.name, body.copy_of))

    @api.get('/api/playbooks/{playbook_id}')
    def playbook_get(playbook_id):
        return playbook_call(lambda: playbooks.get(playbook_id))

    @api.patch('/api/playbooks/{playbook_id}')
    def playbook_rename(playbook_id, body: PlaybookInput):
        return playbook_call(lambda: playbooks.rename(playbook_id, body.name))

    @api.get('/api/playbooks/{playbook_id}/versions')
    def playbook_versions(playbook_id):
        return playbook_call(lambda: playbooks.versions(playbook_id))

    @api.get('/api/playbooks/{playbook_id}/versions/{version}')
    def playbook_version(playbook_id, version: int = Path(ge=1, le=MAX_VERSION)):
        return playbook_call(lambda: playbooks.version(playbook_id, version))

    @api.post('/api/playbooks/{playbook_id}/versions')
    def playbook_save(playbook_id, body: VersionInput):
        if body.proposal_id:  # the user accepted what the agent proposed, as it came or after editing it
            return playbook_call(lambda: tuning.accept(store, playbook_id, body.proposal_id, body.text, body.based_on))
        return playbook_call(lambda: playbooks.save(playbook_id, body.text, 'edited', based_on=body.based_on))

    class TuneInput(BaseModel):
        ask: str = Field(min_length=1, max_length=tuning.MAX_ASK)
        based_on: int = Field(ge=1, le=MAX_VERSION)

    @api.post('/api/playbooks/{playbook_id}/tune')
    async def playbook_tune(playbook_id, body: TuneInput):
        """Ask the agent to rewrite the instructions. It only proposes; accepting is an ordinary save."""
        if not conf.api_key:
            raise HTTPException(503, 'Add OPENAI_API_KEY to .env and restart the application.')
        if not body.ask.strip():
            raise HTTPException(400, 'Say what to change')
        if store.day_used_up():
            raise HTTPException(429, DAY_USED_UP)
        try:
            endpoint = runner.engine.endpoint(store.settings())
            return await tuning.propose(store, endpoint, playbook_id, body.ask, body.based_on)
        except KeyError:
            raise HTTPException(404, 'Playbook, version or skill not found') from None
        except StaleVersion as error:
            raise HTTPException(409, {'message': str(error), 'version': error.current}) from None
        except tuning.TuningFailed as error:
            raise HTTPException(502, str(error)) from None

    @api.get('/api/playbooks/{playbook_id}/tune/{proposal_id}')
    def playbook_proposal(playbook_id, proposal_id):
        return playbook_call(lambda: tuning.read(store, playbook_id, proposal_id))

    @api.post('/api/playbooks/{playbook_id}/tune/{proposal_id}/discard')
    def playbook_discard(playbook_id, proposal_id):
        return playbook_call(lambda: tuning.discard(store, playbook_id, proposal_id))

    @api.post('/api/playbooks/{playbook_id}/restore')
    def playbook_restore(playbook_id, body: RestoreInput):
        return playbook_call(lambda: playbooks.restore(playbook_id, body.version, body.based_on))

    class SkillInput(BaseModel):
        enabled: bool

    @api.get('/api/playbooks/{playbook_id}/tests')
    async def playbook_tests(playbook_id):
        """The past corrections kept as tests of this playbook, their latest results and the latest check."""
        return playbook_call(lambda: replay.summary(store, playbook_id))

    @api.post('/api/playbooks/{playbook_id}/tests/check', status_code=202)
    async def playbook_check(playbook_id):
        """Replay the tests now, whatever the Settings page says about doing so automatically."""
        playbook_call(lambda: playbooks.get(playbook_id))
        if not conf.api_key:
            raise HTTPException(400, 'Add an API key on the Settings page first')
        if store.day_used_up():
            raise HTTPException(409, DAY_USED_UP)
        check = await runner.start_check(playbook_id, 'asked')
        if check is None:
            raise HTTPException(409, 'There is nothing to check, or a check is already running')
        return {'id': check['id'], 'cases': len(check['cases'])}

    @api.post('/api/playbooks/{playbook_id}/tests/{case_id}/restore')
    async def playbook_test_restore(playbook_id, case_id):
        """Bring a retired test back."""
        return playbook_call(lambda: cases.restore(store, playbook_id, case_id)['status'])

    @api.get('/api/playbooks/{playbook_id}/skills')
    def skill_list(playbook_id):
        return playbook_call(lambda: playbooks.skills.list(playbook_id))

    @api.post('/api/playbooks/{playbook_id}/skills', status_code=201)
    async def skill_upload(playbook_id, file: UploadFile = File(...)):
        try:
            data = await file.read(MAX_ZIP_BYTES + 1)  # one byte past the limit is enough to refuse it
        finally:
            await file.close()
        return await asyncio.to_thread(
            playbook_call, lambda: playbooks.skills.add(playbook_id, data, file.filename or '')
        )

    @api.get('/api/playbooks/{playbook_id}/skills/{name}')
    def skill_get(playbook_id, name):
        return playbook_call(lambda: playbooks.skills.get(playbook_id, name))

    @api.patch('/api/playbooks/{playbook_id}/skills/{name}')
    def skill_enable(playbook_id, name, body: SkillInput):
        return playbook_call(lambda: playbooks.skills.enable(playbook_id, name, body.enabled))

    @api.delete('/api/playbooks/{playbook_id}/skills/{name}', status_code=204)
    def skill_remove(playbook_id, name):
        playbook_call(lambda: playbooks.skills.remove(playbook_id, name))

    @api.get('/api/playbooks/{playbook_id}/events')
    def playbook_events(playbook_id, after: int = Query(0, ge=0, le=2**62), recent: int = Query(0, ge=0, le=500)):
        """The activity log from the start (after a sequence number), or with recent, its latest entries first."""
        if recent:
            return playbook_call(lambda: playbooks.recent(playbook_id, recent))
        return playbook_call(lambda: (playbooks.get(playbook_id), playbooks.events(playbook_id, after))[1])

    class SettingsInput(BaseModel):
        values: dict = Field(default_factory=dict)

    def settings_page(values):
        connection = {
            'base_url': conf.base_url,
            'model': conf.model,
            'warning': app_settings.region_warning(conf.base_url, conf.model),
        }
        return {'values': values, 'options': app_settings.describe(), 'connection': connection,
                'tokens_today': store.spent_today()}  # fmt: skip

    @api.get('/api/settings')
    def app_settings_get():
        return settings_page(store.settings())

    @api.put('/api/settings')
    def app_settings_put(body: SettingsInput):
        try:
            return settings_page(store.save_settings(body.values))
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @api.get('/api/jobs')
    async def jobs():
        return store.summaries()

    @api.post('/api/jobs', status_code=201)
    async def upload(
        playbook_id: str = Form(''),
        period: str = Form(''),  # optional: a reporting month, for work that has one
        instructions: str = Form(''),
        context: str = Form(''),
        template: UploadFile | None = File(None),  # optional: a workbook to fill, or a written format
        files: list[UploadFile] = File(...),
    ):
        try:
            playbook = playbooks.get(playbook_id)
        except KeyError:
            raise HTTPException(400, 'Unknown playbook') from None
        if period and not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
            raise HTTPException(400, 'Use a reporting month in YYYY-MM format')
        # A run fills an .xlsx template or builds a workbook from a written format (.json, see app/engine/rules.py).
        # Without either, the agent decides what files the goal calls for; that needs a goal to decide from.
        if template is not None and not (template.filename or '').lower().endswith(('.xlsx', '.json')):
            raise HTTPException(400, 'Choose an .xlsx output template or a .json format')
        if template is None and not instructions.strip():
            raise HTTPException(400, 'Say what the run should produce, or choose a template or format')
        if not files:
            raise HTTPException(400, 'Choose source files')
        if len(instructions) > MAX_TEXT_CHARS or len(context) > MAX_TEXT_CHARS:
            raise HTTPException(400, 'Instructions are too long')
        # The job keeps its own copy of the instructions, so a later edit to the playbook never changes this run.
        # Text sent with the upload that differs from the playbook's (an unsaved edit, a stale tab) is used as
        # sent, and the job then names no version: a version is named only when its exact text was used.
        custom = bool(context) and context != playbook['text']
        used = {'id': playbook['id'], 'name': playbook['name'], 'version': None if custom else playbook['version']}
        if custom:
            used['edited_from'] = playbook['version']
        job_id = store.create(playbook['id'], period, instructions, context or playbook['text'], used)['id']
        try:
            manifest = await save_uploads(store.path(job_id), template, files)
            mark_duplicates(manifest)
            # The job takes its own copy of the skills that are switched on, for the same reason as the text.
            skills = await asyncio.to_thread(
                playbooks.skills.copy_to_job, playbook['id'], store.path(job_id) / 'skills'
            )
            store.update(job_id, files=manifest, skills=skills)
            store.event(
                job_id,
                'files.received',
                f'{sum(entry["role"] == "inputs" for entry in manifest)} source files received',
                type='job',
                files=manifest,
                playbook=used,
                skills=skills,
            )
            return store.get(job_id)
        except Exception as error:
            # Nothing usable was stored, so leave no empty failed run in history.
            shutil.rmtree(store.path(job_id), ignore_errors=True)
            store.delete(job_id)
            raise HTTPException(400, clean(str(error))) from None
        finally:
            if template is not None:
                await template.close()
            for f in files:
                await f.close()

    async def save_uploads(root, template, files):
        """Write the template and source files (expanding ZIPs) into the job folder; returns the manifest."""
        manifest = []
        total = 0
        limit = conf.max_upload_mb * 1024 * 1024
        for upload, role in ([(template, 'template')] if template is not None else []) + [(f, 'inputs') for f in files]:
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
        # Fail early for a corrupt/encrypted/non-workbook template, or a format the engine cannot read.
        if template is not None:
            definition = root / manifest[0]['path']
            if definition.suffix.lower() == '.json':
                check_format_definition(json.loads(definition.read_text(encoding='utf-8-sig')))
            else:
                load_workbook(definition, read_only=True).close()
        return manifest

    @api.get('/api/jobs/{job_id}')
    async def job(job_id):
        return get(job_id)

    @api.post('/api/jobs/{job_id}/start')
    @api.post('/api/jobs/{job_id}/resume')
    async def start(job_id):
        j = get(job_id)
        if not conf.api_key:
            raise HTTPException(503, 'Add OPENAI_API_KEY to .env and restart the application.')
        if j['status'] not in jobstatus.STARTABLE or j.get('legacy'):
            raise HTTPException(409, 'This job cannot start in its current state')
        if runner.active == job_id:
            raise HTTPException(409, 'The previous operation is still finishing')
        if not j['files']:
            raise HTTPException(400, 'No usable input files')
        if store.day_used_up():
            raise HTTPException(409, DAY_USED_UP)
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
        store.event(job_id, 'user.answer', body.answer, type='user')
        # A run still holding its sandbox picks the answer up itself. Otherwise, or once that run has ended, the
        # runner queues the job; the answer was saved first, so whichever side looks last finds it.
        if runner.active != job_id:
            await runner.resume_if_answered(job_id)
        return get(job_id)

    @api.post('/api/jobs/{job_id}/message')
    async def message(job_id, body: TextInput):
        j = get(job_id)
        if j['status'] not in jobstatus.ACCEPTS_MESSAGES or j.get('legacy'):
            raise HTTPException(409, 'Start or resume a run to send instructions')
        if not body.message.strip():
            raise HTTPException(400, 'Enter an instruction')
        # Delivered once into the conversation; also adding it to the task would repeat it.
        store.mutate(job_id, lambda job: job.update(pending_messages=job.get('pending_messages', []) + [body.message]))
        store.event(job_id, 'user.instruction', body.message, type='user')
        return get(job_id)

    # Synchronous handlers run in the threadpool, so file and SQLite waits never block the event loop.
    class CommentInput(BaseModel):
        """One comment and the spot it points at: a cell on a sheet, a place on a page or picture, or a line."""

        path: str = Field(max_length=1000)
        text: str = Field(min_length=1, max_length=feedback.MAX_TEXT)
        remember: bool = False
        sheet: str | None = Field(default=None, max_length=200)
        cell: str | None = Field(default=None, max_length=25)  # one cell, or a block such as C3:E5
        page: int | None = Field(default=None, ge=1, le=100_000)
        x: float | None = Field(default=None, ge=0, le=1)
        y: float | None = Field(default=None, ge=0, le=1)
        w: float | None = Field(default=None, ge=0, le=1)  # a box drawn on a page or picture: its width and height
        h: float | None = Field(default=None, ge=0, le=1)
        line: int | None = Field(default=None, ge=1, le=10_000_000)

    class RoundInput(BaseModel):
        round_id: str = Field(pattern=r'^[A-Za-z0-9-]{8,64}$')  # made by the browser, so a repeat is one round
        comments: list[CommentInput] = Field(min_length=1, max_length=feedback.MAX_COMMENTS)

    @api.post('/api/jobs/{job_id}/comments', status_code=201)
    async def comment_round(job_id, body: RoundInput):
        j = get(job_id)
        known = next((r for r in j.get('feedback') or [] if r['id'] == body.round_id), None)
        if known:
            return {'round': {**known, 'duplicate': True}, 'job': j}
        if j['status'] not in jobstatus.ACCEPTS_COMMENTS or j.get('legacy'):
            raise HTTPException(
                409, 'Comments can be sent once a run has produced files and is not waiting for an answer'
            )
        if j['status'] not in jobstatus.ACTIVE:  # a stopped run starts again to act on them
            if not conf.api_key:
                raise HTTPException(503, 'Add OPENAI_API_KEY to .env and restart the application.')
            if runner.active == job_id:
                raise HTTPException(409, 'The previous operation is still finishing')
        if any(not c.text.strip() for c in body.comments):
            raise HTTPException(400, 'Enter a comment')
        try:
            made = await asyncio.to_thread(
                feedback.create_round, store, job_id, body.round_id, [c.model_dump() for c in body.comments]
            )
        except ValueError as error:
            raise HTTPException(400, clean(str(error))) from None
        # Queue the job unless a run is going and will pick the comments up itself. The comments were saved first,
        # so whichever side looks last finds them.
        if not made.get('duplicate') and runner.active != job_id and get(job_id)['status'] not in jobstatus.ACTIVE:
            await runner.submit(job_id)
        return {'round': made, 'job': get(job_id)}

    @api.get('/api/jobs/{job_id}/comments')
    def comment_rounds(job_id):
        get(job_id)
        return feedback.rounds(store, job_id)

    @api.get('/api/jobs/{job_id}/document-map')
    def document_map(job_id):
        """The agent's file-by-file record: each packet file, its decision and the reason."""
        get(job_id)
        entries, error = read_map(store.path(job_id) / 'outputs')
        if entries is None:
            return {'files': [], 'error': None if error.endswith('not been saved yet.') else error}
        files = [
            {'path': p, 'decision': normal_decision(e.get('decision')), 'reason': str(e.get('reason') or '')}
            for p, e in sorted(entries.items())
        ]
        return {'files': files}

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
        job = get(job_id)

        def chunks():
            # First what the run was given, so the file can be read on its own: the playbook copy it used and its
            # skills. Then every round of comments with its report. Then the events.
            used = {k: job.get(k) for k in ('playbook', 'context', 'skills', 'instructions', 'period')}
            yield json.dumps({'name': 'export.playbook', 'job_id': job_id, **clean(used)}, ensure_ascii=False) + '\n'
            for entry in feedback.rounds(store, job_id):
                yield (
                    json.dumps({'name': 'export.comment_round', 'job_id': job_id, **clean(entry)}, ensure_ascii=False)
                    + '\n'
                )
            if job.get('lessons'):  # what the agent took from those comments, and what became of each
                line = {'name': 'export.lessons', 'job_id': job_id, 'lessons': clean(job['lessons'])}
                yield json.dumps(line, ensure_ascii=False) + '\n'
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

    # The Files panel: look inside an upload, the template or an output without downloading it.
    def job_file_error(error):
        if isinstance(error, FileNotFoundError):
            return HTTPException(404, 'File not found')
        return HTTPException(400, 'Invalid file path' if 'path' in str(error).lower() else str(error))

    @api.get('/api/jobs/{job_id}/preview')
    def file_preview(job_id, path: str, sheet: str | None = None):
        paths = [f['path'] for f in get(job_id)['files']]
        try:
            return preview.preview(store.path(job_id), path, sheet, paths)
        except (FileNotFoundError, ValueError) as error:
            raise job_file_error(error) from None

    @api.get('/api/jobs/{job_id}/changes')
    def file_changes(job_id, path: str, sheet: str | None = None):
        get(job_id)
        try:
            return feedback.changes_since(store, job_id, path, sheet)
        except (FileNotFoundError, ValueError) as error:
            raise job_file_error(error) from None

    @api.get('/api/jobs/{job_id}/find')
    def file_find(job_id, path: str, q: str = Query(min_length=1, max_length=200)):
        get(job_id)
        try:
            hits, more, text = preview.find_in_pdf(store.path(job_id), path, [q])
        except (FileNotFoundError, ValueError) as error:
            raise job_file_error(error) from None
        except Exception as error:  # noqa: BLE001 - a damaged PDF
            raise HTTPException(422, clean(f'This file cannot be searched: {error}')) from None
        return {'hits': [{k: h[k] for k in ('page', 'x', 'y', 'w', 'h')} for h in hits], 'more': more, 'text': text}

    class LocateInput(BaseModel):
        path: str = Field(max_length=1000)
        values: list[Annotated[str, Field(max_length=200)]] = Field(max_length=60)

    @api.post('/api/jobs/{job_id}/locate')
    def file_locate(job_id, body: LocateInput):
        """Where a row of an output shows up in one of its sources."""
        get(job_id)
        try:
            return preview.locate_values(store.path(job_id), body.path, [v for v in body.values if v.strip()])
        except (FileNotFoundError, ValueError) as error:
            raise job_file_error(error) from None
        except Exception as error:  # noqa: BLE001 - a damaged PDF
            raise HTTPException(422, clean(f'This file cannot be searched: {error}')) from None

    @api.get('/api/jobs/{job_id}/page')
    def file_page(job_id, path: str, page: int = 1):
        get(job_id)
        try:
            image = preview.page_png(store.path(job_id), path, page)
        except (FileNotFoundError, ValueError) as error:
            raise job_file_error(error) from None
        except Exception as error:  # noqa: BLE001 - a damaged PDF
            raise HTTPException(422, clean(f'This page cannot be shown: {error}')) from None
        return Response(image, media_type='image/png', headers={'Cache-Control': 'private, max-age=3600'})

    @api.get('/api/jobs/{job_id}/file')
    def file_download(job_id, path: str):
        get(job_id)
        try:
            found = preview.job_file(store.path(job_id), path)
        except (FileNotFoundError, ValueError) as error:
            raise job_file_error(error) from None
        return FileResponse(found, filename=found.name)

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

    api.mount('/', Frontend(directory=ROOT / 'web', html=True), name='frontend')
    return api


class Frontend(StaticFiles):
    """The page's own files. The browser asks each time whether a file changed (a quick "not modified" when it has
    not), so after an update it never mixes a new script with a cached old one."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers['Cache-Control'] = 'no-cache'
        return response


app = create_app()
