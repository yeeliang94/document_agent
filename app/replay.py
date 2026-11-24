"""Replaying a playbook's kept tests (app/cases.py) against a version of its instructions.

A "check" replays every active case once. Each case becomes a hidden job: the case's own input files and goal, the
instructions under test, the playbook's skills as they are now, no earlier runs to look at (the corrected run would
give the answer away) and no questions to the user. The job goes through the same queue and engine as any run.
When it ends, the spot the user once corrected is read from the new output and compared with what the user
accepted. A check is done when every case has a result. Its record is kept under
<data>/playbooks/<playbook>/checks/<id>.json and in the playbook's activity log.

The instructions under test are either a saved version, or a candidate text that is not saved yet (a tidy that is
applied only if its check passes, see app/tidy.py).
"""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from pathlib import Path

from . import cases, feedback, preview
from .cleaning import now

LOCK = threading.Lock()  # one writer at a time for a check's record
KEPT_CHECKS = 10  # records kept per playbook; the jobs of a check are removed once a later check starts
ENDED = {'completed', 'partial', 'failed', 'cancelled', 'paused', 'waiting'}  # a replay job is never continued


def _home(store, playbook_id: str) -> Path:
    return store.playbooks.skills.folder(playbook_id).parent / 'checks'


def _write(store, check: dict) -> None:
    path = _home(store, check['playbook_id']) / f'{check["id"]}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix('.partial')
    partial.write_text(json.dumps(check, indent=2, ensure_ascii=False), encoding='utf-8')
    partial.replace(path)


