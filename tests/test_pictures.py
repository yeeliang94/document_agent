"""Pictures in a run's conversation stay small enough for the model's API to accept."""

import asyncio
import base64
import copy
import io
import random
from types import SimpleNamespace

import pytest
from agents.run_config import CallModelData, ModelInputData
from PIL import Image

from app.engine import agent, model_input, pictures


def png_url(width, height, mode='RGB'):
    """A PNG of noise as a data: URL. Noise does not compress, so the picture is as large as its pixels."""
    pixels = random.Random(7).randbytes(width * height * len(mode))
    return url_of(Image.frombytes(mode, (width, height), pixels))


def url_of(image):
    out = io.BytesIO()
    image.save(out, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode()


def opened(url):
    return Image.open(io.BytesIO(base64.b64decode(url.partition(',')[2])))


def sandbox_call(*urls):
    """A code interpreter call as the run carries it from turn to turn, with the pictures its code showed."""
    outputs = [{'type': 'logs', 'logs': 'shown'}, *({'type': 'image', 'url': url} for url in urls)]
    return {'type': 'code_interpreter_call', 'id': 'ci_1', 'code': 'show()', 'outputs': outputs}


def pinned(url):
    return {
        'role': 'user',
        'content': [
            {'type': 'input_text', 'text': 'Picture round-01-picture-01.png:'},
            {'type': 'input_image', 'detail': 'high', 'image_url': url},
        ],
    }


def test_a_large_picture_shown_in_the_sandbox_is_left_out_with_a_note_and_the_call_kept_as_made():
    big, small = png_url(900, 600), png_url(40, 30)
    assert len(big) > 1_048_576  # what the gateway refused: an image URL over 1,048,576 characters
    conversation = [{'role': 'user', 'content': 'Fill the template.'}, sandbox_call(big, small)]
    before = copy.deepcopy(conversation)

    sent = pictures.fit(conversation)

    # OpenAI keeps no copy of the call, and refuses it back with a changed picture ("Item ... not found"), so the
    # picture is not shrunk: it is left out, which it accepts, and the agent is told so in its place.
    call = sent[1]
    assert call['outputs'] == [
        {'type': 'logs', 'logs': 'shown'},
        {'type': 'logs', 'logs': pictures.SHOWN_NOTE},
        {'type': 'image', 'url': small},
    ]
    assert {k: v for k, v in call.items() if k != 'outputs'} == {k: v for k, v in before[1].items() if k != 'outputs'}
    assert conversation == before  # the run's own record keeps the picture as it was


def test_a_large_pinned_page_is_scaled_down():
    sent = pictures.fit([pinned(png_url(2400, 1600))])[0]['content'][1]['image_url']
    assert len(sent) <= pictures.MAX_URL and opened(sent).size[0] < 2400


def test_a_pinned_picture_is_made_to_fit_with_its_see_through_parts_on_white():
    image = Image.frombytes('RGB', (900, 600), random.Random(7).randbytes(900 * 600 * 3)).convert('RGBA')
    image.paste((0, 0, 0, 0), (0, 0, 100, 100))  # a see-through corner
    message = pinned(url_of(image))

    (sent,) = pictures.fit([message])

    assert sent['content'][0] == message['content'][0]
    assert len(sent['content'][1]['image_url']) <= pictures.MAX_URL
    corner = opened(sent['content'][1]['image_url']).convert('RGB').getpixel((2, 2))
    assert min(corner) > 230  # white, not the black a see-through pixel would turn into


def test_a_long_thin_picture_is_kept():
    strip = png_url(12000, 80)
    assert len(strip) > pictures.MAX_URL
    (sent,) = pictures.fit([pinned(strip)])
    assert len(sent['content'][1]['image_url']) <= pictures.MAX_URL


def test_a_picture_that_cannot_be_sent_is_replaced_by_a_note_the_agent_reads():
    broken = 'data:image/png;base64,' + 'A' * (pictures.MAX_URL + 10)
    (sent,) = pictures.fit([sandbox_call(broken)])
    assert sent['outputs'][1] == {'type': 'logs', 'logs': pictures.SHOWN_NOTE}
    (sent,) = pictures.fit([pinned(broken)])
    assert sent['content'][1] == {'type': 'input_text', 'text': pictures.NOTE}
    nested = [{'type': 'something_else', 'output': {'image_url': broken}}, 'kept']
    assert pictures.fit(nested) == ['kept']  # an unknown holder is left out whole, never sent half empty


def test_a_conversation_without_large_pictures_is_sent_as_it_is():
    conversation = [{'role': 'user', 'content': 'Fill the template.'}, sandbox_call(png_url(40, 30))]
    assert pictures.fit(conversation) is conversation


def test_the_filter_shrinks_pictures_and_passes_the_rest_on():
    assert agent.RUN_CONFIG.call_model_input_filter is model_input.model_input
    data = ModelInputData(input=[pinned(png_url(2400, 1600))], instructions='Work.')
    sent = asyncio.run(pictures.fit_input(CallModelData(model_data=data, agent=None, context=None)))
    assert sent.instructions == 'Work.' and len(sent.input[0]['content'][1]['image_url']) <= pictures.MAX_URL
    small = ModelInputData(input=[sandbox_call(png_url(40, 30))], instructions='Work.')
    assert asyncio.run(pictures.fit_input(CallModelData(model_data=small, agent=None, context=None))) is small


def test_the_main_run_goes_through_the_filter(monkeypatch):
    seen = {}

    def run_streamed(agent_, prompt, context=None, max_turns=0, run_config=None, hooks=None):
        seen['run_config'] = run_config
        raise RuntimeError('stop here')

    monkeypatch.setattr(agent.Runner, 'run_streamed', run_streamed)
    with pytest.raises(RuntimeError, match='stop here'):
        asyncio.run(agent._drive(None, 'go', SimpleNamespace(max_turns=5, turns_used=0), None, None, None))
    assert seen['run_config'] is agent.RUN_CONFIG
