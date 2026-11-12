"""Run test packets through the main app on the sandbox engine, answering its questions as the packet scripts, and
score the result. This is the Phase 3 check that the app and the prototype get the same results.

Live and paid. Run it with the main app's Python, from the repository root:

  .venv/bin/python prototype/sandbox_agent/app_parity.py --packets prototype/sandbox_agent/runs/packets \
      --only payroll_consolidation,payroll_noise --scouts 4 --reviewer

A format deliverable is uploaded in the template's place, as the app allows for sandbox jobs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1]), str(HERE)]

from fastapi.testclient import TestClient  # noqa: E402
from sandbox_agent.score import score  # noqa: E402

from app.config import Settings  # noqa: E402
from app.engine.jobspec import Answerer, load_job  # noqa: E402
from app.main import create_app  # noqa: E402

MONTHS = 'January February March April May June July August September October November December'.split()


def period_of(goal: str) -> str:
    match = re.search(rf'({"|".join(MONTHS)}) (\d{{4}})', goal)
    return f'{match.group(2)}-{MONTHS.index(match.group(1)) + 1:02d}' if match else '2026-10'


def run_packet(client, folder: Path, timeout: int) -> dict:
    spec = load_job(folder)
    deliverable = spec.deliverables[0]
    definition = deliverable.template or deliverable.format_path  # a template to fill, or a format to build to
    response = client.post(
        '/api/jobs',
        data={'module': spec.module, 'period': period_of(spec.goal), 'instructions': spec.goal},
        files=[
            ('template', (definition.name, definition.read_bytes())),
            ('files', (spec.zip.name, spec.zip.read_bytes())),
        ],
    )
    response.raise_for_status()
    job_id = response.json()['id']
    started = time.monotonic()
    client.post(f'/api/jobs/{job_id}/start').raise_for_status()
    answerer = Answerer(spec.answers)
    while True:
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['status'] == 'waiting' and job.get('question') and not job.get('answer'):
            answer = asyncio.run(answerer.answer(job['question']['text']))
            print(f'  {spec.id}: answered "{job["question"]["text"][:80]}"', flush=True)
            client.post(f'/api/jobs/{job_id}/answer', json={'answer': answer}).raise_for_status()
        elif job['status'] in ('completed', 'partial', 'failed', 'paused', 'cancelled'):
            break
        if time.monotonic() - started > timeout:
            client.post(f'/api/jobs/{job_id}/cancel')
            break
        time.sleep(3)
    key = json.loads((folder / 'answer_key.json').read_text(encoding='utf-8'))
    output_name = f'{definition.stem}_output.xlsx' if deliverable.template else f'{definition.stem}.xlsx'
    key['deliverables'] = {output_name: key['deliverables'][deliverable.name]}
    outputs = client.app.state.store.path(job_id) / 'outputs'
    result = score(outputs, key, job.get('engine_questions') or [])
    return {
        'packet': spec.id,
        'status': job['status'],
        'error': job.get('error'),
        'minutes': round((time.monotonic() - started) / 60, 1),
        'requests': job.get('request_count'),
        **{
            k: result[k]
            for k in ('row_recall', 'row_precision', 'cell_accuracy', 'relevance_accuracy', 'asked_expected')
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--packets', type=Path, default=HERE / 'runs' / 'packets')
    parser.add_argument('--only', help='comma-separated packet names')
    parser.add_argument('--scouts', type=int, default=4)
    parser.add_argument('--reviewer', action='store_true')
    parser.add_argument('--timeout', type=int, default=2400, help='seconds per packet')
    args = parser.parse_args()
    data_dir = HERE / 'runs' / 'app-parity' / datetime.now().strftime('%Y%m%d-%H%M%S')
    settings = Settings(data_dir=data_dir, job_timeout=args.timeout)
    folders = sorted(p.parent for p in args.packets.glob('*/job.json'))
    if args.only:
        folders = [f for f in folders if f.name in args.only.split(',')]
    rows = []
    with TestClient(base_url='http://127.0.0.1', app=create_app(settings)) as client:
        client.put('/api/settings', json={'values': {'engine': 'sandbox', 'max_scouts': args.scouts,
                                                     'reviewer': args.reviewer}})  # fmt: skip
        for folder in folders:
            if len(load_job(folder).deliverables) != 1:
                print(f'skip {folder.name}: the app takes one deliverable per job')
                continue
            print(f'start {folder.name}', flush=True)
            row = run_packet(client, folder, args.timeout)
            rows.append(row)
            print(json.dumps(row), flush=True)
    (data_dir / 'parity.json').write_text(json.dumps(rows, indent=1), encoding='utf-8')
    print(f'Results: {data_dir / "parity.json"}')


if __name__ == '__main__':
    main()
