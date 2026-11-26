"""How a caller steers a running job, and the shape of what the user sends it while it runs.

A message from the user is plain text, or {'text': ..., 'images': [{'name': ..., 'data': ..., 'type': ...}, ...]} when
pictures go with it (data is the image's bytes; type its media type, image/png when left out). Each picture is sent right after a line that names it, so text that says "picture X" and the
picture itself can be matched even when several messages arrive together. A message can also carry 'files': files
the user added to the packet, each {'packet': its path in the packet, 'local': where it is on this machine}. The
text already names them; the engine puts them in the sandbox and says where.
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

    def interrupt_requested(self) -> bool:
        """True when the user asked for their waiting messages to be read now, without waiting for the step to end."""
        return False

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
        url = f'data:{image.get("type", "image/png")};base64,' + base64.b64encode(image['data']).decode()
        parts += [
            {'type': 'input_text', 'text': f'Picture {image["name"]}:'},
            {'type': 'input_image', 'detail': 'high', 'image_url': url},
        ]
    return parts


def added_files(messages: list) -> list[dict]:
    return [f for m in messages if not isinstance(m, str) for f in m.get('files') or ()]


INTERRUPTED = (
    'The user stopped your last step before it finished, to tell you this now. That step is not in the conversation '
    'and may be half done: files it wrote in the sandbox can be incomplete, so check before you rely on them.\n'
)


def instructions(messages: list, placed=(), interrupted: bool = False, running=()):
    """The user's messages as one message to the agent. placed: where the files they added landed in the sandbox.
    running: what the scripts the user stopped were doing, so the agent knows what to check."""
    cut = ''
    if interrupted:
        cut = INTERRUPTED + ''.join(f'It was running a script that: {r}\n' for r in running if r)
    text = (
        'New message from the user. Answer anything it asks in your summary; where it changes the work, redo only the '
        'affected work and save again:\n' + '\n'.join(texts(messages))
    )
    if placed:
        text += '\nThe files the user added are in your sandbox at:\n' + '\n'.join(
            f'- {f["remote"]} (packet path {f["packet"]})' for f in placed
        )
    return user_content(cut + text, pictures(messages))


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
