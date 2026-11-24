"""Run the packets that test the app's own loops: comments on a finished output, and "remember for next time".

These cannot run through run_job alone, because what they test lives in the app around the engine: the round of
comments built from the files, how it reaches a run that starts again, the report of what changed, and the playbook
version a remembered comment adds. So each run here makes a real app store in its own folder, sets a job up in it
the way the app would, and lets the app's engine service run it against the model.

A packet takes this path when it has a loop.json:

    {"prior_key": "prior_key.json",   # the earlier, finished output the user is commenting on, as an answer key
     "comments": [{"path": "outputs/x.xlsx", "sheet": "Joiners", "cell": "E5", "text": "...", "remember": false}],
     "then_fresh_run": true,          # optional: afterwards, a new job from the playbook as it now stands
     "learn": true}                   # optional: no comment is marked "remember"; the agent must take the lesson
                                      # itself, the fresh run must follow it, and its kept test must replay right

The packet's answer_key.json describes the last run's expected output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from openpyxl import load_workbook

from app import feedback, replay
from app.engine.agent import SHAPES
from app.engine.jobspec import Answerer, load_job
from app.engine.rules import table
from app.engine.service import SandboxEngine
from app.store import Store

from .score import row_key, same, score
from .selftest import perfect_outputs

STATUS = {'completed': 'complete'}  # the app's words for how a run ended, in the scorecard's


def _new_job(store: Store, spec, context: str, playbook: dict) -> dict:
    """A job set up as an upload would leave it: the template, the packet's files and their list."""
    job = store.create('payroll', '2026-11', spec.goal, context, playbook)
    root = store.path(job['id'])
    template = spec.deliverables[0].template
    shutil.copy2(template, root / 'template' / template.name)
    with zipfile.ZipFile(spec.zip) as archive:
        archive.extractall(root / 'inputs')
    files = [{'path': f'template/{template.name}', 'role': 'template'}]
    for path in sorted(p for p in (root / 'inputs').rglob('*') if p.is_file()):
        data = path.read_bytes()
        files.append({'path': path.relative_to(root).as_posix(), 'role': 'inputs', 'size': len(data),
                      'sha256': hashlib.sha256(data).hexdigest(), 'supported': True})  # fmt: skip
    return store.update(job['id'], files=files)


async def _answer_questions(store: Store, job_id: str, answerer: Answerer):
    """Stand in for the user: answer any question the agent asks with the packet's scripted reply."""
    while True:
        await asyncio.sleep(0.5)
        job = store.get(job_id)
        if job['status'] == 'waiting' and job.get('question') and not job.get('answer'):
            reply = await answerer.answer(job['question']['text'])
            store.update(job_id, answer=reply)


async def _run(engine: SandboxEngine, store: Store, job_id: str, answerer: Answerer) -> dict:
    store.update(job_id, status='queued')
    answering = asyncio.create_task(_answer_questions(store, job_id, answerer))
    try:
        await engine.run(job_id)
    finally:
        answering.cancel()
    return store.get(job_id)


def exact_text(outputs: Path, key: dict) -> bool:
    """True when every cell the key expects is right, and every text cell is right letter for letter. The scorer
    forgives case and spacing, which suits most packets and not one whose point is how a name is written."""
    for name, spec in key.get('deliverables', {}).items():
        try:
            book = load_workbook(outputs / name)
        except Exception:  # noqa: BLE001 - a missing or unreadable workbook is simply not exact
            return False
        for sheet, wanted in spec['sheets'].items():
            if sheet not in book.sheetnames:
                return False
            got = {row_key(values, wanted['key']): values for _, values in table(book[sheet], wanted.get('header_row', 1))[1]}
            for row in wanted['rows']:
                found = got.get(row_key(row, wanted['key']), {})
                for field in wanted['fields']:
                    # Text is compared as written. A date or number the agent stored as one is the scorer's to
                    # judge: "2026-11-03" in the key and a real date in the cell are the same thing.
                    if isinstance(found.get(field), str) and isinstance(row[field], str) and found[field] != row[field]:
                        return False
                    if not same(row[field], found.get(field)):
                        return False
    return True


