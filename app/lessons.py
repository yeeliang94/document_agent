"""Lessons the agent takes from the user's comments, kept in the playbook for every later run.

While it acts on a round of comments, the agent may call its remember tool for a comment that shows a lasting rule
about how this user wants this kind of work done. The lesson is only noted then. When the run finishes with its
checks passing and the commented spot really changed, the lessons of each round are added to the playbook's
instructions as one new version (source "learned"), and each one gets a case to test it later (app/cases.py). A
run that ends any other way adds nothing. Every step is logged on the job and on the playbook, and the version can
be undone like any other.
"""

from __future__ import annotations

import json
import re

from . import cases, feedback

MAX_LESSON = 600  # characters; a lesson is one rule, not a report
MAX_PER_ROUND = 5


def _plain(text: str) -> str:
    return ' '.join(str(text or '').split())


def _playbook_id(job: dict) -> str:
    return (job.get('playbook') or {}).get('id') or ''


def note(store, job_id: str, comment: str, lesson: str) -> str:
    """The agent's remember tool. Returns words for the agent: what was noted, or why not and what to do."""
    job = store.get(job_id)
    open_rounds = {r['number'] for r in job.get('feedback') or [] if r['status'] == 'delivered'}
    if not open_rounds:
        return 'There is no open round of comments. Lessons are taken only from comments the user pinned to a file.'
    if not _playbook_id(job):
        return 'This job has no playbook to keep a lesson in.'
    label = re.fullmatch(r'\s*(\d+)\.(\d+)\s*', comment or '')
    records = {r['number']: r for r in feedback.rounds(store, job_id) if r['number'] in open_rounds}
    found = None
    if label:
        record = records.get(int(label[1]))
        found = next((c for c in (record or {}).get('comments', []) if c['n'] == int(label[2])), None)
    if not found:
        known = ', '.join(f'{r["number"]}.{c["n"]}' for r in records.values() for c in r['comments'])
        return f'There is no comment "{_plain(comment)}". The open comments are: {known}.'
    lesson = _plain(lesson)
    if not lesson:
        return 'The lesson is empty. Write the rule in a sentence or two.'
    if len(lesson) > MAX_LESSON:
        return f'The lesson is {len(lesson)} characters. Keep it to one rule, at most {MAX_LESSON} characters.'
    if found.get('remember'):
        return "The user already asked for this comment to be remembered. It is in the playbook in the user's words."
    key = {'round': int(label[1]), 'n': int(label[2])}
    reply = {}

    def keep(j):
        noted = [x for x in j.get('lessons') or [] if (x['round'], x['n']) != (key['round'], key['n'])]
        if sum(x['round'] == key['round'] and x['status'] == 'noted' for x in noted) >= MAX_PER_ROUND:
            reply['full'] = True
            return
        j['lessons'] = [*noted, {**key, 'lesson': lesson, 'status': 'noted'}]

    store.mutate(job_id, keep)
    if reply.get('full'):
        return f'{MAX_PER_ROUND} lessons are already noted for this round. Keep the ones that matter most.'
    store.event(job_id, 'lesson.noted', f'The agent noted a lesson: {lesson}', type='engine', lesson=lesson, **key)
    return (
        'Noted. It is added to the playbook when this run finishes with its checks passing and that spot changed. '
        'Calling remember again for the same comment replaces it.'
    )


def _drop(store, job_id: str, lesson: dict, reason: str) -> None:
    lesson.update(status='dropped', reason=reason)
    store.event(job_id, 'lesson.dropped', f'A lesson was not kept: {reason}', type='job', level='WARN',
                lesson=lesson['lesson'], round=lesson['round'], n=lesson['n'], reason=reason)  # fmt: skip


