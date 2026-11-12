"""Command line: run the agent on a zip and template, or build a test packet.

  python -m sandbox_agent fixture runs/fixture
  python -m sandbox_agent run --zip runs/fixture/packet.zip --template runs/fixture/output_template.xlsx \
      --answer-key runs/fixture/answer_key.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .agent import run_job
from .config import add_endpoint_args, describe, load_settings, prepare_console_and_network
from .packet import build
from .score import score

HERE = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(prog='sandbox_agent')
    sub = parser.add_subparsers(dest='command', required=True)
    fixture = sub.add_parser('fixture', help='generate the synthetic test packet')
    fixture.add_argument('out', type=Path)
    show = sub.add_parser('settings', help='show which endpoint and model a run would use, and where they come from')
    add_endpoint_args(show)
    run = sub.add_parser('run', help='run the agent')
    run.add_argument('--zip', type=Path, required=True)
    run.add_argument('--template', type=Path, required=True)
    run.add_argument('--answer-key', type=Path, help='score the result against a generated answer key')
    run.add_argument('--runs', type=Path, default=HERE / 'runs')
    add_endpoint_args(run)
    run.add_argument('--scout-model')
    run.add_argument('--effort', default='medium', choices=['none', 'low', 'medium', 'high', 'xhigh'])
    run.add_argument('--max-turns', type=int, default=60)
    run.add_argument('--timeout', type=int, default=1800, help='seconds')
    run.add_argument('--max-scouts', type=int, default=8, help='0 turns parallel scouts off')
    run.add_argument('--memory', default='4g', choices=['1g', '4g', '16g', '64g'])
    args = parser.parse_args()
    prepare_console_and_network()
    if args.command == 'settings':
        print(describe(load_settings(args)))
        return
    if args.command == 'fixture':
        key = build(args.out)
        print(f'{len(key["files"])} files, {len(key["employees"])} expected rows -> {args.out}')
        return
    settings = load_settings(args)
    print(describe(settings))
    store = asyncio.run(
        run_job(
            args.zip,
            args.template,
            settings,
            args.runs,
            max_turns=args.max_turns,
            timeout_seconds=args.timeout,
            effort=args.effort,
            max_scouts=args.max_scouts,
            scout_model=args.scout_model,
            memory=args.memory,
        )
    )
    if args.answer_key:
        result = score(store.dir, args.answer_key)
        store.write_json('score.json', result)
        print(json.dumps({k: v for k, v in result.items() if k not in ('rows_missing', 'rows_extra')}, indent=2))
    # 0 complete, 2 partial or stopped, 1 failed: so scripts can tell what happened
    sys.exit({'complete': 0, 'failed': 1}.get(store.record['status'], 2))


if __name__ == '__main__':
    main()