def read(store, playbook_id: str, check_id: str) -> dict:
    if not check_id.isalnum():
        raise KeyError(check_id)
    try:
        return json.loads((_home(store, playbook_id) / f'{check_id}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise KeyError(check_id) from None


def checks(store, playbook_id: str) -> list[dict]:
    """A playbook's checks, newest first."""
    home, found = _home(store, playbook_id), []
    for path in home.glob('*.json') if home.is_dir() else []:
        try:
            found.append(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            continue
    return sorted(found, key=lambda c: c['started_at'], reverse=True)


def running(store, playbook_id: str) -> dict | None:
    return next((c for c in checks(store, playbook_id) if c['status'] == 'running'), None)


def _replay_job(store, playbook: dict, case: dict, text: str, version: int | None, check_id: str) -> str:
    """Make the hidden job that does one case's work again under the instructions being tested."""
    used = {'id': playbook['id'], 'name': playbook['name'], 'version': version}
    job_id = store.create(playbook['id'], case['period'], case['goal'], text, used)['id']
    root, source = store.path(job_id), cases.folder(store, playbook['id'], case['id']) / 'files'
    try:
        for entry in case['files']:
            target = root / entry['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / entry['path'], target)
        skills = store.playbooks.skills.copy_to_job(playbook['id'], root / 'skills')
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        store.delete(job_id)
        raise
    # past_runs is set empty so none is chosen: the run this case came from would hand over the answer.
    store.update(job_id, files=case['files'], skills=skills, past_runs=[],
                 replay={'check': check_id, 'case': case['id'], 'playbook': playbook['id']})  # fmt: skip
    store.event(job_id, 'replay.created', f'Replaying a past correction: {case["lesson"][:120]}', type='job',
                check=check_id, case=case['id'], version=version)  # fmt: skip
    return job_id


def start(store, playbook_id: str, reason: str, text: str | None = None, based_on: int | None = None,
          extra: dict | None = None) -> dict | None:  # fmt: skip
    """Begin a check of a playbook's active cases and return its record with the jobs to queue; None when there is
    nothing to replay or a check is already running. text is a candidate not saved yet (based_on: the version it
    rewrites); without it the latest saved version is checked."""
    playbook = store.playbooks.get(playbook_id)
    with LOCK:
        active = cases.cases(store, playbook_id)
        if not active or running(store, playbook_id):
            return None
        _clear_jobs(store, playbook_id)
        check = {
            'id': uuid.uuid4().hex,
            'playbook_id': playbook_id,
            'reason': reason,
            'status': 'running',
            'started_at': now(),
            'version': playbook['version'] if text is None else None,
            'based_on': based_on,
            'candidate': text,
            'cases': [],
            **(extra or {}),
        }
        for case in active:
            entry = {'case': case['id'], 'lesson': case['lesson'], 'status': 'queued', 'job_id': None}
            try:
                entry['job_id'] = _replay_job(
                    store, playbook, case, playbook['text'] if text is None else text, check['version'], check['id']
                )
            except Exception as error:  # noqa: BLE001 - one case that cannot be set up does not stop the others
                entry.update(status='error', note=f'The test could not be set up ({type(error).__name__}).')
            check['cases'].append(entry)
        _settle(check)
        _write(store, check)
    store.playbooks.event(playbook_id, 'check.started', f'Checking {len(active)} past correction{"" if len(active) == 1 else "s"} ({reason})',
                          check=check['id'], version=check['version'], based_on=based_on, cases=len(active), reason=reason)  # fmt: skip
    _trim(store, playbook_id)
    return check


def _clear_jobs(store, playbook_id: str) -> None:
    """Remove the hidden jobs of earlier checks of this playbook; their results are in the check records."""
    for job in store.jobs():
        if (job.get('replay') or {}).get('playbook') == playbook_id and job['status'] in ENDED:
            shutil.rmtree(store.path(job['id']), ignore_errors=True)
            store.delete(job['id'])


def _trim(store, playbook_id: str) -> None:
    for old in checks(store, playbook_id)[KEPT_CHECKS:]:
        (_home(store, playbook_id) / f'{old["id"]}.json').unlink(missing_ok=True)


def score(case: dict, root: Path) -> dict:
    """Does a replayed run's output hold what the user accepted, for the record the user corrected?

    A cell belongs to a record: the correction was about one employee's pay, not about whatever sits in C2. Work
    done again may lay rows out in another order, so the record is found by its key (a cell of its row that no
    other row shared, kept with the case) wherever it now stands, and the corrected column is read there. Only
    when the case has no key is the same cell looked at. "passed" is None when the result cannot be told: no row
    carries the key any more, or several do. A line passes when the accepted line stands anywhere in the file."""
    spot, expected = case['spot'], case['expected']
    try:
        path = preview.job_file(root, spot['path'])
        if spot['kind'] == 'line':
            lines = cases.lines(path)
            got = lines[spot['line'] - 1] if spot['line'] <= len(lines) else None  # the file may be shorter now
            if got == expected:
                return {'passed': True, 'got': got}
            if expected in lines:
                return {'passed': True, 'got': expected, 'note': 'on another line'}
            return {'passed': False, 'got': got}
        row, column = feedback._row_column(spot['cell'])
        rows, key = cases.grid(path, spot), case.get('row_key')
        records = [n for n, cells in rows.items() if key and cells.get(key['column']) == key['value']]
        if len(records) == 1:
            got = rows[records[0]].get(column, '')
            moved = {'note': 'in another row'} if got == expected and records[0] != row else {}
            return {'passed': got == expected, 'got': got, **moved}
        if records:
            # The key was one record's own when the test was kept; now several rows carry it. Which of them is the
            # corrected record cannot be told, so this is not a pass and not a failure: it could not be checked.
            return {'passed': None, 'got': None, 'note': f'{len(records)} rows now carry the key of its record'}
        if key:
            # No row carries the record's key any more. Whatever now sits in the old cell belongs to someone else,
            # and matching it by chance must not count as a pass.
            return {'passed': None, 'got': None, 'note': 'its record was not found'}
        got = rows.get(row, {}).get(column, '')
        return {'passed': got == expected, 'got': got}
    except (ValueError, FileNotFoundError, OSError, KeyError) as error:
        problem = str(error).removeprefix('Comment 1: ') if isinstance(error, ValueError) else 'the file is not there'
        return {'passed': False, 'got': None, 'note': problem or 'the file could not be read'}
    except Exception:  # noqa: BLE001 - a file that does not open (a damaged workbook or Word file) is a wrong result;
        # were it raised, the check would never close and the next start of the app would stop on it
        return {'passed': False, 'got': None, 'note': 'the file could not be read'}


def _settle(check: dict) -> bool:
    """Close a check whose cases all have a result. Returns True when this call closed it."""
    if check['status'] != 'running' or any(c['status'] == 'queued' for c in check['cases']):
        return False
    total = len(check['cases'])
    passed = sum(c['status'] == 'passed' for c in check['cases'])
    check.update(status='done', finished_at=now(), total=total, passed_count=passed, passed=passed == total)
    return True


def finished(store, job: dict) -> dict | None:
    """Called when a hidden replay job has ended, however it ended. Scores its case and, when it was the last one,
    closes the check. Returns the check when this call closed it, else None."""
    replay = job['replay']
    playbook_id, check_id = replay['playbook'], replay['check']
    try:
        case = cases.read(store, playbook_id, replay['case'])
    except KeyError:
        case = None
    if job.get('cut_short') in ('service', 'day') and case:
        # The AI service or the day's token limit stopped it, not the instructions: that says nothing about them.
        why = {'service': 'the AI service stopped answering', 'day': "today's token limit was used up"}[
            job['cut_short']
        ]
        result = {'passed': None, 'got': None, 'note': why}
    elif job['status'] in ('completed', 'partial') and case:
        result = score(case, store.path(job['id']))
        if job['status'] == 'partial' and result['passed'] is not None:
            result['note'] = ' '.join(filter(None, [result.get('note'), 'the run ended partly done']))
    else:
        note = 'the test no longer exists' if not case else f'the run ended as {job["status"]}'
        result = {'passed': None, 'got': None, 'note': note}
    usage = job.get('usage') or {}
    result.update(at=now(), check=check_id, job_id=job['id'], tokens=usage.get('input_tokens', 0) + usage.get('output_tokens', 0))  # fmt: skip
    with LOCK:
        try:
            check = read(store, playbook_id, check_id)
        except KeyError:
            return None
        entry = next((c for c in check['cases'] if c['job_id'] == job['id']), None)
        if not entry or entry['status'] != 'queued':
            return None
        status = {True: 'passed', False: 'failed', None: 'error'}[result['passed']]
        entry.update(status=status, **{k: result[k] for k in ('got', 'note', 'tokens') if k in result})
        closed = _settle(check)
        _write(store, check)
    if case and check.get('candidate') is None:  # a candidate's results say nothing about the saved instructions
        cases.add_replay(store, playbook_id, case['id'], {**result, 'version': check['version']})
    words = {'passed': 'still right', 'failed': 'no longer right', 'error': 'could not be checked'}[status]
    store.playbooks.event(playbook_id, 'check.case', f'A past correction is {words}', 'INFO' if status == 'passed' else 'WARN',
                          check=check_id, case=replay['case'], status=status, got=result.get('got'), note=result.get('note'))  # fmt: skip
    if not closed:
        return None
    tokens = sum(c.get('tokens', 0) for c in check['cases'])
    store.playbooks.event(playbook_id, 'check.finished', f'{check["passed_count"]} of {check["total"]} past corrections still right',
                          'INFO' if check['passed'] else 'WARN', check=check_id, passed=check['passed_count'], total=check['total'],
                          version=check['version'], reason=check['reason'], tokens=tokens)  # fmt: skip
    return check


def adopt(store, check: dict, version: int) -> None:
    """The candidate this check replayed has been saved as `version`, so its results now speak for the saved
    instructions: they go onto the tests, and the check counts as the playbook's latest."""
    playbook_id = check['playbook_id']
    with LOCK:
        current = read(store, playbook_id, check['id'])
        current.update(version=version, candidate=None)
        _write(store, current)
    for entry in current['cases']:
        if entry['status'] not in ('passed', 'failed'):
            continue
        result = {'passed': entry['status'] == 'passed', 'got': entry.get('got'), 'note': entry.get('note'),
                  'at': current.get('finished_at'), 'check': current['id'], 'job_id': entry['job_id'],
                  'tokens': entry.get('tokens', 0), 'version': version}  # fmt: skip
        try:
            cases.add_replay(store, playbook_id, entry['case'], result)
        except KeyError:  # the test was retired or removed meanwhile
            continue


def abandon(store, job: dict) -> dict | None:
    """A replay job that the app's restart cut off is not continued: its case counts as not checked."""
    store.update(job['id'], status='cancelled')
    return finished(store, store.get(job['id']))


def _lost(store, playbook_id: str, check_id: str, job_id: str) -> dict | None:
    """A case whose hidden job no longer exists counts as not checked. Returns the check when that closed it."""
    with LOCK:
        check = read(store, playbook_id, check_id)
        entry = next((c for c in check['cases'] if c['job_id'] == job_id and c['status'] == 'queued'), None)
        if not entry:
            return None
        entry.update(status='error', note='the replay was lost when the app stopped')
        closed = _settle(check)
        _write(store, check)
    store.playbooks.event(playbook_id, 'check.case', 'A past correction could not be checked', 'WARN', check=check_id,
                          case=entry['case'], status='error', note=entry['note'])  # fmt: skip
    return check if closed else None


def reconcile(store) -> list[dict]:
    """Called when the app starts: no check may stay "running" with nothing running. Every case still waiting for
    a result gets one: scored if its job had ended, not checked if the job was cut off or is gone. Returns the
    checks this closed. Safe to call again: a case with a result is left alone."""
    closed = []
    for playbook in store.playbooks.list():
        for check in checks(store, playbook['id']):
            if check['status'] != 'running':
                continue
            for entry in check['cases']:
                if entry['status'] != 'queued':
                    continue
                try:
                    job = store.get(entry['job_id'])
                except (KeyError, TypeError):
                    done = _lost(store, playbook['id'], check['id'], entry['job_id'])
                else:
                    done = finished(store, job) if job['status'] in ENDED else abandon(store, job)
                if done:
                    closed.append(done)
            if not check['cases']:  # nothing was ever queued: close it as it stands
                with LOCK:
                    empty = read(store, playbook['id'], check['id'])
                    if _settle(empty):
                        _write(store, empty)
    return closed


def summary(store, playbook_id: str) -> dict:
    """What the playbook page shows: the active tests with their latest result, the score, and the latest check."""
    store.playbooks.get(playbook_id)
    active, found = cases.cases(store, playbook_id), checks(store, playbook_id)
    rows = []
    for case in active:
        last = case['replays'][-1] if case['replays'] else None
        rows.append({
            'id': case['id'], 'lesson': case['lesson'], 'created_at': case['created_at'], 'where': case['spot']['where'],
            'file': case['spot']['path'].removeprefix('outputs/'), 'expected': case['expected'], 'job_id': case['job_id'],
            'last': {k: last.get(k) for k in ('passed', 'got', 'note', 'at', 'version')} if last else None,
        })  # fmt: skip
    judged = [r for r in rows if r['last'] and r['last']['passed'] is not None]
    retired = [c for c in cases.cases(store, playbook_id, None) if c['status'] == 'retired']
    latest = next((c for c in found if c['status'] == 'done' and c.get('candidate') is None), None)
    return {
        'cases': rows,
        'limit': cases.MAX_CASES,
        'right': sum(r['last']['passed'] for r in judged),
        'checked': len(judged),
        'retired': [{'id': c['id'], 'lesson': c['lesson'], 'reason': c.get('retired_reason', ''), 'retired_at': c.get('retired_at')} for c in retired],
        'running': bool(running(store, playbook_id)),
        'last_check': {k: latest.get(k) for k in ('id', 'finished_at', 'version', 'passed_count', 'total', 'reason')} if latest else None,
    }  # fmt: skip
