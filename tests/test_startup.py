"""Bootstrap handoff preserves paths, flags and application exit status."""

import hashlib
import json
import os
import subprocess
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import start


@pytest.mark.parametrize('exit_code', [0, 7])
def test_windows_bootstrap_preserves_spaced_paths_and_exit_code(tmp_path, monkeypatch, exit_code):
    root = tmp_path / 'Doc Intelligence'
    python = root / '.venv' / 'Scripts' / 'python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    (root / 'requirements.txt').write_text('')
    (root / '.env').touch()
    identity = 'Python 3.12 test interpreter'
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()
    (python.parent.parent / '.requirements-stamp').write_text(fingerprint)
    script = root / 'scripts' / 'start.py'
    monkeypatch.setattr(start, 'ROOT', root)
    monkeypatch.setattr(start, '__file__', str(script))
    # Do not mutate the global os.name: pathlib must retain the host path flavour.
    monkeypatch.setattr(start, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(start.sys, 'argv', [str(script), '--bootstrap', '--no-browser', '--restart'])
    monkeypatch.setattr(start.subprocess, 'check_output', lambda *a, **kw: identity)
    commands = []

    def call(command):
        commands.append(command)
        return exit_code

    monkeypatch.setattr(start.subprocess, 'call', call)
    with pytest.raises(SystemExit) as result:
        start.bootstrap()
    assert result.value.code == exit_code
    assert commands == [[str(python), str(script.resolve()), '--no-browser', '--restart']]


def test_posix_bootstrap_replaces_process(tmp_path, monkeypatch):
    root = tmp_path / 'Doc Intelligence'
    python = root / '.venv' / 'bin' / 'python'
    python.parent.mkdir(parents=True)
    python.touch()
    (root / 'requirements.txt').write_text('')
    (root / '.env').touch()
    identity = 'Python 3.12 test interpreter'
    (python.parent.parent / '.requirements-stamp').write_text(hashlib.sha256(identity.encode()).hexdigest())
    monkeypatch.setattr(start, 'ROOT', root)
    monkeypatch.setattr(start, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(start.sys, 'argv', [start.__file__, '--bootstrap', '--no-browser'])
    monkeypatch.setattr(start.subprocess, 'check_output', lambda *a, **kw: identity)

    def execv(executable, command):
        assert executable == str(python)
        assert command == [str(python), str(Path(start.__file__).resolve()), '--no-browser']
        raise SystemExit(23)

    monkeypatch.setattr(start.os, 'execv', execv, raising=False)
    with pytest.raises(SystemExit) as result:
        start.bootstrap()
    assert result.value.code == 23


@pytest.mark.skipif(os.name != 'nt', reason='Requires native Windows process argument handling')
def test_native_windows_bootstrap_handoff(tmp_path, monkeypatch):
    root = tmp_path / 'Doc Intelligence'
    folder = root / '.venv'
    venv.create(folder, with_pip=False)
    python = folder / 'Scripts' / 'python.exe'
    identity = subprocess.check_output(
        [str(python), '-c', 'import sys; print(sys.version); print(sys.executable)'], text=True
    )
    (root / 'requirements.txt').write_text('')
    (folder / '.requirements-stamp').write_text(hashlib.sha256(identity.encode()).hexdigest())
    (root / '.env').touch()
    script = root / 'scripts' / 'start.py'
    script.parent.mkdir()
    script.write_text(
        'import json, pathlib, sys\n'
        'pathlib.Path(__file__).with_suffix(".json").write_text(json.dumps(sys.argv))\n'
        'sys.exit(7)\n'
    )
    monkeypatch.setattr(start, 'ROOT', root)
    monkeypatch.setattr(start, '__file__', str(script))
    monkeypatch.setattr(start.sys, 'argv', [str(script), '--bootstrap', '--no-browser', '--restart'])
    with pytest.raises(SystemExit) as result:
        start.bootstrap()
    assert result.value.code == 7
    assert json.loads(script.with_suffix('.json').read_text()) == [str(script.resolve()), '--no-browser', '--restart']
