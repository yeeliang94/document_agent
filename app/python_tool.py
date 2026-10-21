"""Opt-in run_python tool: model-written Python in an operating-system sandbox scoped to one job.

Off unless DI_PYTHON_TOOL=1. Two layers keep the code inside the job:
- the OS sandbox (macOS sandbox-exec) denies network, process creation, reads outside the job's
  inputs/, template/ and working/ folders plus the Python installation, and writes outside this
  run's staging folder and working/python/scratch;
- an audit hook inside the child gives clear errors for the same rules. It is not a boundary on its own.
Outputs are edited as staged copies and committed only when the script succeeds, so a killed or
failed run never leaves a half-written workbook.
"""

import asyncio
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import sysconfig
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import get_args

from openpyxl import load_workbook
from pydantic_ai import RunContext

from .config import ROOT, Settings
from .datasets import dataset_rows, fresh_id, require_current
from .execution import acquire_job_lock, run_killable
from .files import atomic_bytes
from .harness_tools import Kind, observed_kind
from .regions import accounting_version
from .state import encode, identity
from .tools import JobDeps, registrar, template_changes

SANDBOX_EXEC = '/usr/bin/sandbox-exec'
MAX_ROWS = 100_000
# Software locations a sandboxed interpreter may read; none hold job or user data.
SYSTEM_READS = ('/System', '/usr', '/Library/Frameworks', '/opt/homebrew', '/private/var/db/dyld')

BOOT = r'''
import json, linecache, os, resource, sys, traceback

config = json.loads(sys.stdin.buffer.read())
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
sys.dont_write_bytecode = True
for limit, soft in (
    (resource.RLIMIT_CPU, config['cpu_seconds']),
    (resource.RLIMIT_FSIZE, config['file_bytes']),
    (resource.RLIMIT_NOFILE, 256),
    (resource.RLIMIT_CORE, 0),
):
    ceiling = resource.getrlimit(limit)[1]
    cap = lambda value: value if ceiling == resource.RLIM_INFINITY else min(value, ceiling)
    # A CPU soft limit sends SIGXCPU first; the hard limit one second later kills.
    resource.setrlimit(limit, (cap(soft), cap(soft + 1 if limit == resource.RLIMIT_CPU else soft)))

import openpyxl  # noqa: F401  Loaded before the hook so its data files need no exception.
try:
    import pandas  # noqa: F401  Only offered when installed; it is not an application dependency.
except ImportError:
    pass

READ = tuple(config['read'])
WRITE = tuple(config['write'])
BLOCKED = (
    'socket.', 'subprocess.', 'os.exec', 'os.posix_spawn', 'os.spawn', 'os.fork', 'os.forkpty', 'os.system',
    'os.kill', 'os.killpg', 'ctypes.', 'pty.', 'webbrowser.', 'urllib.', 'http.', 'ftplib.', 'smtplib.',
    'imaplib.', 'poplib.', 'nntplib.', 'telnetlib.', 'sys.remote_exec', 'winreg.', '_winapi.',
)
WRITE_EVENTS = {
    'os.remove', 'os.rename', 'os.mkdir', 'os.rmdir', 'os.chmod', 'os.chown', 'os.chflags', 'os.lchflags',
    'os.link', 'os.symlink', 'os.truncate', 'os.utime', 'os.setxattr', 'os.removexattr', 'shutil.rmtree',
}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def inside(path, roots):
    try:
        real = os.path.realpath(os.fsdecode(path))
    except (TypeError, ValueError):
        return False
    return any(real == root or real.startswith(root + os.sep) for root in roots)


def deny(event, detail=''):
    raise PermissionError(f'run_python sandbox blocks {event} {detail}'.strip())


def hook(event, args):
    if event.startswith(BLOCKED) or (event == 'import' and args[0].split('.')[0] in ('ctypes', '_ctypes')):
        deny(event)
    if event == 'open':
        path, mode, flags = args
        if path is None or isinstance(path, int):
            return
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or bool((flags or 0) & WRITE_FLAGS)
        if not inside(path, WRITE if writing else READ + WRITE):
            deny('writing' if writing else 'reading', os.fsdecode(path))
    elif event in WRITE_EVENTS:
        for value in args:
            if isinstance(value, (str, bytes, os.PathLike)) and not inside(value, WRITE):
                deny(event, os.fsdecode(value))
    elif event in ('os.listdir', 'os.scandir') and args and args[0] is not None and not isinstance(args[0], int):
        if not inside(args[0], READ + WRITE):
            deny(event, os.fsdecode(args[0]))


sys.addaudithook(hook)
with open(config['datasets'], encoding='utf-8') as handle:
    DATASETS = json.load(handle)


def save_dataset(rows, kinds=None, excluded=None):
    """Save rows ({values, sources, evidence?, issues?, resolved?}) as the result dataset of this run."""
    plain = lambda value: value.isoformat() if hasattr(value, 'isoformat') else str(value)
    with open(config['result'], 'w', encoding='utf-8') as handle:
        json.dump({'rows': list(rows), 'kinds': kinds or {}, 'excluded': excluded or {}}, handle, default=plain)


namespace = {'__name__': '__main__', 'DATASETS': DATASETS, 'save_dataset': save_dataset, **config['names']}
code = config['code']
linecache.cache['<run_python>'] = (len(code), None, code.splitlines(True), '<run_python>')
os.chdir(config['names']['SCRATCH'])
try:
    exec(compile(code, '<run_python>', 'exec'), namespace)
except SystemExit as error:
    sys.exit(error.code)
except BaseException:
    lines = traceback.format_exc().splitlines(True)
    # Drop the bootstrap frame; the model only needs its own code's traceback.
    sys.stderr.write(''.join(line for line in lines if 'File "<string>"' not in line))
    sys.exit(1)
'''


