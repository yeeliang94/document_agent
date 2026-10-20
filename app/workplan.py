"""Bounded durable work hypotheses; completed operations, not prose, establish results."""

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
    focus: str = Field(min_length=1, max_length=1000)
    items: list[WorkItem] = Field(max_length=20)
    next_actions: list[str] = Field(min_length=1, max_length=5)


def save_plan(state, plan):
    known = {item.id for item in plan.items}
    if len(known) != len(plan.items):
        raise ValueError('Work item IDs must be unique')
    for item in plan.items:
        if any(key not in known for key in item.dependencies) or item.id in item.dependencies:
            raise ValueError('Dependencies must reference other work items')
    # The plan is a working hypothesis: drop references that do not exist rather than refuse the whole plan.
    ignored = []
    for item in plan.items:
        ignored.extend(key for key in item.evidence if not state.has_reference(key))
        item.evidence = [key for key in item.evidence if state.has_reference(key)]
        # Claiming completion still needs a real saved result behind it.
        if item.status == 'done' and not item.evidence:
            raise ValueError(
                f'Work item {item.id} is marked done but cites no saved result. Cite an id returned by a tool, '
                'such as validate_job, extract_region, save_output_requirements or an evidence_id.'
            )
    visiting, visited = set(), set()
    dependencies = {item.id: item.dependencies for item in plan.items}

    def visit(key):
        if key in visiting:
            raise ValueError('Work item dependencies contain a cycle')
        if key in visited:
            return
        visiting.add(key)
        for dependency in dependencies[key]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in known:
        visit(key)
    values = plan.model_dump()
    old = state.get('plan', 'current', {})
    digest = identity(values, state.revision('instruction_revision'))
    note = {'ignored_references': sorted(set(ignored))} if ignored else {}
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
