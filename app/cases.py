"""Past corrections kept as tests of a playbook: one case per lesson the agent learned.

A case is what is needed to do the same piece of work again and see whether the lesson still holds: the run's
input files and goal, the spot the user commented on, and what stood at that spot once the user's correction was
made and the run finished with its checks passing. Cases live under <data>/playbooks/<playbook>/cases/<id>/:

  case.json   the lesson, the spot, the accepted value, where it came from, and every replay's result
  files/      the run's input files and template, as that run had them
  expected/   the output file the spot is in, as accepted

They belong to the playbook, not the run, so deleting the run does not break them. The app keeps the list short by
itself (see MAX_CASES and app/curator.py); a person never has to tidy it.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import threading
import uuid
from pathlib import Path

from . import preview
from .cleaning import now

MAX_CASES = 20  # active cases per playbook; each one is a full run when replayed
MAX_CASE_BYTES = 60 * 1024**2  # the input files of one case; a larger run is not kept as a case
KEPT_DAYS = 30  # a retired case stays on disk this long, so a wrong retirement can be undone
# One writer at a time for case.json. A replay adds its result while a review of the list may retire the same case:
# each reads the whole file, changes it and writes it back, so without this one would undo the other.
LOCK = threading.Lock()


def _home(store, playbook_id: str) -> Path:
    return store.playbooks.skills.folder(playbook_id).parent / 'cases'  # the id is checked there


def folder(store, playbook_id: str, case_id: str) -> Path:
    if not case_id.isalnum():
        raise KeyError(case_id)
    return _home(store, playbook_id) / case_id


def read(store, playbook_id: str, case_id: str) -> dict:
    try:
        return json.loads((folder(store, playbook_id, case_id) / 'case.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise KeyError(case_id) from None


def _write(store, case: dict) -> None:
    path = folder(store, case['playbook_id'], case['id']) / 'case.json'
    partial = path.with_suffix('.partial')
    partial.write_text(json.dumps(case, indent=2, ensure_ascii=False), encoding='utf-8')
    partial.replace(path)


def cases(store, playbook_id: str, status: str | None = 'active') -> list[dict]:
    """A playbook's cases, oldest first. status=None lists the retired ones too."""
    home, found = _home(store, playbook_id), []
    for path in sorted(home.glob('*/case.json')) if home.is_dir() else []:
        try:
            case = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue  # half written or damaged: not a case
        if status is None or case['status'] == status:
            found.append(case)
    return sorted(found, key=lambda c: c['created_at'])


def grid(path: Path, spot: dict) -> dict[int, dict[int, str]]:
    """The filled cells of the sheet (or CSV) a spot is in, as text: {row: {column: value}}, both counted from 1."""
    from . import feedback

    rows: dict[int, dict[int, str]] = {}
    if 'sheet' in spot:
        for (sheet, cell), value in feedback._cells(path).items():
            if sheet == spot['sheet']:
                row, column = feedback._row_column(cell)
                rows.setdefault(row, {})[column] = str(value)
    else:
        for r, line in enumerate(csv.reader(io.StringIO(_whole(path))), 1):
            rows[r] = {c: value for c, value in enumerate(line, 1) if value != ''}
    return rows


def _whole(path: Path) -> str:
    """All of a text or CSV file. The preview reads only the start of a large one; a test must see all of it, or a
    record a replay moved further down would count as missing."""
    with open(path, encoding='utf-8-sig', errors='replace') as handle:
        return handle.read()


def lines(path: Path) -> list[str]:
    """The lines of a text file or a Word document, as a comment on a line counts them. Keeping a test and scoring
    its replay both read through here, so the two sides of the comparison are read the same way."""
    return (preview._docx(path) if path.suffix.lower() == '.docx' else _whole(path)).split('\n')


def scoreable(comment: dict) -> bool:
    """A case needs a spot whose content can be read back and compared: a cell or a line of an output file."""
    in_outputs = comment['path'].startswith('outputs/')
    sheet_cell = (
        comment['kind'] == 'cell'
        and ':' not in comment['cell']  # a block has no one value to read back
        and ('sheet' in comment or Path(comment['path']).suffix.lower() == '.csv')
    )
    return in_outputs and (sheet_cell or comment['kind'] == 'line')


