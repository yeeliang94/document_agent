"""Command line.

python -m sandbox_agent packets runs/packets                  build every test packet and self-test it
python -m sandbox_agent run --job runs/packets/payment_journal --shape C
python -m sandbox_agent run --zip my.zip --template my_template.xlsx --goal "Prepare ..."
python -m sandbox_agent experiment --packets runs/packets --shapes A,B,C --repeats 1
python -m sandbox_agent settings                              show the endpoint and model a run would use
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from app.engine.agent import SHAPES, run_job
from app.engine.jobspec import Answerer, adhoc_job, load_job

from . import packets as packet_registry
from .config import add_endpoint_args, describe, load_settings, prepare_console_and_network
from .score import score
from .selftest import selftest

HERE = Path(__file__).resolve().parents[1]
SUMMARY_KEYS = ('row_recall', 'row_precision', 'cell_accuracy', 'relevance_accuracy', 'decision_accuracy',
                'relevance_wrong', 'asked_expected', 'questions_asked', 'unneeded_questions')  # fmt: skip


def _run_options(parser: argparse.ArgumentParser):
    add_endpoint_args(parser)
    parser.add_argument(
        '--shape', default='B', choices=sorted(SHAPES), help='A lead only, B with scouts, C with scouts and reviewer'
    )
    parser.add_argument('--max-scouts', type=int, help='override the shape: 0 turns parallel scouts off')
    parser.add_argument('--review', action=argparse.BooleanOptionalAction, default=None, help='override the shape')
    parser.add_argument(
        '--proof', default='row', choices=['row', 'none'], help='row: every written row names its sources'
    )
    parser.add_argument('--scout-model')
    parser.add_argument('--effort', default='medium', choices=['none', 'low', 'medium', 'high', 'xhigh'])
    parser.add_argument('--max-turns', type=int, default=60)
    parser.add_argument('--timeout', type=int, default=1800, help='seconds')
    parser.add_argument('--memory', default='4g', choices=['1g', '4g', '16g', '64g'])


def _options(args) -> dict:
    return {
        'max_scouts': args.max_scouts,
        'review_on': args.review,
        'proof': args.proof,
        'max_turns': args.max_turns,
        'timeout_seconds': args.timeout,
        'effort': args.effort,
        'scout_model': args.scout_model,
        'memory': args.memory,
    }


def build_packets(out: Path, only: list[str] | None) -> bool:
    names = only or packet_registry.available()
    ok = True
    for name in names:
        shutil.rmtree(out / name, ignore_errors=True)
        packet_registry.build(name, out)
        job = load_job(out / name)
        key = json.loads((out / name / 'answer_key.json').read_text(encoding='utf-8'))
        perfect = out / '_selftest' / name
        shutil.rmtree(perfect, ignore_errors=True)
        result = selftest(job, key, perfect)
        ok &= result['passed']
        issues = result['check']['issues'][:3]
        print(f'{name}: {"self-test passed" if result["passed"] else "SELF-TEST FAILED " + str(issues)}')
    return ok


async def _console_ask(question: str) -> str:
    print(f'\nThe agent asks: {question}')
    return await asyncio.to_thread(input, 'Your answer: ')


def main() -> None:
    parser = argparse.ArgumentParser(prog='sandbox_agent')
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('packets', help='build the synthetic test packets and self-test each one')
    build.add_argument('out', type=Path)
    build.add_argument('--only', help='comma-separated packet names')
    fixture = sub.add_parser('fixture', help='build only the payroll consolidation packet (older scripts use this)')
    fixture.add_argument('out', type=Path)
    show = sub.add_parser('settings', help='show which endpoint and model a run would use, and where they come from')
    add_endpoint_args(show)
    run = sub.add_parser('run', help='run the agent on one job')
    run.add_argument('--job', type=Path, help='a job folder (with job.json) or job.json itself')
    run.add_argument('--zip', type=Path)
    run.add_argument('--template', type=Path)
    run.add_argument('--goal', help='what the run should produce (with --zip and --template)')
    run.add_argument('--module', default='payroll')
    run.add_argument(
        '--answer-key', type=Path, help='score against this key (default: answer_key.json beside job.json)'
    )
    run.add_argument('--ask', choices=['scripted', 'console'], default='scripted', help='who answers the agent')
    run.add_argument('--runs', type=Path, default=HERE / 'runs')
    _run_options(run)
    exp = sub.add_parser('experiment', help='run team shapes across the test packets and compare them')
    exp.add_argument('--packets', type=Path, default=HERE / 'runs' / 'packets')
    exp.add_argument('--shapes', default='A,B,C')
    exp.add_argument('--repeats', type=int, default=1)
    exp.add_argument('--concurrency', type=int, default=3)
    exp.add_argument('--only', help='comma-separated packet names')
    exp.add_argument('--out', type=Path, default=HERE / 'runs' / 'experiments')
    _run_options(exp)
    args = parser.parse_args()
    prepare_console_and_network()

    if args.command == 'packets':
        sys.exit(0 if build_packets(args.out, args.only.split(',') if args.only else None) else 1)
    if args.command == 'fixture':
        key = packet_registry.build('payroll_consolidation', args.out)
        print(f'{len(key["files"])} files -> {args.out / "payroll_consolidation"}')
        return
    if args.command == 'settings':
        print(describe(load_settings(args)))
        return
    settings = load_settings(args)
    print(describe(settings))
    if args.command == 'experiment':
        from .experiment import experiment

        shapes = [s.strip().upper() for s in args.shapes.split(',')]
        folder = asyncio.run(
            experiment(
                args.packets, shapes, args.repeats, settings, args.out, args.concurrency,
                args.only.split(',') if args.only else None, **_options(args),
            )
        )  # fmt: skip
        print(f'Results: {folder / "summary.md"}')
        return

    if args.job:
        spec = load_job(args.job)
    elif args.zip and args.template and args.goal:
        spec = adhoc_job(args.zip, args.template, args.goal, args.module)
    else:
        parser.error('give --job, or --zip with --template and --goal')
    answerer = Answerer(spec.answers, ask=_console_ask if args.ask == 'console' else None)
    store = asyncio.run(run_job(spec, settings, args.runs, shape=args.shape, answerer=answerer, **_options(args)))
    key_path = args.answer_key or (spec.folder / 'answer_key.json' if spec.folder else None)
    if key_path and key_path.exists():
        result = score(store.outputs, json.loads(key_path.read_text(encoding='utf-8')), store.record.get('questions'))
        store.write_json('score.json', result)
        print(json.dumps({k: result[k] for k in SUMMARY_KEYS}, indent=2))
    # 0 complete, 2 partial or stopped, 1 failed: so scripts can tell what happened
    sys.exit({'complete': 0, 'failed': 1}.get(store.record['status'], 2))


if __name__ == '__main__':
    main()
