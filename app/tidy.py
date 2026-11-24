"""The agent keeps a playbook's instructions tidy by itself, and only when the kept tests still pass.

Lessons are added one at a time at the end of the instructions. When enough have piled up, the rewrite step
(app/tuning.py) proposes a consolidated version. Nothing is saved yet: the proposal is replayed against the
playbook's tests (app/replay.py) as a candidate. If every test still passes and nobody changed the instructions in
the meantime, the tidy becomes a new version (source "tidied"), which can be undone like any other. Otherwise it
is dropped and the reason is logged. Without tests to replay there is no way to know a tidy is safe, so none is
made.
"""

from __future__ import annotations

import json

from . import cases, replay, tuning
from .playbooks import StaleVersion

TIDY_AFTER = 5  # lessons added since the last tidy (or the last attempt at one)
TIDY_CHARS = 20_000  # or instructions this long, with at least one lesson since
LESSONS = ('learned', 'remembered')
ASK = (
    'Tidy these instructions. Several rules were added one at a time at the end, each as its own paragraph. Put each '
    'where it belongs, merge rules that say the same thing, and keep the meaning of every rule. Add nothing new.'
)


def _attempts(store, playbook_id: str) -> list[dict]:
    folder, found = tuning._folder(store, playbook_id), []
    for path in folder.glob('*.json') if folder.is_dir() else []:
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if record.get('tidy'):
            found.append(record)
    return found


def due(store, playbook_id: str) -> bool:
    """Is it time to tidy? Counted from the last tidy or the last attempt, so a tidy that was dropped is not tried
    again with every new lesson."""
    if store.settings()['replay_tests'] == 'never' or not cases.cases(store, playbook_id):
        return False
    if replay.running(store, playbook_id):
        return False
    playbook, versions = store.playbooks.get(playbook_id), store.playbooks.versions(playbook_id)
    since = max(
        [v['version'] for v in versions if v['source'] == 'tidied']
        + [r['based_on'] for r in _attempts(store, playbook_id)]
        + [0]
    )
    lessons = sum(v['source'] in LESSONS and v['version'] > since for v in versions)
    return lessons >= TIDY_AFTER or (lessons >= 1 and len(playbook['text']) > TIDY_CHARS)


async def begin(store, endpoint, playbook_id: str) -> dict | None:
    """Ask for a tidy of the latest instructions. Returns what to replay: {'proposal', 'text', 'based_on'}; None
    when there was nothing to tidy or the proposal could not be made (logged either way)."""
    books = store.playbooks
    version = books.get(playbook_id)['version']
    books.event(playbook_id, 'tidy.started', 'Enough lessons have piled up: asking for a tidy of the instructions', version=version)  # fmt: skip
    try:
        record = await tuning.propose(store, endpoint, playbook_id, ASK, version)
    except (tuning.TuningFailed, StaleVersion, KeyError) as error:
        books.event(playbook_id, 'tidy.failed', 'A tidy could not be proposed; the instructions stay as they are', 'WARN',
                    reason=str(error)[:300], version=version)  # fmt: skip
        return None
    record['tidy'] = True
    tuning._write(store, record)
    if not record['proposal']['changed']:
        tuning.discard(store, playbook_id, record['id'])
        books.event(playbook_id, 'tidy.nothing', 'The instructions needed no tidying', proposal_id=record['id'], version=version)  # fmt: skip
        return None
    return {'proposal': record['id'], 'text': record['proposal']['text'], 'based_on': version}


def conclude(store, check: dict) -> dict | None:
    """The replay of a tidy has finished. Save the tidy when every test passed and the instructions are still the
    version it rewrote; drop it otherwise. Returns the saved playbook, or None."""
    books, playbook_id, proposal_id = store.playbooks, check['playbook_id'], check['tidy']
    ids = {'proposal_id': proposal_id, 'check': check['id'], 'version': check['based_on']}
    failed = [c['lesson'][:120] for c in check['cases'] if c['status'] == 'failed']
    unchecked = sum(c['status'] not in ('passed', 'failed') for c in check['cases'])
    if not check['passed']:
        tuning.discard(store, playbook_id, proposal_id)
        why = (f'{len(failed)} of {check["total"]} past corrections were no longer right with it' if failed
               else f'{unchecked} of {check["total"]} past corrections could not be checked')  # fmt: skip
        books.event(playbook_id, 'tidy.dropped', f'The tidy was dropped: {why}', 'WARN', failed=failed, unchecked=unchecked, **ids)  # fmt: skip
        return None
    try:
        record = tuning.read(store, playbook_id, proposal_id)
        saved = books.save(playbook_id, check['candidate'], 'tidied', record['proposal']['summary'],
                           based_on=check['based_on'], proposal_id=proposal_id, check=check['id'])  # fmt: skip
    except StaleVersion as stale:
        tuning.discard(store, playbook_id, proposal_id)
        books.event(playbook_id, 'tidy.outdated', f'The tidy was dropped: the instructions changed while it was being checked (now version {stale.current})',
                    'WARN', **ids)  # fmt: skip
        return None
    except (KeyError, ValueError) as error:
        books.event(playbook_id, 'tidy.failed', 'The tidy passed its check but could not be saved', 'WARN', reason=str(error)[:300], **ids)  # fmt: skip
        return None
    tuning.read(store, playbook_id, proposal_id)  # marks the proposal accepted, from the version just written
    replay.adopt(store, check, saved['version'])  # the tests it passed were passed by what is now saved
    books.event(playbook_id, 'tidy.applied', f'Tidied the instructions as version {saved["version"]}; all {check["total"]} past corrections are still right',
                **{**ids, 'version': saved['version']})  # fmt: skip
    return saved