def capture(store, job: dict, round_record: dict, comment: dict, lesson: str, version: int) -> dict | None:
    """Keep one corrected spot of a finished run as a case of its playbook. Returns the case, or None (with the
    reason logged on the playbook) when this correction cannot be turned into one."""
    from . import feedback  # imported here: feedback reads the files, and nothing in it needs cases

    playbook_id = (job.get('playbook') or {}).get('id') or job.get('module') or ''
    log = store.playbooks.event
    where = {'job_id': job['id'], 'round': round_record['number'], 'n': comment['n']}
    if not scoreable(comment):
        log(
            playbook_id,
            'case.skipped',
            'No test kept: the comment is not on one cell or one line of an output',
            **where,
        )
        return None
    root = store.path(job['id'])
    size = sum((root / entry['path']).stat().st_size for entry in job['files'] if (root / entry['path']).is_file())
    if size > MAX_CASE_BYTES:
        log(playbook_id, 'case.skipped', f'No test kept: the run has more than {MAX_CASE_BYTES // 1024**2} MB of input files', **where)  # fmt: skip
        return None
    spot = {k: comment[k] for k in ('path', 'kind', 'sheet', 'cell', 'line', 'where') if k in comment}
    try:
        feedback.locate(root, [spot])  # refuses a spot that is not there
        row, key = None, None
        # The accepted value in full, read the way a replay's is (app/replay.py): what the screen shows is cut short.
        if spot['kind'] == 'line':
            expected = lines(preview.job_file(root, spot['path']))[spot['line'] - 1]
        else:
            number, column = feedback._row_column(spot['cell'])
            sheet = grid(preview.job_file(root, spot['path']), spot)
            cells = sheet.get(number, {})
            expected = cells.get(column, '')
            row = [cells[c] for c in sorted(cells)]
            # Which record the spot belongs to: the leftmost cell of its row whose value no other row has in that
            # column (an ID, a name; not a department that many rows share). A replay may lay rows out in another
            # order; the record is found by this. A row with no such cell has no key.
            for candidate in sorted(cells):
                shared = sum(other.get(candidate) == cells[candidate] for other in sheet.values())
                if candidate != column and shared == 1 and not cells[candidate].startswith('='):
                    key = {'column': candidate, 'value': cells[candidate]}
                    break
    except (ValueError, OSError, IndexError):
        log(playbook_id, 'case.skipped', 'No test kept: the corrected spot could not be read back', **where)
        return None
    case = {
        'id': uuid.uuid4().hex,
        'playbook_id': playbook_id,
        'status': 'active',
        'created_at': now(),
        'lesson': lesson,
        'version': version,
        **where,
        'comment': comment['text'],
        'was': comment.get('now'),
        'goal': job.get('instructions') or '',
        'period': job.get('period') or '',
        'spot': spot,
        'expected': expected,
        'expected_row': row,
        'row_key': key,
        'files': job['files'],  # the run's own manifest: a replay starts from exactly these
        'replays': [],
    }
    home = folder(store, playbook_id, case['id'])
    try:
        for entry in job['files']:
            target = home / 'files' / entry['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / entry['path'], target)
        target = home / 'expected' / spot['path'].removeprefix('outputs/')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(preview.job_file(root, spot['path']), target)
        _write(store, case)
    except OSError as error:
        shutil.rmtree(home, ignore_errors=True)
        log(playbook_id, 'case.skipped', f'No test kept: its files could not be copied ({type(error).__name__})', 'WARN', **where)  # fmt: skip
        return None
    log(playbook_id, 'case.captured', f'Kept a test for: {lesson[:120]}', case=case['id'], version=version, **where)
    keep_within_limit(store, playbook_id)
    sweep(store, playbook_id)  # with every new test, the files of long-retired ones go
    return case


def retire(store, playbook_id: str, case_id: str, reason: str, by: str = 'app') -> dict:
    """Stop replaying a case. Its files stay for KEPT_DAYS, so it can be brought back."""
    with LOCK:
        case = read(store, playbook_id, case_id)
        retired = case['status'] == 'active'
        if retired:
            case.update(status='retired', retired_at=now(), retired_reason=reason, retired_by=by)
            _write(store, case)
    if retired:
        store.playbooks.event(playbook_id, 'case.retired', f'Retired a test: {reason}', case=case_id, by=by, lesson=case['lesson'][:200])  # fmt: skip
    return case


def restore(store, playbook_id: str, case_id: str) -> dict:
    with LOCK:
        case = read(store, playbook_id, case_id)
        restored = case['status'] == 'retired'
        if restored:
            case['status'] = 'active'
            for key in ('retired_at', 'retired_reason', 'retired_by'):
                case.pop(key, None)
            _write(store, case)
    if restored:
        store.playbooks.event(playbook_id, 'case.restored', 'Brought a retired test back', case=case_id)
        keep_within_limit(store, playbook_id, spare=case_id)
    return case


def keep_within_limit(store, playbook_id: str, spare: str = '') -> None:
    """Hold a playbook to MAX_CASES. The first to go is the oldest case that has never caught anything; when
    every case has, the oldest. The newest case (or the one just brought back) is never the one retired."""
    active = cases(store, playbook_id)
    newest = spare or (active[-1]['id'] if active else '')
    while len(active) > MAX_CASES:
        candidates = [c for c in active if c['id'] != newest]
        idle = [c for c in candidates if not any(r['passed'] is False for r in c['replays'])]
        gone = (idle or candidates)[0]
        retire(store, playbook_id, gone['id'], f'the playbook holds at most {MAX_CASES} tests, and this was the oldest {"that never caught anything" if idle else "one"}')  # fmt: skip
        active = [c for c in active if c['id'] != gone['id']]


def add_replay(store, playbook_id: str, case_id: str, result: dict) -> dict:
    with LOCK:
        case = read(store, playbook_id, case_id)
        case['replays'] = [*case['replays'], result][-20:]
        _write(store, case)
    return case


def sweep(store, playbook_id: str, today: str | None = None) -> int:
    """Delete the files of cases retired more than KEPT_DAYS ago; returns how many went."""
    from datetime import date, timedelta

    limit = (date.fromisoformat((today or now())[:10]) - timedelta(days=KEPT_DAYS)).isoformat()
    gone = 0
    for case in cases(store, playbook_id, None):
        if case['status'] == 'retired' and case.get('retired_at', '9999')[:10] < limit:
            shutil.rmtree(folder(store, playbook_id, case['id']), ignore_errors=True)
            gone += 1
    return gone
