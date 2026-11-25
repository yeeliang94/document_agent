"""A step OpenAI refuses ("Item with id ... not found") is sent as a note from then on, and the run carries on."""

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest
from agents.run_config import CallModelData, ModelInputData

from app.engine import agent, model_input, pictures

STEP = {
    'type': 'code_interpreter_call',
    'id': 'ci_0dea6008',
    'code': 'print(6 * 7)\nshow(page)',
    'container_id': 'cntr_1',
    'status': 'completed',
    'outputs': [{'type': 'logs', 'logs': '42'}, {'type': 'image', 'url': 'data:image/png;base64,AAAA'}],
}


def not_found(item='ci_0dea6008'):
    """The error the gateway passed on, worded as OpenAI words it."""
    message = (
        f"Error code: 404 - litellm.BadRequestError: OpenAIException - Item with id '{item}' not found. Items are "
        'not persisted when `store` is set to false. Try again with `store` set to true, or remove this item.'
    )
    response = httpx.Response(404, request=httpx.Request('POST', 'https://gateway.example/v1/responses'))
    return openai.NotFoundError(message, response=response, body=None)


def test_the_refused_step_is_read_from_the_error():
    assert model_input.refused_item(not_found()) == 'ci_0dea6008'
    other = httpx.Response(404, request=httpx.Request('POST', 'https://gateway.example/v1/containers'))
    assert model_input.refused_item(openai.NotFoundError('Container not found', response=other, body=None)) is None
    assert model_input.refused_item(ValueError("Item with id 'ci_1' not found")) is None


def test_a_refused_step_becomes_a_note_with_its_code_and_what_it_printed():
    before = [{'role': 'user', 'content': 'go'}, {'type': 'reasoning', 'id': 'rs_1'}, STEP, {'type': 'message'}]

    sent = model_input.without_refused(before, {'ci_0dea6008'})

    assert sent[:2] == before[:2] and sent[3] == before[3]
    note = sent[2]
    assert note['role'] == 'user' and 'print(6 * 7)' in note['content'] and '42' in note['content']
    assert 'base64' not in note['content']
    assert model_input.without_refused(before, set()) is before


def test_the_filter_sends_refused_steps_as_notes_and_shrinks_pictures():
    big = 'data:image/png;base64,' + 'A' * (pictures.MAX_URL + 10)
    pinned = {'role': 'user', 'content': [{'type': 'input_image', 'detail': 'high', 'image_url': big}]}
    data = ModelInputData(input=[STEP, pinned], instructions='Work.')
    job = SimpleNamespace(refused={'ci_0dea6008'})

    sent = asyncio.run(model_input.model_input(CallModelData(model_data=data, agent=None, context=job)))

    assert sent.instructions == 'Work.'
    assert sent.input[0]['role'] == 'user' and '42' in sent.input[0]['content']
    assert sent.input[1]['content'] == [{'type': 'input_text', 'text': pictures.NOTE}]


class FakeRun:
    """A streamed run that fails with the given error, or finishes."""

    def __init__(self, error=None):
        self.error, self.new_items, self.current_turn, self.final_output = error, [], 1, None if error else 'done'
        usage = SimpleNamespace(
            requests=1, input_tokens=1, output_tokens=1, input_tokens_details=None, output_tokens_details=None
        )
        self.context_wrapper = SimpleNamespace(usage=usage)

    async def stream_events(self):
        if self.error:
            raise self.error
        return
        yield


def drive(monkeypatch, runs, prompt=None):
    """Run the real loop against fake runs, and return what each call would have sent through the run's own filter,
    with the refused ids as they stood at that call."""
    calls = []

    def run_streamed(agent_, prompt, context=None, max_turns=0, run_config=None, hooks=None):
        calls.append((run_config, prompt, SimpleNamespace(refused=set(context.refused))))
        return runs.pop(0)

    monkeypatch.setattr(agent.Runner, 'run_streamed', run_streamed)
    events = []
    store = SimpleNamespace(event=lambda name, **kw: events.append((name, kw)))
    job = agent.Job(settings=None, store=store, spec=None, main=None, zip_path=None, files=[], scout_model='', effort='low',
                    max_scouts=0, answerer=None)  # fmt: skip
    control = SimpleNamespace(stop_requested=lambda: None, new_messages=lambda: [])
    conversation = prompt or [{'role': 'user', 'content': 'go'}, STEP, {'role': 'user', 'content': 'carry on'}]
    result = asyncio.run(agent._drive(None, conversation, job, store, lambda *_: None, control))
    sent = [
        asyncio.run(config.call_model_input_filter(CallModelData(ModelInputData(items, None), None, context))).input
        for config, items, context in calls
    ]
    return result, job, events, sent


def test_the_run_carries_on_sending_the_refused_step_as_a_note(monkeypatch):
    result, job, events, sent = drive(monkeypatch, [FakeRun(not_found()), FakeRun()])

    assert result.final_output == 'done' and len(sent) == 2
    assert sent[0][1] == STEP  # the first request carried the step as it was
    assert sent[1][1]['role'] == 'user' and '42' in sent[1][1]['content']  # the retry carries the note in its place
    assert sent[1][0] == sent[0][0] and sent[1][2] == sent[0][2]
    assert job.refused == {'ci_0dea6008'}
    assert events[0][0] == 'model.item_refused' and events[0][1]['item'] == 'ci_0dea6008'


def test_a_step_refused_again_still_ends_the_run(monkeypatch):
    with pytest.raises(openai.NotFoundError):
        drive(monkeypatch, [FakeRun(not_found()), FakeRun(not_found())])


def test_a_refusal_of_anything_but_a_sandbox_step_ends_the_run_as_before(monkeypatch):
    call = {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1', 'name': 'save_outputs', 'arguments': '{}'}
    output = {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'passed'}
    with pytest.raises(openai.NotFoundError):
        drive(
            monkeypatch,
            [FakeRun(not_found('fc_1')), FakeRun()],
            prompt=[{'role': 'user', 'content': 'go'}, call, output],
        )