def _real(path):
    return os.path.realpath(path)


def python_reads(data_dir):
    """The interpreter and its libraries; never sys.path, which the launcher extends with the application folder."""
    paths = {sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(sys.executable)}
    for scheme in (sysconfig.get_paths(), sysconfig.get_paths(vars={'base': sys.base_prefix})):
        paths.update(scheme[key] for key in ('stdlib', 'platstdlib', 'purelib', 'platlib'))
    reads = sorted({_real(p) for p in paths if os.path.isdir(p)} | {p for p in SYSTEM_READS if os.path.exists(p)})
    # A library folder that contains the application (.env) or the job store would expose every job.
    for private in (_real(ROOT), _real(data_dir)):
        if any(private == p or private.startswith(p.rstrip(os.sep) + os.sep) for p in reads):
            raise RuntimeError(f'run_python cannot sandbox {private}: it lies inside a Python library folder')
    return reads


def _quote(path):
    return '"' + path.replace('\\', '\\\\').replace('"', '\\"') + '"'


def sandbox_profile(reads, writes, executables):
    def subpaths(paths):
        return ' '.join(f'(subpath {_quote(p)})' for p in paths)

    return '\n'.join(
        [
            '(version 1)',
            '(deny default)',
            # Path existence is visible; file contents outside the read list are not.
            '(allow file-read-metadata)',
            '(allow sysctl-read)',
            f'(allow process-exec {subpaths(executables)})',
            f'(allow file-read* (literal "/") (literal "/dev/urandom") (literal "/dev/null") '
            f'(regex #"^/private/etc/.*mime\\.types$") {subpaths(reads)})',
            f'(allow file-write* (literal "/dev/null") {subpaths(writes)})',
        ]
    )


def sandbox_command(reads, writes, data_dir):
    if sys.platform != 'darwin' or not os.path.exists(SANDBOX_EXEC):
        # No unsandboxed fallback: payroll data must not meet unconfined model code.
        raise RuntimeError('run_python needs an OS sandbox; only macOS sandbox-exec is supported in this prototype')
    executables = sorted({_real(os.path.dirname(sys.executable)), _real(sys.base_prefix), _real(sys.prefix)})
    profile = sandbox_profile([*python_reads(data_dir), *reads], writes, executables)
    return [SANDBOX_EXEC, '-p', profile, sys.executable, '-I', '-c', BOOT]


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _staged_bytes(path):
    """A staged file's bytes, refusing links: the unsandboxed parent must never read where a link points.
    O_NONBLOCK keeps a planted named pipe from blocking the open; the type check below then refuses it."""
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ValueError(f'{path.name} is not a plain file the script wrote ({error.strerror})') from None
    with os.fdopen(handle, 'rb') as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f'{path.name} is a link or special file; outputs must be plain files')
        return file.read()


