"""The run folder: the local record of everything a run received, did and produced.

runs/<run id>/
  input/          the uploaded zip and template, byte for byte
  inventory.json  every file in the zip with its size and SHA-256, made here, not by the model
  outputs/        every file the agent saved under /mnt/data/outputs in its sandbox
  scouts/         each parallel scout's file profiles
  code/           every script the model ran, with its printed output
  events.jsonl    one line per step: model calls, tool calls, saves, checks, errors
  transcript.json the full conversation, for replay and debugging
  run.json        status, model, timings, token use, containers and the final check result
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class RunStore:
    def __init__(self, root: Path, run_id: str | None = None):
        self.id = run_id or datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
        self.dir = root / self.id
        for sub in ('input', 'outputs', 'scouts', 'code'):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self._code_count = 0
        self.record: dict = {'id': self.id, 'status': 'running', 'started_at': _now()}
        self.save_record()

    @property
    def outputs(self) -> Path:
        return self.dir / 'outputs'

    def add_input(self, source: Path) -> Path:
        target = self.dir / 'input' / source.name
        shutil.copy2(source, target)
        return target

    def event(self, name: str, **data):
        line = {'t': round(time.monotonic() - self.started, 2), 'at': _now(), 'event': name, **data}
        with open(self.dir / 'events.jsonl', 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(line, default=str) + '\n')

    def write_json(self, relative: str, data) -> Path:
        path = self.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding='utf-8')
        return path

    def save_code(self, who: str, code: str, outputs: list) -> str:
        self._code_count += 1
        name = f'{self._code_count:03d}-{who}.py'
        printed = '\n'.join(str(o.get('logs', '')) if isinstance(o, dict) else str(o) for o in outputs or [])
        body = code + (
            '\n\n# ---- output ----\n' + '\n'.join('# ' + line for line in printed.splitlines()) if printed else ''
        )
        (self.dir / 'code' / name).write_text(body, encoding='utf-8')
        return name

    def replace_outputs(self, files: dict[str, bytes]) -> list[str]:
        """Make outputs/ exactly this set of files. Writes to a staging folder first, so a failure leaves the
        previous outputs untouched, and files the agent deleted do not linger."""
        staging = self.dir / 'outputs.staging'
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        for relative, data in files.items():
            target = (staging / relative).resolve()
            if staging.resolve() not in target.parents:
                shutil.rmtree(staging, ignore_errors=True)
                raise ValueError(f'Refusing to save outside the outputs folder: {relative}')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        previous = self.dir / 'outputs.previous'
        shutil.rmtree(previous, ignore_errors=True)
        try:
            _rename(self.outputs, previous)
        except PermissionError as error:  # Windows: a file in outputs/ is open, usually in Excel
            shutil.rmtree(staging, ignore_errors=True)
            raise PermissionError(
                f'Could not replace {self.outputs}: a file in it is open in another program, usually Excel. '
                'Close it and save again.'
            ) from error
        try:
            _rename(staging, self.outputs)
        except OSError:
            _rename(previous, self.outputs)  # put the last good copy back
            raise
        shutil.rmtree(previous, ignore_errors=True)
        return sorted(files)

    def save_record(self, **updates):
        self.record.update(updates)
        self.record['elapsed_seconds'] = round(time.monotonic() - self.started, 1)
        self.write_json('run.json', self.record)


def _rename(source: Path, target: Path, attempts: int = 3):
    """Rename, retrying briefly: on Windows, antivirus or OneDrive can hold a file it just saw being written."""
    for attempt in range(attempts):
        try:
            source.rename(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')