def _tokens(job: dict) -> dict:
    usage = job.get('usage') or {}
    return {'requests': job.get('request_count', 0), 'input_tokens': usage.get('input_tokens', 0),
            'output_tokens': usage.get('output_tokens', 0)}  # fmt: skip


async def loop_run(job_dir: Path, shape: str, settings, runs: Path, echo=print, **options) -> dict:
    """Run one loop packet and return its scorecard row (the columns one_run gives, plus what only a loop has)."""
    spec = load_job(job_dir)
    plan = json.loads((job_dir / 'loop.json').read_text(encoding='utf-8'))
    key = json.loads((job_dir / 'answer_key.json').read_text(encoding='utf-8'))
    prior = json.loads((job_dir / plan['prior_key']).read_text(encoding='utf-8'))
    run_id = f'{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}'
    store = Store(runs / f'loop-{spec.id}-{run_id}')
    team = SHAPES[shape]
    effort = options.get('effort', 'medium')
    if effort not in ('low', 'medium', 'high'):
        # These packets run through the app, which offers these three; a run at another effort would not be the one
        # the rest of the scorecard is being compared with.
        raise SystemExit(f'{spec.id} runs through the app, whose Settings page has no effort "{effort}".')
    store.save_settings({
        'max_scouts': team['max_scouts'] if options.get('max_scouts') is None else options['max_scouts'],
        'reviewer': team['review'] if options.get('review_on') is None else options['review_on'],
        'proof': options.get('proof', 'row'), 'max_turns': options.get('max_turns', 60), 'effort': effort,
        'time_limit_minutes': max(5, options.get('timeout_seconds', 1800) // 60),
        'litellm_provider': settings.provider or '', 'model_on_create': settings.model_on_create,
    })  # fmt: skip
    engine = SandboxEngine(store, SimpleNamespace(api_key=settings.api_key, base_url=settings.base_url, model=settings.model))
    answerer = Answerer(spec.answers)
    started = asyncio.get_running_loop().time()

    # Run one: a finished job with the earlier output, and the user's comments on it.
    book = store.playbooks.get('payroll')
    job = _new_job(store, spec, book['text'], {k: book[k] for k in ('id', 'name', 'version')})
    root = store.path(job['id'])
    perfect_outputs(spec, prior, root / 'outputs')
    (root / 'outputs' / 'notes.md').write_text('Filled from the joiners list. Nothing open.', encoding='utf-8')
    (root / 'engine' / 'run-01').mkdir(parents=True)
    earlier = {'status': 'complete', 'final_check': {'passed': True, 'issues': []}}
    (root / 'engine' / 'run-01' / 'run.json').write_text(json.dumps(earlier), encoding='utf-8')
    store.update(job['id'], status='completed')
    sent = feedback.create_round(store, job['id'], uuid.uuid4().hex, plan['comments'])
    echo(f'  comments sent as round {sent["number"]}; remembered as playbook version {sent["remembered_version"]}')
    job = await _run(engine, store, job['id'], answerer)
    first = score(root / 'outputs', key, job.get('engine_questions'))
    rounds = feedback.rounds(store, job['id'])
    report = (rounds[0]['report'] if rounds else None) or {}
    row = {
        'packet': spec.id, 'shape': shape, 'run': store.root.name, 'status': STATUS.get(job['status'], job['status']),
        'error': job.get('error'), 'scouts': 0, 'reviews': (job.get('engine_check') or {}).get('reviews', 0),
        'review_doubts': (job.get('engine_check') or {}).get('doubts', 0),
        'check_passed': bool((job.get('engine_check') or {}).get('passed')),
        'round_status': rounds[0]['status'] if rounds else None,
        'comments': len(plan['comments']), 'comments_changed': len(report.get('changed_where_asked') or []),
        'changed_elsewhere': report.get('changed_elsewhere_count'),
        'remembered_version': sent['remembered_version'],
        **_tokens(job),
    }  # fmt: skip
    # What the agent took from the comments by itself (app/lessons.py), and the version that made of the playbook.
    row['lessons'] = [{'lesson': x['lesson'], 'status': x['status'], 'reason': x.get('reason')} for x in job.get('lessons') or []]
    row['learned_version'] = next((x['version'] for x in job.get('lessons') or [] if x['status'] == 'saved'), None)
    result, last = first, job
    ended = [row['status']]
    # The feature is proven when the commented spots changed, nothing else in the deliverables did, the checks
    # passed and the output is right, letter for letter.
    row['proved'] = (
        row['status'] == 'complete' and row['check_passed'] and row['comments_changed'] == row['comments']
        and row['changed_elsewhere'] == 0 and first['cell_accuracy'] == 1.0 and first['row_precision'] == 1.0
        and exact_text(root / 'outputs', key)
    )  # fmt: skip

    if plan.get('then_fresh_run'):
        # Run two: a new job on the same packet, from the playbook as the remembered comment left it. No comment.
        row['first_run'] = {'status': row['status'], **{k: first[k] for k in ('row_recall', 'row_precision', 'cell_accuracy')}}
        book = store.playbooks.get('payroll')
        fresh = _new_job(store, spec, book['text'], {k: book[k] for k in ('id', 'name', 'version')})
        if plan.get('learn'):  # no earlier run to look at: only the instructions can carry the lesson
            store.update(fresh['id'], past_runs=[])
        echo(f'  fresh run from playbook version {book["version"]}')
        last = await _run(engine, store, fresh['id'], answerer)
        result = score(store.path(fresh['id']) / 'outputs', key, last.get('engine_questions'))
        tokens = _tokens(last)
        row.update({k: row[k] + tokens[k] for k in tokens})
        ended.append(STATUS.get(last['status'], last['status']))
        # Both runs count: the row is complete only when both were, and shows the first that was not otherwise.
        row.update(status=next((s for s in ended if s != 'complete'), 'complete'), error=row['error'] or last.get('error'),
                   check_passed=bool((last.get('engine_check') or {}).get('passed')), playbook_version=book['version'])  # fmt: skip
        # Here the comment asks for every name to change, so changes elsewhere are the point, not a fault. What
        # proves remembering is the second run: right, letter for letter, with no comment to tell it.
        row['proved'] = (
            ended == ['complete', 'complete'] and row['check_passed']
            and book['version'] == (row['learned_version'] if plan.get('learn') else sent['remembered_version'])
            and result['cell_accuracy'] == 1.0 and result['row_precision'] == 1.0
            and exact_text(store.path(fresh['id']) / 'outputs', key)
        )  # fmt: skip
    if plan.get('learn') and plan.get('then_fresh_run'):
        # The lesson's kept test, replayed against the playbook as it now stands, through the same engine.
        check = replay.start(store, 'payroll', 'asked')
        closed = None
        for entry in (check or {}).get('cases', []):
            replayed = await _run(engine, store, entry['job_id'], answerer)
            tokens = _tokens(replayed)
            row.update({k: row[k] + tokens[k] for k in tokens})
            closed = replay.finished(store, store.get(entry['job_id'])) or closed
        row['replay'] = f'{closed["passed_count"]}/{closed["total"]}' if closed else 'no test was kept'
        row['proved'] = bool(row['proved'] and closed and closed['passed'])
    (store.root / 'score.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    row['seconds'] = round(asyncio.get_running_loop().time() - started, 1)
    row.update({k: result[k] for k in ('row_recall', 'row_precision', 'cell_accuracy', 'relevance_accuracy',
                                       'decision_accuracy', 'asked_expected', 'questions_asked', 'unneeded_questions')})  # fmt: skip
    return row
