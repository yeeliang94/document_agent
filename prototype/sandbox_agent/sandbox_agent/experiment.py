"""Run every team shape on every test packet, score each run, and write a comparison table.

  python -m sandbox_agent experiment --packets runs/packets --shapes A,B,C --repeats 1

Writes runs/experiments/<id>/results.jsonl (one line per run) and summary.md (averages per shape and per packet).
"""

from __future__ import annotations

import asyncio
import json
import statistics
from datetime import datetime
from pathlib import Path

from app.engine.agent import run_job
from app.engine.jobspec import load_job

from .config import Settings
from .score import score


def _tokens(usage: dict) -> dict:
    parts = list(usage.get('lead', [])) + list(usage.get('scouts', [])) + list(usage.get('reviews', []))
    return {
        'requests': sum(p.get('requests', 0) for p in parts),
        'input_tokens': sum(p.get('input_tokens', 0) for p in parts),
        'output_tokens': sum(p.get('output_tokens', 0) for p in parts),
    }


async def one_run(job_dir: Path, shape: str, settings: Settings, runs: Path, log_dir: Path, **options) -> dict:
    spec = load_job(job_dir)
    key = json.loads((job_dir / 'answer_key.json').read_text(encoding='utf-8'))
    log_path = log_dir / f'{spec.id}-{shape}-{datetime.now():%H%M%S%f}.log'

    def echo(line: str):
        with open(log_path, 'a', encoding='utf-8') as handle:
            handle.write(line + '\n')

    store = await run_job(spec, settings, runs, shape=shape, echo=echo, **options)
    record = store.record
    result = score(store.outputs, key, record.get('questions', []))
    store.write_json('score.json', result)
    return {
        'packet': spec.id,
        'shape': shape,
        'run': store.id,
        'status': record['status'],
        'error': record.get('error'),
        'seconds': record.get('seconds'),
        'scouts': record.get('scouts', 0),
        'check_passed': bool(record.get('final_check') and record['final_check']['passed']),
        'reviews': len(record.get('reviews', [])),
        'review_doubts': sum(len(r['doubts']) for r in record.get('reviews', [])),
        **_tokens(record.get('usage', {})),
        **{k: result[k] for k in ('row_recall', 'row_precision', 'cell_accuracy', 'relevance_accuracy',
                                  'decision_accuracy', 'asked_expected', 'questions_asked', 'unneeded_questions')},
    }  # fmt: skip


def _mean(values) -> str:
    values = [v for v in values if v is not None]
    return f'{statistics.mean(values):.3f}' if values else '-'


def summary(rows: list[dict]) -> str:
    lines = ['# Team-shape experiment', '']
    columns = ['complete', 'row recall', 'row precision', 'cell accuracy', 'relevance', 'asked when needed',
               'unneeded questions', 'minutes', 'requests', 'input tokens (k)']  # fmt: skip

    def block(title: str, groups: dict[str, list[dict]]):
        lines.extend([f'## {title}', '', '| | runs | ' + ' | '.join(columns) + ' |', '|' + '---|' * (len(columns) + 2)])
        for name, items in groups.items():
            asked = [r['asked_expected'] for r in items if r['asked_expected'] is not None]
            cells = [
                f'{sum(r["status"] == "complete" for r in items)}/{len(items)}',
                _mean(r['row_recall'] for r in items),
                _mean(r['row_precision'] for r in items),
                _mean(r['cell_accuracy'] for r in items),
                _mean(r['relevance_accuracy'] for r in items),
                f'{sum(asked)}/{len(asked)}' if asked else '-',
                str(sum(r['unneeded_questions'] or 0 for r in items)),
                _mean((r['seconds'] or 0) / 60 for r in items),
                _mean(r['requests'] for r in items),
                _mean(r['input_tokens'] / 1000 for r in items),
            ]
            lines.append(f'| {name} | {len(items)} | ' + ' | '.join(cells) + ' |')
        lines.append('')

    by_shape, by_packet, by_both = {}, {}, {}
    for r in rows:
        by_shape.setdefault(r['shape'], []).append(r)
        by_packet.setdefault(r['packet'], []).append(r)
        by_both.setdefault(f'{r["packet"]} / {r["shape"]}', []).append(r)
    block('By shape', dict(sorted(by_shape.items())))
    block('By packet', dict(sorted(by_packet.items())))
    block('By packet and shape', dict(sorted(by_both.items())))
    failures = [r for r in rows if r['status'] != 'complete']
    if failures:
        lines += ['## Runs that did not complete', '']
        lines += [
            f'- {r["packet"]} / {r["shape"]} ({r["run"]}): {r["status"]}, {r["error"] or "see run.json"}'
            for r in failures
        ]
    return '\n'.join(lines) + '\n'


async def experiment(
    packets: Path, shapes: list[str], repeats: int, settings: Settings, out: Path, concurrency: int = 3,
    only: list[str] | None = None, **options,
) -> Path:  # fmt: skip
    folder = out / datetime.now().strftime('%Y%m%d-%H%M%S')
    runs, logs = folder / 'runs', folder / 'logs'
    runs.mkdir(parents=True)
    logs.mkdir()
    jobs = sorted(p.parent for p in packets.glob('*/job.json') if not only or p.parent.name in only)
    if not jobs:
        raise SystemExit(
            f'No packets with job.json under {packets}. Build them with: python -m sandbox_agent packets {packets}'
        )
    plan = [(job, shape) for _ in range(repeats) for job in jobs for shape in shapes]
    gate = asyncio.Semaphore(concurrency)
    results_path = folder / 'results.jsonl'
    rows: list[dict] = []

    async def guarded(job: Path, shape: str):
        async with gate:
            print(f'start  {job.name} / {shape}', flush=True)
            try:
                row = await one_run(job, shape, settings, runs, logs, **options)
            except Exception as error:  # noqa: BLE001 - one broken run must not stop the experiment
                row = {'packet': job.name, 'shape': shape, 'run': None, 'status': 'failed', 'error': repr(error),
                       'seconds': None, 'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'row_recall': None,
                       'row_precision': None, 'cell_accuracy': None, 'relevance_accuracy': None,
                       'decision_accuracy': None, 'asked_expected': None, 'questions_asked': 0,
                       'unneeded_questions': 0}  # fmt: skip
            rows.append(row)
            with open(results_path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, default=str) + '\n')
            (folder / 'summary.md').write_text(summary(rows), encoding='utf-8')
            print(
                f'done   {job.name} / {shape}: {row["status"]}, cells {row["cell_accuracy"]}, '
                f'relevance {row["relevance_accuracy"]}, {row["seconds"]} s',
                flush=True,
            )

    await asyncio.gather(*(guarded(job, shape) for job, shape in plan))
    return folder
