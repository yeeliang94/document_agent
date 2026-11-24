"""How a caller steers a running job, and the shape of what the user sends it while it runs.

A message from the user is plain text, or {'text': ..., 'images': [{'name': ..., 'data': PNG bytes}, ...]} when
pictures go with it. Each picture is sent right after a line that names it, so text that says "picture X" and the
picture itself can be matched even when several messages arrive together.
"""

from __future__ import annotations

import base64

OMITTED = '[picture omitted from the record; the run folder or the job folder holds the file]'


class Control:
    """The app overrides these; the command line uses none of them."""

    def stop_requested(self) -> str | None:
        """'paused', 'cancelled' or 'waiting' to stop at the end of the current turn; None to carry on."""
        return None

    def new_messages(self) -> list:
        """What the user sent since the last call; each message is delivered once."""
        return []

    can_remember = False  # True when the caller can keep a lesson; the agent then gets the remember tool

    def remember(self, comment: str, lesson: str) -> str:
        """Note a lasting rule the agent took from one of the user's comments; returns words for the agent."""
        return 'Nothing can be remembered in this run.'

    def after_save(self) -> dict:
        """Extra facts for the agent about the files it just saved, added to what save_outputs returns."""
        return {}


def texts(messages: list) -> list[str]:
    return [m if isinstance(m, str) else m['text'] for m in messages]


def pictures(messages: list) -> list[dict]:
    return [image for m in messages if not isinstance(m, str) for image in m.get('images') or ()]


def user_content(text: str, images=()):
    """One user message: plain text, or the text followed by each picture under a line that names it."""
    if not images:
        return text
    parts = [{'type': 'input_text', 'text': text}]
    for image in images:
        url = 'data:image/png;base64,' + base64.b64encode(image['data']).decode()
        parts += [
            {'type': 'input_text', 'text': f'Picture {image["name"]}:'},
            {'type': 'input_image', 'detail': 'high', 'image_url': url},
        ]
    return parts


def instructions(messages: list):
    text = (
        'New instructions from the user. Decide what they change, redo only the affected work, and save again:\n'
        + '\n'.join(texts(messages))
    )
    return user_content(text, pictures(messages))


def with_message(prompt, content) -> list:
    """The conversation so far plus one more user message (text, or the parts user_content builds)."""
    items = prompt if isinstance(prompt, list) else [{'role': 'user', 'content': prompt}]
    return [*items, {'role': 'user', 'content': content}]


def without_pictures(value):
    """A conversation for the run record, with the bytes of each picture left out: the files are kept elsewhere,
    and a record full of pictures is too large to read."""
    if isinstance(value, dict):
        return {
            key: OMITTED if key == 'image_url' and isinstance(item, str) and item.startswith('data:') else without_pictures(item)
            for key, item in value.items()
        }  # fmt: skip
    if isinstance(value, list):
        return [without_pictures(item) for item in value]
    return value
