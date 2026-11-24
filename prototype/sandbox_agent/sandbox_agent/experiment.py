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


async def one_run(
    job_dir: Path, shape: str, settings: Settings, runs: Path, log_dir: Path, skills: bool = True,
    past_runs: bool = True, **options,
) -> dict:  # fmt: skip
    """skills=False runs a packet that has skills without them, and past_runs=False a packet that names earlier
    runs without them, for comparison."""
    spec = load_job(job_dir, skills=skills, past_runs=past_runs)
    key = json.loads((job_dir / 'answer_key.json').read_text(encoding='utf-8'))
    log_path = log_dir / f'{spec.id}-{shape}-{datetime.now():%H%M%S%f}.log'

    def echo(line: str):
        with open(log_path, 'a', encoding='utf-8') as handle:
            handle.write(line + '\n')

    if (job_dir / 'loop.json').exists():  # a packet that tests the app's own loops; see loop.py
        from .loop import loop_run

        return await loop_run(job_dir, shape, settings, runs, echo=echo, **options)
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
        'skills_offered': [k['name'] for k in record.get('skills') or []],
        'skills_read': (record.get('skills_read') or {}).get('main', []),
        'skills_run': record.get('skills_run') or [],
        'without_skills': not skills and bool(load_job(job_dir).skills),
        'past_runs_offered': [r['label'] for r in record.get('past_runs') or []],
        'past_runs_opened': record.get('past_runs_opened') or [],
        'without_past_runs': not past_runs and bool(load_job(job_dir).past_runs),
        # A packet with skills proves them when the agent read one and the output is right; a packet with earlier
        # runs, when the agent opened one and the output is right.
        'proved': _proved(spec, record, result),
        **_tokens(record.get('usage', {})),
        **{k: result[k] for k in ('row_recall', 'row_precision', 'cell_accuracy', 'relevance_accuracy',
                                  'decision_accuracy', 'asked_expected', 'questions_asked', 'unneeded_questions')},
    }  # fmt: skip


def _proved(spec, record: dict, result: dict) -> bool | None:
    if not spec.skills and not spec.past_runs:
        return None
    # A skill with a program is used when the program ran; a skill of text only, when it was read.
    with_programs = any(k.get('programs') for k in spec.skills)
    skills_used = bool(record.get('skills_run')) if with_programs else bool(record.get('skills_read'))
    used = (not spec.skills or skills_used) and (
        not spec.past_runs or bool(record.get('past_runs_opened'))
    )
    return used and result['cell_accuracy'] == 1.0


def _mean(values) -> str:
    values = [v for v in values if v is not None]
    return f'{statistics.mean(values):.3f}' if values else '-'


