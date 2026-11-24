"""What each model call of a run sends: the conversation, made fit to send.

OpenAI keeps no copy of a run's steps (store=False), so every call sends the conversation again, and OpenAI accepts a
step it made only as it made it. Two things are done before each call:
- a sandbox step OpenAI has refused ("Item with id ... not found") is replaced by a note to the agent that keeps its code and
  what it printed. The agent loses only that step's pictures, and the run carries on (removing the step, or putting a
  note in its place, are both accepted: tried against the live API);
- every picture is made small enough to send (pictures.py).
"""

from __future__ import annotations

import re

from agents.run_config import CallModelData, ModelInputData

from .pictures import fit_input

MISSING = re.compile(r"Item with id '([^']+)' not found")


def refused_item(error: Exception) -> str | None:
    """The id of the step OpenAI says it cannot find, when that is why it refused the request."""
    if getattr(error, 'status_code', None) != 404:
        return None
    found = MISSING.search(str(error))
    return found.group(1) if found else None


def is_sandbox_step(items, item_id: str) -> bool:
    """Whether the conversation holds this id as a sandbox step: the only kind a note can stand in for. Anything else
    (a tool call with its result, a reply, reasoning) has no faithful note, so a refusal of it still ends the run."""
    return isinstance(items, list) and any(_is_step(i) and i.get('id') == item_id for i in items)


def _is_step(item) -> bool:
    return isinstance(item, dict) and item.get('type') == 'code_interpreter_call'


def note_for(item: dict) -> dict:
    """A refused step as a note the agent reads: what the code was and what it printed."""
    printed = '\n'.join(o.get('logs', '') for o in item.get('outputs') or () if o.get('type') == 'logs').strip()
    code = (item.get('code') or '').strip()
    text = (
        'One of your earlier sandbox steps could not be sent back to you, so it is shown here as a note. '
        'Any picture it showed is left out; show it again if you still need it.'
    )
    if code:
        text += f'\nIts code:\n{code}'
    text += f'\nWhat it printed:\n{printed or "(nothing)"}'
    return {'role': 'user', 'content': text}


def without_refused(items: list, refused) -> list:
    if not refused or not any(_is_step(i) and i.get('id') in refused for i in items):
        return items
    return [note_for(i) if _is_step(i) and i.get('id') in refused else i for i in items]


async def model_input(data: CallModelData) -> ModelInputData:
    """The run's call_model_input_filter. The job (the run's context) holds the ids OpenAI refused."""
    refused = getattr(data.context, 'refused', None)
    model = data.model_data
    items = without_refused(model.input, refused)
    if items is not model.input:
        model = ModelInputData(input=items, instructions=model.instructions)
    return await fit_input(CallModelData(model_data=model, agent=data.agent, context=data.context))
