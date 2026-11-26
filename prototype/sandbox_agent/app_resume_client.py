"""Two live runs through the main app (plan Step 24): one for a client, whose notes the run must follow, and one
with no client that is paused after its first save and continued from that saved version.

Live and paid. Run it with the main app's Python, from the repository root:

  .venv/bin/python prototype/sandbox_agent/app_resume_client.py --packets <packets folder> --out <folder>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1]), str(HERE)]

from app_parity import period_of  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sandbox_agent.score import score  # noqa: E402

from app.config import Settings  # noqa: E402
from app.engine.jobspec import Answerer, load_job  # noqa: E402
from app.main import create_app  # noqa: E402

MARKER = 'Prepared for Northwind under its client notes.'
NOTES = f'Start notes.md with this exact line: "{MARKER}"'
ENDED = ('completed', 'partial', 'failed', 'cancelled')


def start(client, folder: Path, client_id: str = '') -> str:
    spec = load_job(folder)
    definition = spec.deliverables[0].template or spec.deliverables[0].format_path
    data = {'playbook_id': spec.module, 'period': period_of(spec.goal), 'instructions': spec.goal, 'client_id': client_id}
    files = [('template', (definition.name, definition.read_bytes())), ('files', (spec.zip.name, spec.zip.read_bytes()))]
    made = client.post('/api/jobs', data=data, files=files)
    made.raise_for_status()
    client.post(f'/api/jobs/{made.json()["id"]}/start').raise_for_status()
    return made.json()['id']


def wait(client, job_id: str, folder: Path, until, timeout: int = 2400) -> dict:
    answerer, started = Answerer(load_job(folder).answers), time.monotonic()
    while time.monotonic() - started < timeout:
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['status'] == 'waiting' and job.get('question') and not job.get('answer'):
            reply = asyncio.run(answerer.answer(job['question']['text']))
            client.post(f'/api/jobs/{job_id}/answer', json={'answer': reply}).raise_for_status()
        elif until(job):
            return job
        time.sleep(3)
    raise TimeoutError(job_id)


def scored(client, job_id: str, folder: Path) -> dict:
    spec, job = load_job(folder), client.get(f'/api/jobs/{job_id}').json()
    deliverable = spec.deliverables[0]
    definition = deliverable.template or deliverable.format_path
    key = json.loads((folder / 'answer_key.json').read_text(encoding='utf-8'))
    name = f'{definition.stem}_output.xlsx' if deliverable.template else f'{definition.stem}.xlsx'
    key['deliverables'] = {name: key['deliverables'][deliverable.name]}
    outputs = client.app.state.store.path(job_id) / 'outputs'
    result = score(outputs, key, job.get('engine_questions') or [])
    notes = (outputs / 'notes.md').read_text(encoding='utf-8') if (outputs / 'notes.md').exists() else ''
    runs = job.get('runs') or []
    return {
        'status': job['status'], 'error': job.get('error'), 'runs': len(runs),
        'minutes': round(sum(r['seconds'] for r in runs) / 60, 1), 'requests': sum(r['requests'] for r in runs),
        'input_tokens': sum(r['input_tokens'] for r in runs), 'output_tokens': sum(r['output_tokens'] for r in runs),
        'notes_start_with_marker': notes.lstrip().startswith(MARKER), 'report': job.get('report'),
        **{k: result[k] for k in ('row_recall', 'row_precision', 'cell_accuracy', 'relevance_accuracy')},
    }  # fmt: skip


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--packets', type=Path, required=True)
    parser.add_argument('--packet', default='payroll_consolidation')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    folder = args.packets / args.packet
    rows = {}
    with TestClient(base_url='http://127.0.0.1', app=create_app(Settings(data_dir=args.out))) as client:
        values = {'max_scouts': 0, 'reviewer': False, 'time_limit_minutes': 40}
        client.put('/api/settings', json={'values': values}).raise_for_status()
        northwind = client.post('/api/clients', json={'name': 'Northwind', 'notes': NOTES}).json()

        print('run 1: for Northwind', flush=True)
        job = start(client, folder, northwind['id'])
        wait(client, job, folder, lambda j: j['status'] in ENDED)
        rows['with_client'] = scored(client, job, folder)
        print(json.dumps(rows['with_client'], default=str), flush=True)

        print('run 2: no client, paused after its first save, continued from that version', flush=True)
        job = start(client, folder)
        saved = lambda j: j['status'] in ENDED or client.get(f'/api/jobs/{j["id"]}/versions').json()['versions']  # noqa: E731
        first = wait(client, job, folder, saved)
        if first['status'] not in ENDED:
            client.post(f'/api/jobs/{job}/pause').raise_for_status()
            wait(client, job, folder, lambda j: j['status'] in ('paused', *ENDED))
        kept = client.get(f'/api/jobs/{job}/versions').json()['versions']
        print(f'  versions kept before continuing: {[(v["number"], v["description"]) for v in kept]}', flush=True)
        oldest = kept[-1]['number']
        for _ in range(40):  # the paused run may still be letting go of its sandbox
            continued = client.post(f'/api/jobs/{job}/versions/{oldest}/continue')
            if continued.status_code != 409 or 'still finishing' not in continued.text:
                break
            time.sleep(3)
        print(f'  continue from version {oldest}: {continued.status_code}', flush=True)
        if continued.status_code == 200:
            wait(client, job, folder, lambda j: j['status'] in ENDED)
        rows['stopped_and_continued'] = {
            **scored(client, job, folder), 'continued_from': oldest, 'stopped_at_status': first['status'],
            'continue_status': continued.status_code,
        }  # fmt: skip
        print(json.dumps(rows['stopped_and_continued'], default=str), flush=True)
    (args.out / 'phase8_app_runs.json').write_text(json.dumps(rows, indent=1, default=str), encoding='utf-8')
    print(f'Results: {args.out / "phase8_app_runs.json"}')


if __name__ == '__main__':
    main()