def summary(rows: list[dict], baseline: list[dict] | None = None) -> str:
    """baseline: the rows of an earlier experiment (its results.jsonl), to show beside these."""
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
    extras = [r for r in rows if r.get('skills_offered') or r.get('without_skills') or r.get('comments')
              or r.get('past_runs_offered') or r.get('without_past_runs')]  # fmt: skip
    if extras:
        lines += ['## Skills, earlier runs, comments and remembering', '', '| packet | run | feature proven | what happened |', '|---|---|---|---|']
        for r in extras:
            if r.get('comments'):
                said = (f'{r["comments_changed"]}/{r["comments"]} commented spots changed, {r["changed_elsewhere"]} '
                        f'change(s) elsewhere, round {r["round_status"]}')  # fmt: skip
                if r.get('remembered_version'):
                    first = r.get('first_run') or {}
                    said += (f'; remembered as playbook version {r["remembered_version"]}; after the comment: '
                             f'{first.get("status")}, cells right {first.get("cell_accuracy")}; fresh run from the '
                             f'playbook: cells right {r["cell_accuracy"]}')  # fmt: skip
                if r.get('lessons'):
                    said += f'; lessons the agent took: {[(x["status"], x["lesson"][:80]) for x in r["lessons"]]}'
                if r.get('replay'):
                    said += f'; its test replayed: {r["replay"]}'
            elif r.get('without_past_runs'):
                said = f'run without its earlier runs, for comparison: cells right {r["cell_accuracy"]}'
            elif r.get('past_runs_offered'):
                said = (f'earlier runs offered {r["past_runs_offered"]}, opened {r["past_runs_opened"]}, cells right '
                        f'{r["cell_accuracy"]}')  # fmt: skip
            elif r.get('without_skills'):
                said = f'run without its skills, for comparison: cells right {r["cell_accuracy"]}'
            else:
                ran = f', programs run {r["skills_run"]}' if r.get('skills_run') else ''
                said = f'skills offered {r["skills_offered"]}, read {r["skills_read"]}{ran}, cells right {r["cell_accuracy"]}'
            proved = {True: 'yes', False: 'NO', None: '-'}[r.get('proved')]
            lines.append(f'| {r["packet"]} / {r["shape"]} | {r["run"]} | {proved} | {said} |')
        lines.append('')
    if baseline:
        lines += ['## Against the earlier scorecard', '', 'Each cell is earlier -> now, averaged over the runs of that packet and shape.', '',
                  '| | runs | cell accuracy | row recall | relevance | minutes | requests | input tokens (k) | output tokens (k) |',
                  '|' + '---|' * 9]  # fmt: skip
        groups: dict[str, tuple[list, list]] = {}
        for which, source in enumerate((baseline, rows)):
            for r in source:
                groups.setdefault(f'{r["packet"]} / {r["shape"]}', ([], []))[which].append(r)
        for name, (before, after) in sorted(groups.items()):
            if before and after:
                pair = lambda pick, a=before, b=after: f'{_mean(pick(r) for r in a)} -> {_mean(pick(r) for r in b)}'  # noqa: E731
                lines.append(
                    f'| {name} | {len(before)} -> {len(after)} | ' + ' | '.join([
                        pair(lambda r: r['cell_accuracy']), pair(lambda r: r['row_recall']),
                        pair(lambda r: r['relevance_accuracy']), pair(lambda r: (r['seconds'] or 0) / 60),
                        pair(lambda r: r['requests']), pair(lambda r: r['input_tokens'] / 1000),
                        pair(lambda r: r.get('output_tokens', 0) / 1000),
                    ]) + ' |'
                )  # fmt: skip
        lines.append('')
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
    only: list[str] | None = None, skills: bool = True, baseline: Path | None = None, past_runs: bool = True,
    **options,
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
    earlier = [json.loads(line) for line in baseline.read_text(encoding='utf-8').splitlines()] if baseline else None

    async def guarded(job: Path, shape: str):
        async with gate:
            print(f'start  {job.name} / {shape}', flush=True)
            try:
                row = await one_run(job, shape, settings, runs, logs, skills=skills, past_runs=past_runs, **options)
            except Exception as error:  # noqa: BLE001 - one broken run must not stop the experiment
                row = {'packet': job.name, 'shape': shape, 'run': None, 'status': 'failed', 'error': repr(error),
                       'seconds': None, 'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'row_recall': None,
                       'row_precision': None, 'cell_accuracy': None, 'relevance_accuracy': None,
                       'decision_accuracy': None, 'asked_expected': None, 'questions_asked': 0,
                       'unneeded_questions': 0}  # fmt: skip
            rows.append(row)
            with open(results_path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, default=str) + '\n')
            (folder / 'summary.md').write_text(summary(rows, earlier), encoding='utf-8')
            print(
                f'done   {job.name} / {shape}: {row["status"]}, cells {row["cell_accuracy"]}, '
                f'relevance {row["relevance_accuracy"]}, {row["seconds"]} s',
                flush=True,
            )

    await asyncio.gather(*(guarded(job, shape) for job, shape in plan))
    return folder