def _staged_files(folder):
    """Plain top-level files of a staging folder the script could write; any link or folder is refused."""
    if not stat.S_ISDIR(os.lstat(folder).st_mode):
        raise ValueError(f'{folder.name} was replaced by a link; nothing was saved')
    files = {}
    with os.scandir(folder) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            if not entry.is_file(follow_symlinks=False):
                raise ValueError(f'outputs/{entry.name} is a link or folder; outputs must be plain files')
            files[entry.name] = _staged_bytes(Path(entry.path))
    return files


def _prepare(deps, run_id, datasets):
    if deps.actor != 'main':
        raise ValueError('run_python is available to the main agent only')
    root = Path(_real(deps.root))
    run = root / 'working/python/runs' / run_id
    staged, scratch = run / 'outputs', root / 'working/python/scratch'
    staged.mkdir(parents=True)
    scratch.mkdir(parents=True, exist_ok=True)
    originals = {}
    for item in sorted((root / 'outputs').glob('*')):
        if item.is_file():
            shutil.copy2(item, staged / item.name)
            originals[item.name] = _digest(item)
    exported = {}
    for dataset in datasets:
        require_current(deps.state, dataset)
        info, rows = dataset_rows(deps.state, dataset)
        exported[dataset] = {'columns': info.get('columns'), 'kinds': info.get('kinds', {}), 'rows': rows}
    atomic_bytes(run / 'datasets.json', encode(exported).encode())
    names = {
        'INPUTS': str(root / 'inputs'),
        'TEMPLATE': str(root / 'template'),
        'WORKING': str(root / 'working'),
        'OUTPUTS': str(staged),
        'SCRATCH': str(scratch),
    }
    reads = [str(root / folder) for folder in ('inputs', 'template', 'working')]
    writes = [str(run), str(scratch)]
    data_dir = (deps.settings or Settings()).data_dir
    return {
        'run': run,
        'staged': staged,
        'originals': originals,
        'argv': sandbox_command(reads, writes, data_dir),
        'env': {'PATH': '/usr/bin:/bin', 'HOME': str(scratch), 'TMPDIR': str(scratch), 'LANG': 'en_US.UTF-8'},
        'config': {
            'read': [*python_reads(data_dir), *reads],
            'write': writes,
            'names': names,
            'datasets': str(run / 'datasets.json'),
            'result': str(run / 'result.json'),
            'file_bytes': 512 * 1024 * 1024,
        },
    }


