"""The agent's plan: a short checklist it writes with the update_plan tool and ticks off as it goes.

The plan is kept with the run (plan.json in the run folder, and on the job by the app), not only in the conversation,
so it is still there after the older conversation is condensed and when a stopped run is picked up in a fresh sandbox.
The person sees it at the top of the run; when the agent changes it, the run says what changed and why.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

STATUSES = ('pending', 'in_progress', 'done')
MAX_STEPS = 10
STEP_CHARS = 120
SIMILAR = 0.5  # how alike a step's old and new wording must be to count as the same step reworded


def clean(steps: list[dict]) -> tuple[list[dict], str]:
    """The plan as kept, or a sentence saying why it cannot be kept (then the list is empty)."""
    kept = []
    for number, step in enumerate(steps, 1):
        text = ' '.join(str(step.get('step') or '').split())
        status = step.get('status') if step.get('status') in STATUSES else 'pending'
        if not text:
            return [], f'Step {number} has no words. Give every step a few plain words.'
        if len(text) > STEP_CHARS:
            return [], (
                f'Step {number} is {len(text)} characters long. Keep each step under {STEP_CHARS}: a short line the '
                'person reads at a glance, with the detail left to your notes.'
            )
        kept.append({'step': text, 'status': status})
    if not kept:
        return [], 'The plan has no steps. Send the whole plan, three to eight steps.'
    if len(kept) > MAX_STEPS:
        return [], f'The plan has {len(kept)} steps. Keep it to {MAX_STEPS} at most by joining related work.'
    seen = set()
    for step in kept:
        key = step['step'].lower()
        if key in seen:
            return [], f'"{step["step"]}" is in the plan twice. Name each step once.'
        seen.add(key)
    return kept, ''


def changes(old: list[dict], new: list[dict]) -> dict:
    """What changed in the steps themselves (not their ticks): steps added, steps reworded (with what they said
    before) and steps dropped."""
    before, after = [s['step'] for s in old], [s['step'] for s in new]
    added, changed, removed = [], [], []
    matcher = SequenceMatcher(None, [s.lower() for s in before], [s.lower() for s in after], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == 'insert':
            added += after[j1:j2]
        elif op == 'delete':
            removed += before[i1:i2]
        elif op == 'replace':
            olds, news = before[i1:i2], after[j1:j2]
            for k in range(max(len(olds), len(news))):
                was = olds[k] if k < len(olds) else None
                now = news[k] if k < len(news) else None
                if was and now and SequenceMatcher(None, was.lower(), now.lower()).ratio() >= SIMILAR:
                    changed.append({'step': now, 'was': was})
                    continue
                if now:
                    added.append(now)
                if was:
                    removed.append(was)
    return {'added': added, 'changed': changed, 'removed': removed}


def counted(steps: list[dict]) -> str:
    done = sum(s['status'] == 'done' for s in steps)
    return f'all {len(steps)} done' if done == len(steps) else f'{done} of {len(steps)} done'


def as_text(steps: list[dict]) -> str:
    """The plan as the agent reads it back: one line per step, with its status."""
    marks = {'done': '[done]', 'in_progress': '[doing now]', 'pending': '[to do]'}
    return '\n'.join(f'{n}. {marks[s["status"]]} {s["step"]}' for n, s in enumerate(steps, 1))


def keep(job, steps: list[dict], why: str = '') -> str:
    """Keep the plan the agent sent, record it with the run, and say back what was kept."""
    new, problem = clean(steps)
    if problem:
        return f'The plan was not kept. {problem} Send the whole plan again.'
    old = list(getattr(job, 'plan', None) or [])
    why = ' '.join(str(why or '').split())[:300]
    if new == old:
        return f'No change: the plan is as it was ({counted(new)}).'
    moved = changes(old, new) if old else {'added': [], 'changed': [], 'removed': []}
    job.plan = new
    job.store.event('plan.updated', steps=new, why=why, first=not old, **moved)
    job.store.write_json('plan.json', {'steps': new, 'why': why})
    said = f'Plan kept ({counted(new)}). The person sees it at the top of the run.'
    if sum(s['status'] == 'in_progress' for s in new) > 1:
        said += ' More than one step is marked in_progress; mark only the step you are on.'
    if any(moved.values()) and not why:
        said += ' You changed the steps without saying why; when you change them, say why in one sentence in "why".'
    return said


def reminder(job) -> str:
    """The plan as it stands, for the agent after its history was condensed; empty when it has none."""
    steps = getattr(job, 'plan', None) or []
    return f' Your plan as it stands (keep it up to date with update_plan):\n{as_text(steps)}' if steps else ''


# --- Summaries of the agent's thinking -------------------------------------------------------------------------

# The Settings value, and what OpenAI is asked for. Short and detailed both ask for a summary; the app shows the first
# paragraph of each for "short", since not every model offers OpenAI's own concise summary.
SUMMARY = {'off': None, 'short': 'auto', 'detailed': 'detailed'}
HEADING = re.compile(r'^\s*\*\*(.+?)\*\*\s*')
TITLE_CHARS = 90


def thought_parts(item) -> list[dict]:
    """A reasoning item's summary as parts the screen shows: each a short heading and the text under it. OpenAI
    usually opens each part with a bold heading; a part without one is headed by its first sentence."""
    parts = []
    for entry in getattr(item, 'summary', None) or ():
        text = str(getattr(entry, 'text', '') if not isinstance(entry, dict) else entry.get('text', '')).strip()
        if not text:
            continue
        found = HEADING.match(text)
        if found:
            title, body = found.group(1).strip(), text[found.end() :].strip()
        else:
            first = re.split(r'(?<=[.!?])\s', text, maxsplit=1)[0]
            title, body = first, text
        if len(title) > TITLE_CHARS:
            title = title[: TITLE_CHARS - 1].rstrip() + '…'
        parts.append({'title': title, 'text': body})
    return parts


def summary_refused(error: Exception) -> bool:
    """True when the AI service refused the request because of the thinking-summary setting, so the run can carry on
    without it. A gateway names the parameter it does not take; any other refusal is not this."""
    return getattr(error, 'status_code', None) == 400 and 'summary' in str(error).lower()
