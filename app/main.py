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
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Path, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus

from . import app_settings, cases, feedback, jobstatus, links, plain_errors, preview, replay, tuning, voice
from .config import ROOT, Settings, settings
from .engine import versions
from .engine.agent import UPLOAD_LIMIT
from .engine.checks import normal_decision, read_map
from .engine.rules import check_format_definition
from .engine.service import DAY_USED_UP
from .files import add_file, add_zip, added_name, is_junk, within
from .playbooks import MAX_NAME, MAX_TEXT, StaleVersion
from .runtime import Runner
from .skills import MAX_ZIP_BYTES
from .store import Store, clean

VERSION = '0.3.0'
MAX_TEXT_CHARS = MAX_TEXT
MAX_VERSION = 2**31
MAX_ADDED_FILES = 10  # files sent with one message


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
    api.state.voice_connect = ws_connect  # where the live voice relay opens its session; tests put a fake here

    trusted_hosts = conf.trusted_hosts()
    allowed_origins = {o.rstrip('/') for o in conf.allowed_origins}

    def known_host(header: str) -> bool:
        # A DNS-rebinding page sends its own name as Host, so reject unknown hosts.
        try:
            host = urlparse('//' + header).hostname or ''
        except ValueError:
            host = ''
        return '*' in trusted_hosts or host in trusted_hosts

    @api.middleware('http')
    async def boundary(request: Request, call_next):
        if not known_host(request.headers.get('host', '')):
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

    @api.exception_handler(RequestValidationError)
    async def form_error(request: Request, error: RequestValidationError):
        """A value the form or address got wrong, as a sentence the page can show beside it."""
        return JSONResponse({'detail': plain_errors.field_errors(error.errors())}, 422)

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
            'more_steps': store.settings()['more_steps'],  # the run screen names it on its button
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

    clients = store.clients

    class ClientInput(BaseModel):
        name: str = Field(max_length=MAX_NAME * 2)
        notes: str = Field(default='', max_length=MAX_TEXT_CHARS)

    class SuggestInput(BaseModel):
        paths: list[str] = Field(max_length=5000)  # the chosen folder and file names, as the browser has them

    def client_call(action):
        """Run one client action, turning its refusals into answers the screen can show."""
        try:
            return action()
        except KeyError:
            raise HTTPException(404, 'Client or version not found') from None
        except StaleVersion as error:
            raise HTTPException(409, {'message': str(error), 'version': error.current}) from None
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @api.get('/api/clients')
    def client_list():
        return clients.list()

    @api.post('/api/clients', status_code=201)
    def client_create(body: ClientInput):
        return client_call(lambda: clients.create(body.name, body.notes))

    @api.post('/api/clients/suggest')
    def client_suggest(body: SuggestInput):
        """The client the chosen files point to, for the New run form to offer. It is only ever a suggestion."""
        found = clients.suggest([p[:1000] for p in body.paths])
        return {'client': {k: found[k] for k in ('id', 'name')} if found else None}

    @api.get('/api/clients/{client_id}')
    def client_get(client_id):
        return client_call(lambda: clients.get(client_id))

    @api.patch('/api/clients/{client_id}')
    def client_rename(client_id, body: PlaybookInput):
        return client_call(lambda: clients.rename(client_id, body.name))

    @api.get('/api/clients/{client_id}/versions')
    def client_versions(client_id):
        return client_call(lambda: clients.versions(client_id))

    @api.get('/api/clients/{client_id}/versions/{version}')
    def client_version(client_id, version: int = Path(ge=1, le=MAX_VERSION)):
        return client_call(lambda: clients.version(client_id, version))

    @api.post('/api/clients/{client_id}/versions')
    def client_save(client_id, body: VersionInput):
        return client_call(lambda: clients.save(client_id, body.text, 'edited', based_on=body.based_on))

    @api.post('/api/clients/{client_id}/restore')
    def client_restore(client_id, body: RestoreInput):
        return client_call(lambda: clients.restore(client_id, body.version, body.based_on))

    @api.get('/api/clients/{client_id}/events')
    def client_events(client_id, recent: int = Query(100, ge=1, le=500)):
        return client_call(lambda: clients.recent(client_id, recent))

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
        except app_settings.SettingError as error:
            # field says which setting the sentence is about, so the page shows it beside that field.
            return JSONResponse({'detail': str(error), 'field': error.field}, 400)

    @api.get('/api/jobs')
    async def jobs():
        return store.summaries()

    @api.post('/api/jobs', status_code=201)
    async def upload(
        playbook_id: str = Form(''),
        period: str = Form(''),  # optional: a reporting month, for work that has one
        instructions: str = Form(''),
        context: str = Form(''),
        client_id: str = Form(''),  # optional: the client whose notes the run follows on top of its playbook
        template: UploadFile | None = File(None),  # optional: a workbook to fill, or a written format
        files: list[UploadFile] = File(...),
    ):
        try:
            playbook = playbooks.get(playbook_id)
        except KeyError:
            raise HTTPException(400, 'Unknown playbook') from None
        try:
            client = clients.get(client_id) if client_id else None
        except KeyError:
            raise HTTPException(400, 'Unknown client') from None
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
            # Like the playbook's text, the client's notes are copied now, so a later edit never changes this run.
            if client:
                following = {'id': client['id'], 'name': client['name'], 'version': client['version']}
                store.update(job_id, client=following, client_notes=client['text'])
            store.update(job_id, files=manifest, skills=skills)
            store.event(
                job_id,
                'files.received',
                f'{sum(entry["role"] == "inputs" for entry in manifest)} source files received',
                type='job',
                files=manifest,
                playbook=used,
                skills=skills,
                client=client and client['name'],
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

    class JobInput(BaseModel):
        name: str = Field(max_length=MAX_NAME * 2)

    @api.patch('/api/jobs/{job_id}')
    def job_rename(job_id, body: JobInput):
        """Give a run its own name in the run list. An empty name goes back to the one made from its goal."""
        get(job_id)
        name = ' '.join(body.name.split())
        if len(name) > MAX_NAME:
            raise HTTPException(400, f'Use a name of at most {MAX_NAME} characters')
        return store.update(job_id, name=name or None)

    @api.delete('/api/jobs/{job_id}', status_code=204)
    def job_delete(job_id):
        """Remove a run for good: its record, its events and its folder. A run that is going is refused."""
        j = get(job_id)
        if j['status'] in jobstatus.ACTIVE or runner.active == job_id:
            raise HTTPException(409, 'This run is still going. Stop it before you delete it.')
        store.delete(job_id)
        shutil.rmtree(store.path(job_id), ignore_errors=True)

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
    async def message(
        job_id,
        message: str = Form('', max_length=MAX_TEXT_CHARS),
        files: list[UploadFile] | None = File(None),  # added to the run's packet, for files the user forgot
    ):
        uploads = files or []
        try:
            j = get(job_id)
            refuse_message(j)
            text = message.strip()
            if not text and not uploads:
                raise HTTPException(400, 'Enter a message or add a file')
            if len(uploads) > MAX_ADDED_FILES:
                raise HTTPException(400, f'Add at most {MAX_ADDED_FILES} files at a time')
            if j['status'] not in jobstatus.ACTIVE:  # a run that has stopped starts again to act on the message
                if not conf.api_key:
                    raise HTTPException(503, 'Add OPENAI_API_KEY to .env and restart the application.')
                if store.day_used_up():
                    raise HTTPException(409, DAY_USED_UP)
            received = await read_added(uploads, j['files'])
        finally:
            for f in uploads:
                await f.close()
        entry, starts = await asyncio.to_thread(keep_message, job_id, text, received)
        shown = [{k: f[k] for k in ('path', 'packet')} for f in entry['files']]
        store.event(job_id, 'user.instruction', text, type='user', message_id=entry['id'], files=shown)
        if starts:
            await runner.enqueue(job_id)
        return get(job_id)

    def refuse_message(job):
        if job['status'] not in jobstatus.ACCEPTS_MESSAGES or job.get('legacy'):
            if job['status'] == 'waiting':
                raise HTTPException(409, 'Answer the question first')
            raise HTTPException(409, 'Start the run to send messages')
        # A run that was stopped (cancelled, paused, failed) starts again for the message, but not while the last run
        # is still letting go of it: queuing then would undo a cancel the engine has not seen yet, or be overwritten
        # as that run settles. A finished run needs no wait: the runner starts it again as it lets go
        # (runtime.resume_if_answered).
        if job['status'] not in jobstatus.ACTIVE | jobstatus.FINISHED and runner.active == job['id']:
            raise HTTPException(409, 'The previous operation is still finishing')

    def too_big() -> str:
        return f'The run would exceed {conf.max_upload_mb} MB or {conf.max_files} files'

    async def read_added(uploads, manifest) -> list[tuple[str, bytes]]:
        """The files sent with a message, each checked on its own; where they go is settled when they are kept.
        Reading stops as soon as they would not fit in the run, so an oversized drop is never held in memory whole.
        (keep_message checks again against the manifest as it is then.)"""
        room = conf.max_upload_mb * 1024 * 1024 - sum(e['size'] for e in manifest)
        if len(manifest) + len(uploads) > conf.max_files:
            raise HTTPException(400, too_big())
        received = []
        for upload in uploads:
            name = upload.filename or 'unnamed'
            if name.lower().endswith('.zip'):
                raise HTTPException(400, 'Add the files inside a ZIP one by one')
            data = await upload.read(UPLOAD_LIMIT + 1)
            if not data:
                raise HTTPException(400, clean('Empty file: ' + name))
            if len(data) > UPLOAD_LIMIT:
                raise HTTPException(400, clean(f'{name} is larger than {UPLOAD_LIMIT // 1024**2} MB'))
            room -= len(data)
            if room < 0:
                raise HTTPException(400, too_big())
            received.append((name, data))
        return received

    def keep_message(job_id, text, received) -> tuple[dict, bool]:
        """Add the files to the job's packet and the message to its queue in one step, so two messages sent at once
        never pick the same file name, and a run that finishes meanwhile is either still going and reads the message,
        or has stopped and is queued again here. The next run's packet holds the files; a run that is going gets them
        in its sandbox when the message reaches it. Returns the message and whether the run starts again for it."""
        root, limit = store.path(job_id), conf.max_upload_mb * 1024 * 1024
        entry = {'id': uuid.uuid4().hex, 'text': text, 'files': []}
        kept = {}

        def change(job):
            refuse_message(job)
            manifest, written = job['files'], []
            total = sum(e['size'] for e in manifest)
            try:
                for name, data in received:
                    total += len(data)
                    if total > limit or len(manifest) >= conf.max_files:
                        raise ValueError(too_big())
                    stored, packet = added_name(manifest, name)
                    add_file(root, 'inputs', stored, data, manifest)
                    written.append(root / manifest[-1]['path'])
                    manifest[-1]['added'] = True
                    entry['files'].append(
                        {'name': PurePosixPath(packet).name, 'path': manifest[-1]['path'], 'packet': packet}
                    )
            except BaseException:
                # Nothing half added: the change is not saved, so its files go too. Removed here, while the job is
                # still locked, so another message can never have taken one of these names in the meantime.
                for path in written:
                    path.unlink(missing_ok=True)
                raise
            mark_duplicates(manifest)
            # Delivered once into the conversation; also adding it to the task would repeat it.
            job['pending_messages'] = [*(job.get('pending_messages') or []), entry]
            kept['starts'] = job['status'] not in jobstatus.ACTIVE
            if kept['starts']:
                job.update(status='queued', error=None)

        try:
            store.mutate(job_id, change)
        except (ValueError, HTTPException) as error:
            if isinstance(error, HTTPException):
                raise
            raise HTTPException(400, clean(str(error))) from None
        return entry, kept['starts']

    @api.post('/api/jobs/{job_id}/interrupt')
    async def interrupt(job_id):
        """Stop the agent's current step so it reads the messages waiting for it now. When the agent took them in the
        moment before (the button was still showing), there is nothing left to do, and that is not an error."""
        get(job_id)
        flagged = []

        def flag(job):  # in one step with the check, so it never outlives the messages it was for
            if job['status'] == 'running' and job.get('pending_messages'):
                job['interrupt'] = True
                flagged.append(True)

        store.mutate(job_id, flag)
        if flagged:  # for the Technical log; the thread says the step was stopped once it really was
            store.event(job_id, 'user.interrupt', 'Stopping the current step to read your message', type='job')
        return get(job_id)

    @api.post('/api/jobs/{job_id}/more-steps')
    def more_steps(job_id):
        """Give a run more steps (the amount is a Settings-page value). A run that is going takes them at its next
        step and carries on in the same sandbox; a paused one takes them when it starts again."""
        get(job_id)
        added = store.settings()['more_steps']
        given = []

        def give(job):
            if job['status'] in jobstatus.ACTIVE | {'waiting', 'paused'}:
                job['more_turns'] = (job.get('more_turns') or 0) + added
                given.append(True)

        store.mutate(job_id, give)
        if not given:
            raise HTTPException(409, 'Only a run that is going or paused can be given more steps')
        store.event(job_id, 'user.more_steps', f'Gave the agent {added} more steps', type='user', added=added)
        return get(job_id)

    @api.get('/api/jobs/{job_id}/versions')
    def job_versions(job_id):
        """The outputs kept at each save of every run of this job, newest first."""
        get(job_id)
        return {'versions': versions.listed(store.path(job_id) / 'versions')}

    async def keep_current(root, description):
        """Keep the outputs as a version of their own before they are replaced, unless they already are the newest
        version: a run's last save, or a file changed by hand, can be newer than any kept version."""
        outputs, folder = root / 'outputs', root / 'versions'
        if any(p.is_file() for p in outputs.rglob('*')) and not await asyncio.to_thread(versions.matches_latest, outputs, folder):
            await asyncio.to_thread(versions.keep, outputs, folder, 'kept', 0, description, False, store.settings()['versions_kept'])

    @api.post('/api/jobs/{job_id}/versions/{number}/continue')
    async def continue_from(job_id, number: int = Path(ge=1, le=MAX_VERSION)):
        """Make one kept version the outputs again and start a run that carries on from it in a fresh sandbox."""
        get(job_id)
        if not conf.api_key:
            raise HTTPException(503, 'Add OPENAI_API_KEY to .env and restart the application.')
        if store.day_used_up():
            raise HTTPException(409, DAY_USED_UP)
        before = {}

        def claim(job):  # in one write with the check, so a second click or a Start cannot run alongside
            if job['status'] not in jobstatus.STARTABLE | {'completed'} or job.get('legacy'):
                before['refused'] = 'Stop the run before continuing from a version.'
            elif runner.active == job_id:
                before['refused'] = 'The previous operation is still finishing'
            else:
                before['status'] = job['status']
                job['status'] = 'queued'

        store.mutate(job_id, claim)
        if 'refused' in before:
            raise HTTPException(409, before['refused'])
        root = store.path(job_id)
        try:
            await keep_current(root, f'Before going back to version {number}')
            version = await asyncio.to_thread(versions.restore, root / 'versions', number, root / 'outputs')
        except (KeyError, OSError) as error:
            store.update(job_id, status=before['status'])
            if isinstance(error, KeyError):
                raise HTTPException(404, 'That version is no longer kept.') from None
            raise HTTPException(409, plain_errors.sentence(error)) from None
        store.update(job_id, resume_from=version, start_over=None, error=None,
                     artifacts=runner.engine.artifacts(job_id, False))  # fmt: skip
        store.event(job_id, 'user.continue_from', f'Continuing from version {number}: {version["description"]}',
                    type='user', version=number)  # fmt: skip
        await runner.enqueue(job_id)
        return get(job_id)

    @api.post('/api/jobs/{job_id}/start-over')
    async def start_over(job_id):
        """Empty the outputs and start a run that does the work from the beginning, in a fresh sandbox. The kept
        versions stay, so the work can still be continued from one of them afterwards."""
        j = get(job_id)
        if not conf.api_key:
            raise HTTPException(503, 'Add OPENAI_API_KEY to .env and restart the application.')
        if not j['files']:
            raise HTTPException(400, 'No usable input files')
        if store.day_used_up():
            raise HTTPException(409, DAY_USED_UP)
        before = {}

        def claim(job):  # in one write with the check, so a second click or a Start cannot run alongside
            if job['status'] not in jobstatus.STARTABLE | {'completed'} or job.get('legacy'):
                before['refused'] = 'Stop the run before starting over.'
            elif runner.active == job_id:
                before['refused'] = 'The previous operation is still finishing'
            else:
                before['status'] = job['status']
                job['status'] = 'queued'

        store.mutate(job_id, claim)
        if 'refused' in before:
            raise HTTPException(409, before['refused'])
        root = store.path(job_id)
        try:
            await keep_current(root, 'Before starting over')  # if that fails, nothing is set aside
            await asyncio.to_thread(versions.set_aside, root / 'outputs')
        except OSError as error:
            store.update(job_id, status=before['status'])
            raise HTTPException(409, plain_errors.sentence(error)) from None
        store.update(job_id, start_over=True, resume_from=None, error=None, artifacts=[])
        store.event(job_id, 'user.start_over', 'Starting over from the beginning', type='user')
        await runner.enqueue(job_id)
        return get(job_id)

    # Synchronous handlers run in the threadpool, so file and SQLite waits never block the event loop.
    class CommentInput(BaseModel):
        """One comment and the spot it points at: a cell on a sheet, a place on a page or picture, or a line."""

        path: str = Field(max_length=1000)
        text: str = Field(min_length=1, max_length=feedback.MAX_TEXT)
        remember: bool = False
        # Where a lesson from this comment is kept: for this client only, or in the playbook for everyone. Unset,
        # a remembered comment goes to the playbook and the agent proposes a place for a lesson it takes.
        remember_to: Literal['client', 'playbook'] | None = None
        sheet: str | None = Field(default=None, max_length=200)
        cell: str | None = Field(default=None, max_length=25)  # one cell, or a block such as C3:E5
        page: int | None = Field(default=None, ge=1, le=100_000)
        x: float | None = Field(default=None, ge=0, le=1)
        y: float | None = Field(default=None, ge=0, le=1)
        w: float | None = Field(default=None, ge=0, le=1)  # a box drawn on a page or picture: its width and height
        h: float | None = Field(default=None, ge=0, le=1)
        line: int | None = Field(default=None, ge=1, le=10_000_000)
        reply_to: str | None = Field(default=None, pattern=r'^\d{1,4}\.\d{1,3}$')  # "1.2": a reply to that comment

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

    class LinksInput(BaseModel):
        texts: list[str] = Field(max_length=200)

    @api.post('/api/jobs/{job_id}/links')
    def text_links(job_id, body: LinksInput):
        """The cells and files named in texts the screen shows (the report, answers, messages), each checked against
        this run's files, so only real ones become links. One list per text, in the order sent."""
        j = get(job_id)
        paths = [f['path'] for f in j['files']] + [a['path'] for a in j.get('artifacts') or [] if a.get('path')]
        root = store.path(job_id)
        return {'links': [links.find(root, paths, text) for text in body.texts]}

    class ResolveInput(BaseModel):
        comment: str = Field(pattern=r'^\d{1,4}\.\d{1,3}$')
        resolved: bool = True

    @api.post('/api/jobs/{job_id}/comments/resolve')
    def resolve_comment(job_id, body: ResolveInput):
        """Resolve a comment, or open it again. Only the screen uses this; nothing reaches the agent."""
        get(job_id)
        try:
            feedback.resolve(store, job_id, body.comment, body.resolved)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None
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
    def file_preview(job_id, path: str, sheet: str | None = None, version: int | None = None):
        paths = [f['path'] for f in get(job_id)['files']]
        try:
            return preview.preview(store.path(job_id), path, sheet, paths, version)
        except (FileNotFoundError, ValueError) as error:
            raise job_file_error(error) from None

    @api.get('/api/jobs/{job_id}/changes')
    def file_changes(job_id, path: str, sheet: str | None = None, version: int | None = None, since: int | None = None):
        """What changed in an output (or in a kept version of it) since the last round of comments, or since an
        earlier version."""
        get(job_id)
        try:
            return feedback.changes_since(store, job_id, path, sheet, version, since)
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
    def file_page(job_id, path: str, page: int = 1, version: int | None = None):
        get(job_id)
        try:
            image = preview.page_png(store.path(job_id), path, page, version)
        except (FileNotFoundError, ValueError) as error:
            raise job_file_error(error) from None
        except Exception as error:  # noqa: BLE001 - a damaged PDF
            raise HTTPException(422, clean(f'This page cannot be shown: {error}')) from None
        return Response(image, media_type='image/png', headers={'Cache-Control': 'private, max-age=3600'})

    @api.get('/api/jobs/{job_id}/file')
    def file_download(job_id, path: str, version: int | None = None):
        get(job_id)
        try:
            found = preview.any_file(store.path(job_id), path, version)
        except (FileNotFoundError, ValueError) as error:
            raise job_file_error(error) from None
        return FileResponse(found, filename=found.name)

    # --- Voice: speak instead of type (app/voice.py) ---------------------------------------------------------
    # The HTTP boundary above does not see WebSockets, so the live route checks host, origin and token itself.
    # A browser cannot add headers to a WebSocket, so the access token comes in the first message, not the address.

    def same_origin(ws: WebSocket) -> bool:
        origin = (ws.headers.get('origin') or '').rstrip('/')
        if origin in allowed_origins:
            return True
        parts = urlparse(origin)
        return parts.scheme in ('http', 'https') and parts.netloc == ws.headers.get('host', '')

    @api.websocket('/api/voice/live')
    async def voice_live(ws: WebSocket):
        if not known_host(ws.headers.get('host', '')) or not same_origin(ws):
            await ws.close(code=1008)
            return
        await ws.accept()

        async def refuse(message: str, action: str = ''):
            with suppress(Exception):
                await ws.send_json({'type': 'error', 'message': message, 'action': action})
                await ws.close(code=1011)

        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
            token = str(first.get('token') or '') if isinstance(first, dict) else ''
        except (TimeoutError, ValueError, KeyError, WebSocketDisconnect):
            return await refuse('The voice connection did not start properly. Try again.', 'retry')
        if conf.access_token and not hmac.compare_digest(token.encode(), conf.access_token.encode()):
            return await refuse('Access token required.')
        if not conf.api_key:
            return await refuse('Add OPENAI_API_KEY to .env and restart the application.')
        values = store.settings()
        minutes = values['voice_max_minutes']
        opened = False
        try:
            async with api.state.voice_connect(
                voice.live_url(conf.base_url),
                additional_headers=voice.headers(conf.api_key, values['litellm_provider']),
                open_timeout=10,
                max_size=None,
            ) as upstream:
                await upstream.send(json.dumps(voice.session_update(values['voice_live_model'])))
                await ws.send_json({'type': 'ready'})
                opened = True
                # A page that opens a session and then sends nothing does not hold it open past the longest recording.
                await asyncio.wait_for(voice.relay(ws, upstream, minutes), minutes * 60 + voice.SETTLE_SECONDS + 30)
        except WebSocketDisconnect:
            return
        except voice.VoiceError as error:
            return await refuse(str(error), error.action)
        except TimeoutError:
            if opened:
                return await refuse(voice.limit_message(minutes))
            return await refuse('The transcription service did not answer in time. Try again.', 'retry')
        except InvalidStatus as error:
            code = error.response.status_code
            return await refuse(f'The transcription service refused the live connection (HTTP {code}). If your gateway '
                                'does not allow it, choose "After you stop" for voice in Settings.', 'settings')  # fmt: skip
        except Exception as error:
            return await refuse(f'Could not reach the transcription service ({type(error).__name__}). Try again.', 'retry')
        with suppress(Exception):
            await ws.close()

    @api.get('/api/voice')
    def voice_config():
        values = store.settings()
        minutes = values['voice_max_minutes']
        return {'mode': values['voice_mode'], 'max_minutes': minutes, 'limit_message': voice.limit_message(minutes)}

    @api.post('/api/voice/transcribe')
    async def voice_transcribe(request: Request):
        """The recording arrives as the request body, read into memory up to the limit and never written to disk."""
        if not conf.api_key:
            raise HTTPException(503, 'Add OPENAI_API_KEY to .env and restart the application.')
        values = store.settings()
        minutes = values['voice_max_minutes']
        # A browser recording is compressed far below this: 16-bit audio at 24 kHz, for the longest recording set.
        limit = min(voice.FILE_LIMIT_BYTES, minutes * 60 * voice.BYTES_PER_SECOND)
        if int(request.headers.get('content-length') or 0) > limit:
            raise HTTPException(413, voice.too_long_message(minutes))
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > limit:
                raise HTTPException(413, voice.too_long_message(minutes))
        if not data:
            raise HTTPException(400, 'The recording was empty.')
        client = runner.engine.endpoint(values).client().with_options(timeout=120, max_retries=0)
        name = voice.file_name(request.headers.get('content-type', ''))

        async def lines():
            try:
                async for part in voice.transcribe_file(client, values['voice_file_model'], name, bytes(data)):
                    yield json.dumps(part) + '\n'
            except Exception as error:
                failed = voice.refused(getattr(error, 'message', None) or str(error))
                yield json.dumps({'type': 'error', 'message': str(failed), 'action': failed.action}) + '\n'

        return StreamingResponse(lines(), media_type='application/x-ndjson')

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
            raw = clean(f'{type(error).__name__}: {error}')
            raise HTTPException(502, {'message': plain_errors.sentence(raw), 'raw': raw}) from None

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