def save_python_dataset(state, datasets, payload, code_sha256):
    """Register rows a script derived from input datasets, refusing invented or silently dropped sources."""
    if not datasets:
        raise ValueError('Pass the input datasets to run_python before saving a derived dataset')
    parent, parent_rows = dataset_rows(state, datasets[0])
    inherited = {}
    for dataset in datasets:
        for row in dataset_rows(state, dataset)[1]:
            for source in row['sources']:
                inherited.setdefault(source, set()).update(row.get('issues') or [])
    allowed = set(inherited)
    rows, kinds, excluded = payload.get('rows'), payload.get('kinds') or {}, payload.get('excluded') or {}
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError(f'save_dataset needs a list of at most {MAX_ROWS} rows')
    saved, columns = [], {}
    for number, row in enumerate(rows):
        values, sources = row.get('values') if isinstance(row, dict) else None, (row or {}).get('sources')
        if not isinstance(values, dict) or any(
            not isinstance(key, str) or not (value is None or isinstance(value, (str, int, float, bool)))
            for key, value in values.items()
        ):
            raise ValueError(f'Row {number}: values must map field names to text, numbers, booleans or null')
        if not isinstance(sources, list) or not sources or not set(sources) <= allowed:
            raise ValueError(f'Row {number}: sources must be a non-empty list of input dataset sources')
        issues, evidence = row.get('issues') or [], row.get('evidence') or {}
        resolved = row.get('resolved') or {}
        if not all(isinstance(issue, str) for issue in issues) or not isinstance(evidence, dict):
            raise ValueError(f'Row {number}: issues must be text and evidence a mapping')
        # Problems found upstream follow the records they belong to unless the script resolves them by name.
        carried = set().union(*(inherited[source] for source in sources))
        if not isinstance(resolved, dict) or any(
            issue not in carried or not isinstance(reason, str) or not reason.strip()
            for issue, reason in resolved.items()
        ):
            raise ValueError(f'Row {number}: resolved must map issues of its source records to a non-empty reason')
        issues = list(dict.fromkeys([*issues, *sorted(carried - set(resolved))]))
        if resolved:
            evidence = evidence | {'resolved_issues': resolved}
        columns.update(dict.fromkeys(values))
        saved.append(
            {
                'id': str(row.get('id') or identity(sorted(sources), number)),
                'values': values,
                'evidence': evidence,
                'sources': sorted(set(sources)),
                'issues': issues,
            }
        )
    # Rejected rows are later keyed by ID, so a shared ID would let one row's reasons replace another's.
    duplicates = sorted(key for key, count in Counter(row['id'] for row in saved).items() if count > 1)
    if duplicates:
        raise ValueError(f'Row IDs must be unique; repeated: {", ".join(duplicates[:5])}')
    surviving = {source for row in saved for source in row['sources']}
    missing = {source for row in parent_rows for source in row['sources']} - surviving - set(excluded)
    if missing:
        raise ValueError(
            f'{len(missing)} source records of {datasets[0]} are neither in the result nor in excluded; '
            'pass excluded={source: reason} for records deliberately left out'
        )
    if any(source not in allowed or not str(reason).strip() for source, reason in excluded.items()):
        raise ValueError('excluded must map input dataset sources to a non-empty reason')
    if any(kind not in get_args(Kind) for kind in kinds.values()):
        raise ValueError(f'kinds must be one of {", ".join(get_args(Kind))}')
    parent_kinds = parent.get('kinds', {})
    kinds = {
        column: kinds.get(column) or parent_kinds.get(column) or observed_kind([r['values'].get(column) for r in saved])
        for column in columns
    }
    revision = state.revision('instruction_revision')
    operations = [{'operation': 'python', 'datasets': list(datasets), 'code_sha256': code_sha256}]
    result_id = fresh_id(state, identity(datasets, code_sha256, revision, saved, kinds, excluded))
    exclusions = [*state.get('exclusions', datasets[0], []), *((s, str(r)) for s, r in excluded.items())]
    state.put('rows', result_id, saved)
    result = {
        **parent,
        'id': result_id,
        'parent': datasets[0],
        'rows': len(saved),
        'exceptions': sum(bool(row['issues']) for row in saved),
        'instruction_revision': revision,
        'columns': list(columns) or parent.get('columns', []),
        'kinds': kinds,
    }
    state.put('dataset', result_id, result)
    state.put(
        'mapping', result_id, {'dataset': datasets[0], 'operations': operations, 'instruction_revision': revision}
    )
    state.put('exclusions', result_id, [(s, r) for s, r in exclusions if s not in surviving])
    return result


def _commit(deps, prepared, code, datasets):
    state, root = deps.state, Path(_real(deps.root))
    staged, originals = prepared['staged'], prepared['originals']
    files = _staged_files(staged)
    for name in files:
        if name not in originals:
            raise ValueError(f'New output {name} was not saved; create outputs with create_output first')
    changed = [name for name, data in files.items() if hashlib.sha256(data).hexdigest() != originals[name]]
    template = deps.resolve(next(f['path'] for f in deps.current()['files'] if f['role'] == 'template'))
    for name in changed:
        if name.endswith('.xlsx'):
            # Without current requirements nothing says which cells a script may change.
            specification = state.get('requirement', identity('outputs/' + name))
            if (
                accounting_version(state) != 2
                or not specification
                or specification.get('stale')
                or specification['instruction_revision'] != state.revision('instruction_revision')
            ):
                raise ValueError(f'Save current output requirements for outputs/{name} before editing it with Python')
            try:
                load_workbook(io.BytesIO(files[name])).close()
            except Exception as error:
                raise ValueError(f'{name} no longer opens as a workbook: {error}') from None
            # The same checks as final validation, before a good output can be replaced.
            failures, _ = template_changes(deps, 'outputs/' + name, template, files[name])
            if failures:
                raise ValueError(f'{name} was not saved: ' + '; '.join(failures[:10]))
        if any(
            op.get('status') == 'prepared' and op.get('result', {}).get('path') == 'outputs/' + name
            for _, op in state.all('operation')
        ):
            raise ValueError(f'outputs/{name} has an interrupted operation; recover it before editing')
    result_file = prepared['run'] / 'result.json'
    dataset = None
    code_sha256 = hashlib.sha256(code.encode()).hexdigest()
    if os.path.lexists(result_file):
        dataset = save_python_dataset(state, datasets, json.loads(_staged_bytes(result_file)), code_sha256)
    for name in changed:
        atomic_bytes(root / 'outputs' / name, files[name])
    audit = state.artifact(
        'python-runs',
        {'code': code, 'datasets': datasets, 'outputs_changed': changed, 'dataset': dataset and dataset['id']},
    )
    summary = (
        {key: dataset[key] for key in ('id', 'parent', 'rows', 'exceptions', 'columns', 'kinds')} if dataset else None
    )
    return {
        'outputs_changed': ['outputs/' + name for name in changed],
        'outputs_removed_ignored': sorted(set(originals) - set(files)),
        'dataset': summary,
        'audit': audit,
        **({'next_action': 'Changed outputs must pass validate_output again.'} if changed else {}),
    }