def settle(store, job_id: str, outcome: str) -> None:
    """Called once a job has settled, with how it really ended: "passed" (finished, checks passing, nothing from the
    user left unread), "later" (work the user asked for is still to come, so the question stays open) or "failed"
    (anything else: cancelled, failed, finished with problems). Decides the lessons noted for rounds of comments
    that are closed: keeps them, leaves them noted, or drops each with its reason. Never raises for a lesson: how
    a run ended does not depend on what could be remembered from it."""
    if outcome == 'later':
        return
    job = store.get(job_id)
    records = {r['number']: r for r in feedback.rounds(store, job_id) if r['status'] == 'done' and r.get('report')}
    closing = [x for x in job.get('lessons') or [] if x['status'] == 'noted' and x['round'] in records]
    if not closing:
        return
    playbook_id = _playbook_id(job)
    try:
        current = store.playbooks.get(playbook_id)['text'].casefold()
    except KeyError:
        current = None
    for lesson in closing:
        record = records[lesson['round']]
        comment = next(c for c in record['comments'] if c['n'] == lesson['n'])
        lesson['comment'] = comment
        # Each round is judged by its own final report, made when the run that acted on it finished.
        changed = {(s['round'], s['n']) for s in record['report'].get('spots') or [] if s['changed']}
        on_output = comment['path'].startswith('outputs/')
        if outcome != 'passed':
            _drop(store, job_id, lesson, 'the run did not finish with its checks passing')
        elif current is None:
            _drop(store, job_id, lesson, "the run's playbook no longer exists")
        elif on_output and (lesson['round'], lesson['n']) not in changed:
            _drop(store, job_id, lesson, 'the commented spot did not change')
        elif lesson['lesson'].casefold() in current:
            _drop(store, job_id, lesson, 'the instructions already say this')
    for number in sorted({x['round'] for x in closing}):
        kept = [x for x in closing if x['round'] == number and x['status'] == 'noted']
        if not kept:
            continue
        record = records[number]
        detail = {'job_id': job_id, 'round': number, 'round_id': record['id'], 'learned': True}
        text = '\n\n'.join(x['lesson'] for x in kept)
        try:
            earlier = next((v for v in store.playbooks.versions(playbook_id) if v['detail'] == detail), None)
            version = (
                earlier['version']
                if earlier
                else store.playbooks.save(playbook_id, text, 'learned', ' '.join(x['lesson'] for x in kept), append=True, announce=False, **detail)['version']
            )  # fmt: skip
        except (KeyError, ValueError) as error:
            for x in kept:
                _drop(store, job_id, x, _plain(error) if isinstance(error, ValueError) else "the run's playbook no longer exists")  # fmt: skip
            continue
        for x in kept:
            x.update(status='saved', version=version)
            store.event(job_id, 'lesson.saved', f'Learned for next time: {x["lesson"]}', type='job', lesson=x['lesson'],
                        round=number, n=x['n'], version=version, playbook=playbook_id)  # fmt: skip
            try:
                case = cases.capture(store, job, record, x['comment'], x['lesson'], version)
                x['case'] = case['id'] if case else None
            except Exception as error:  # noqa: BLE001 - a test that cannot be kept never undoes the lesson
                store.playbooks.event(playbook_id, 'case.skipped', f'No test kept ({type(error).__name__})', 'WARN', job_id=job_id, round=number, n=x['n'])  # fmt: skip
    settled = {(x['round'], x['n']): {k: v for k, v in x.items() if k != 'comment'} for x in closing}
    saved_versions = sorted({x['version'] for x in closing if x['status'] == 'saved'})

    def write(j):
        j['lessons'] = [settled.get((x['round'], x['n']), x) for x in j.get('lessons') or []]
        for entry in j.get('feedback') or []:
            saved = [x for x in settled.values() if x['round'] == entry['number'] and x['status'] == 'saved']
            if saved:
                entry['learned_version'] = saved[0]['version']
                entry['learned'] = [{'n': x['n'], 'lesson': x['lesson']} for x in saved]

    store.mutate(job_id, write)
    # Told only now, with each lesson's test in place: what follows a new version may replay the tests.
    if saved_versions and store.playbooks.on_change:
        store.playbooks.on_change(playbook_id, saved_versions[-1], 'learned')


def describe(job: dict) -> str:
    """For the export: every lesson of a job and what became of it."""
    return json.dumps(job.get('lessons') or [], ensure_ascii=False)
