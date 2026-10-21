"""The agent's durable working plan and notes; completed operations, not prose, establish results."""

from typing import Literal

from pydantic import BaseModel, Field

from .state import identity


class WorkItem(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=1000)
    status: Literal['pending', 'active', 'blocked', 'done'] = 'pending'
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    blocker: str = Field(default='', max_length=1000)


class WorkPlan(BaseModel):
    objective: str = Field(min_length=1, max_length=3000)
    focus: str = Field(default='', max_length=1000)
    items: list[WorkItem] = Field(default_factory=list, max_length=30)
    next_actions: list[str] = Field(default_factory=list, max_length=10)
    notes: str = Field(default='', max_length=4000)


def save_plan(state, plan):
    """Save the agent's own working notes. Never refused: the final checks, not the plan, decide completion."""
    seen, items = set(), []
    # A repeated ID keeps its latest entry, so a sloppy rewrite still saves.
    for item in reversed(plan.items):
        if item.id not in seen:
            seen.add(item.id)
            items.append(item)
    plan.items = items[::-1]
    unknown = []
    for item in plan.items:
        unknown.extend(key for key in item.evidence if not state.has_reference(key))
        unknown.extend(key for key in item.dependencies if key not in seen)
        item.dependencies = [key for key in item.dependencies if key in seen and key != item.id]
    values = plan.model_dump()
    old = state.get('plan', 'current', {})
    digest = identity(values, state.revision('instruction_revision'))
    note = {'unrecognized_references': sorted(set(unknown))} if unknown else {}
    if old.get('hash') == digest:
        return old | note
    if old:
        state.put('plan_history', old['hash'], old)
    result = values | {'hash': digest, 'instruction_revision': state.revision('instruction_revision')}
    state.put('plan', 'current', result)
    return result | note


def record_outcome(state, actor, tool, arguments, result, events):
    """Compact attempted approaches per work item; claims never set a work item to done."""
    from .supervisor import normalized

    plan = state.get('plan', 'current', {})
    focus = next((item['id'] for item in plan.get('items', []) if item['status'] == 'active'), 'unassigned')
    key = identity(actor, focus, tool, normalized(arguments))
    old = state.get('approach', key, {'count': 0})
    state.put(
        'approach',
        key,
        {
            'actor': actor,
            'work_item': focus,
            'tool': tool,
            'arguments': str(normalized(arguments))[:1200],
            'count': old['count'] + 1,
            'outcome': (str(result.get('error', 'completed'))[:500] if isinstance(result, dict) else 'completed'),
            'error_class': result.get('error_class') if isinstance(result, dict) else None,
            'progress': events[:5],
        },
    )
    if plan and events:
        pointers = [
            result[key]
            for key in ('id', 'path', 'operation_id', 'evidence_id')
            if isinstance(result, dict) and key in result
        ]
        state.put(
            'plan', 'current', plan | {'last_result': {'tool': tool, 'references': pointers, 'events': events[:5]}}
        )