async def _settled(function, *args):
    """Run a thread to its end even if cancelled: a commit cannot be stopped midway, so the job lock and
    staging files must outlive it. Cancellation is re-raised once the thread has finished."""
    future = asyncio.ensure_future(asyncio.to_thread(function, *args))
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        raise asyncio.CancelledError
    return future.result()


async def run_python(deps, code, datasets):
    settings = deps.settings or Settings()
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    try:
        prepared = await _settled(_prepare, deps, run_id, datasets)
        try:
            finished = await run_killable(
                prepared['argv'],
                json.dumps(prepared['config'] | {'code': code, 'cpu_seconds': settings.tool_timeout}).encode(),
                settings.tool_timeout,
                cwd=prepared['config']['names']['SCRATCH'],
                env=prepared['env'],
                limit=max(1024, settings.tool_result_bytes // 4),
            )
        except TimeoutError:
            raise TimeoutError(
                f'run_python exceeded its {settings.tool_timeout}s deadline; nothing was saved. Narrow the work.'
            ) from None
        result = {
            'ok': finished.returncode == 0,
            'returncode': finished.returncode,
            'stdout': finished.stdout.decode(errors='replace'),
            'stderr': finished.stderr.decode(errors='replace'),
            'duration_ms': round((time.monotonic() - started) * 1000),
        }
        if finished.stdout_bytes > len(finished.stdout):
            result['stdout_truncated'] = f'first {len(finished.stdout)} of {finished.stdout_bytes} bytes shown'
        if finished.stderr_bytes > len(finished.stderr):
            result['stderr_truncated'] = f'last {len(finished.stderr)} of {finished.stderr_bytes} bytes shown'
        if finished.returncode:
            return result | {'saved': False, 'note': 'The script failed; no outputs or dataset were saved.'}
        try:
            return result | {'saved': True} | await _settled(_commit, deps, prepared, code, datasets)
        except ValueError as error:
            return result | {'ok': False, 'saved': False, 'error': str(error)}
    finally:
        await asyncio.to_thread(shutil.rmtree, Path(_real(deps.root)) / 'working/python/runs' / run_id, True)


def register_python_tool(agent):
    tool = registrar(agent, False, set())

    @tool
    async def run_python_tool(ctx: RunContext[JobDeps], code: str, datasets: list[str] | None = None) -> dict:
        """Run Python 3 with openpyxl (pandas only if installed) against this job. No network or subprocesses.
        Variables: INPUTS and TEMPLATE (read-only source folders), WORKING (read-only job working folder),
        OUTPUTS (editable copies of existing outputs, saved only if the script succeeds), SCRATCH (persistent
        writable folder). DATASETS maps each requested dataset ID to {columns, kinds, rows}; each row has
        values, sources, evidence and issues. save_dataset(rows, kinds=None, excluded=None) saves a derived
        dataset: every row keeps the sources it came from, and every source of the first dataset must survive
        or appear in excluded={source: reason}. Issues of a row's sources carry over unless the row lists them
        in resolved={issue: reason}. Edited workbooks may change only cells inside their input areas. print() what you need; output is truncated. Outputs still
        need validate_output, and populate_template from a saved dataset is what records source accounting."""
        datasets = list(datasets or [])

        async def work():
            lock = await acquire_job_lock(ctx.deps)
            try:
                ctx.deps.check_stop()
                return await run_python(ctx.deps, code, datasets)
            finally:
                lock.release()

        return await ctx.deps.async_action('run_python', {'code': code, 'datasets': datasets}, work())
