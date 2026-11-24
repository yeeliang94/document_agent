"""Pictures small enough to send to the model.

A picture travels inside the request as text (a data: URL), and every later turn of a run sends the whole conversation
again. OpenAI, and gateways in front of it, refuse an image URL longer than 1,048,576 characters, so one large picture
(a chart or a page the sandbox showed, a page the user pinned a comment to) would fail every request after it and stop
the run for good. Before each model call, a picture over the limit is dealt with by where it came from:
- One the sandbox's code showed is replaced by a note. The model saw it when the code ran, and OpenAI keeps no copy of
  the call (store=False): it accepts the call back only as it made it, or with a picture left out. A changed picture
  makes it look the call up by its id, which fails with "Item ... not found" (tried against the live API).
- One sent with a message, such as a page the user pinned a comment to, is the app's own and is made smaller until it
  fits. One that cannot be made to fit is replaced by a note too.
Either way the agent is told, in the same place, that a picture is missing.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import io

from agents.run_config import CallModelData, ModelInputData
from PIL import Image

MAX_URL = 1_000_000  # characters; the limit is 1,048,576, and this leaves room for the URL's prefix
SMALLEST_SIDE = 64  # a picture whose longer side would be shorter than this shows nothing worth reading
SHOWN_NOTE = 'The picture this code showed is left out from here on: it is too large to send again. You saw it when the code ran. To look at it again, show a smaller or cropped version.'  # fmt: skip
NOTE = 'A picture here was too large to send and could not be made small enough to look at.'
# What a picture over the limit becomes, by the kind of item that held it: the same place in the conversation, in a
# form the model reads. Anything else holding one is left out whole.
NOTE_AS = {
    'image': {'type': 'logs', 'logs': SHOWN_NOTE},  # shown by code in the sandbox; never changed, only left out
    'input_image': {'type': 'input_text', 'text': NOTE},  # sent with a message, such as a pinned picture
}
_GONE = object()  # what fit() returns for an item that is left out


async def fit_input(data: CallModelData) -> ModelInputData:
    """The run's call_model_input_filter. Shrinking a picture takes up to a second or two, so it runs off the event
    loop the web server shares; a conversation with no large picture is passed on as it is, at once."""
    model = data.model_data
    if not _holds_big(model.input):
        return model
    return ModelInputData(input=await asyncio.to_thread(fit, model.input), instructions=model.instructions)


def fit(value):
    """The same items, with each picture over MAX_URL replaced by a note or, in a message, made smaller. Unchanged parts are the same
    objects, and the run's own record of the conversation is not touched."""
    if isinstance(value, list):
        if not _holds_big(value):
            return value
        return [item for item in map(fit, value) if item is not _GONE]
    if isinstance(value, dict) and _holds_big(value):
        out = {}
        for key, item in value.items():
            if _big(item) and key in ('url', 'image_url'):
                item = smaller(item) if value.get('type') != 'image' else None
                if item is None:
                    return NOTE_AS.get(value.get('type'), _GONE)
            else:
                item = fit(item)
                if item is _GONE:
                    return _GONE
            out[key] = item
        return out
    return value


def _big(value) -> bool:
    return isinstance(value, str) and len(value) > MAX_URL and value.startswith('data:image/')


def _holds_big(value) -> bool:
    if _big(value):
        return True
    if isinstance(value, dict):
        return any(map(_holds_big, value.values()))
    if isinstance(value, list):
        return any(map(_holds_big, value))
    return False


@functools.lru_cache(maxsize=16)
def smaller(url: str) -> str | None:
    """The picture as PNG while that fits (sharper text) and JPEG after, first at its own size and then scaled down,
    or None if it cannot fit or will not open. The same picture is sent again each turn; keeping the answer saves the
    work and gives the same bytes every time, so the prompt cache still matches."""
    try:
        image = Image.open(io.BytesIO(base64.b64decode(url.partition(',')[2])))
        image.load()
        if image.mode in ('RGBA', 'LA', 'PA') or 'transparency' in image.info:
            image = _on_white(image)
        elif image.mode != 'RGB':
            image = image.convert('RGB')
        scale = 1.0  # a picture may be large only for how it was saved, so saving it again comes first
        while max(image.width, image.height) * scale >= SMALLEST_SIDE:
            size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            resized = image if scale == 1.0 else image.resize(size, Image.LANCZOS)
            shortest = None
            for kind in ('png', 'jpeg'):
                out = io.BytesIO()
                try:
                    resized.save(out, format=kind.upper(), **({'quality': 85} if kind == 'jpeg' else {}))
                except (OSError, ValueError):  # JPEG takes no side over 65,500 pixels; a smaller size may save
                    continue
                fitted = f'data:image/{kind};base64,' + base64.b64encode(out.getvalue()).decode()
                if len(fitted) <= MAX_URL:
                    return fitted
                shortest = min(shortest or len(fitted), len(fitted))
            # The size grows with the area, so the sides shrink by the square root of how far over it still is.
            scale *= min(0.8, (MAX_URL / shortest) ** 0.5) if shortest else 0.5
    except Exception:  # noqa: BLE001 - a picture that will not open cannot be sent either
        return None
    return None


def _on_white(image: Image.Image) -> Image.Image:
    """A see-through picture over a white page, as it would be looked at; JPEG has no see-through parts."""
    page = Image.new('RGB', image.size, 'white')
    page.paste(image, mask=image.convert('RGBA').getchannel('A'))
    return page
