"""The test packets (the scorecard). Each module builds one packet; see common.py for what a builder writes."""

from __future__ import annotations

import importlib
from pathlib import Path

PACKETS = [
    'payroll_consolidation',
    'payment_journal',
    'payroll_instructions_question',
    'payroll_noise',
    'ap_invoice_upload',
    'large_packet',
]


def available() -> list[str]:
    here = Path(__file__).parent
    return [name for name in PACKETS if (here / f'{name}.py').exists()]


def build(name: str, out: Path, seed: int = 20261031) -> dict:
    if name not in available():
        raise SystemExit(f'Unknown packet {name!r}. Available: {available()}')
    module = importlib.import_module(f'{__name__}.{name}')
    return module.build(out / name, seed)
