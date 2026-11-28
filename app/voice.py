"""Speak instead of type: the browser's audio goes to this app, which passes it to the transcription service and
sends the words back. The key never leaves the server, and no recording is written to disk.

Two ways, chosen on the Settings page:
- live: a Realtime transcription session over a WebSocket. The browser streams 24 kHz mono 16-bit audio; the words
  come back while the person is still speaking, and settle once they stop.
- after: the browser sends the whole recording when the person stops; the words stream back as the model writes them.

The browser side of the live relay speaks a small protocol of its own, so the page never sees the service's events:
  browser -> app: {"type": "start", "token": "..."} first, then binary audio frames, then {"type": "stop"}
  app -> browser: {"type": "ready"}, {"type": "delta", "text"}, {"type": "done", "text"},
                  {"type": "limit", "message"} and {"type": "error", "message"}
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import suppress

from .cleaning import clean

RATE = 24000  # samples per second the live model takes
BYTES_PER_SECOND = RATE * 2  # 16-bit mono
FILE_LIMIT_BYTES = 25 * 1024 * 1024  # the most the transcription endpoint takes in one file


class VoiceError(Exception):
    """A sentence the person can read. action 'settings' offers the Settings page, 'retry' a fresh try."""

    def __init__(self, message: str, action: str = ''):
        super().__init__(message)
        self.action = action


DROPPED = 'The connection to the transcription service dropped. What was heard so far is kept.'


def live_url(base_url: str) -> str:
    base = base_url.rstrip('/')
    for plain, secure in (('https://', 'wss://'), ('http://', 'ws://')):
        if base.startswith(plain):
            base = secure + base[len(plain) :]
    return base + '/realtime?intent=transcription'


def session_update(model: str) -> dict:
    # The live model refuses turn detection: the whole recording is one stretch of speech, finished by a commit.
    return {'type': 'session.update', 'session': {'type': 'transcription', 'audio': {'input': {
        'format': {'type': 'audio/pcm', 'rate': RATE},
        'transcription': {'model': model},
        'turn_detection': None,
    }}}}  # fmt: skip


def headers(api_key: str, provider: str) -> dict:
    found = {'Authorization': f'Bearer {api_key}'}
    if provider:
        found['custom-llm-provider'] = provider  # LiteLLM gateways, as for model calls
    return found


def _minutes(n: int) -> str:
    return f'{n} minute{"s" if n != 1 else ""}'


def limit_message(minutes: int) -> str:
    return f'Stopped at {_minutes(minutes)}, the longest recording set in Settings.'


def too_long_message(minutes: int) -> str:
    return f'The recording is longer than {_minutes(minutes)}, the longest recording set in Settings.'


def refused(detail: str) -> VoiceError:
    """The service's own reason, as a sentence that says where to look. A gateway may echo part of a key in its
    error, so the text is cleaned as every other error the page shows."""
    reason = clean(detail or 'no reason given').rstrip('.')
    return VoiceError(
        f'The transcription service refused the request: {reason}. Check the voice models in Settings.', 'settings'
    )


def service_error(event: dict) -> VoiceError:
    return refused((event.get('error') or {}).get('message') or event.get('message') or '')


async def relay(browser, upstream, max_minutes: int, settle_seconds: float) -> None:
    """Pass audio from browser to upstream and the words back, until the person stops and the text has settled.
    settle_seconds is how long the final text has to arrive after Stop (the voice_settle_seconds setting).

    browser has receive() -> {'bytes'|'text'}, send_json(); upstream has send(str) and async iteration over str.
    """
    limit = max_minutes * 60 * BYTES_PER_SECOND
    sent = 0

    async def from_browser():
        nonlocal sent
        while True:
            message = await browser.receive()
            if message.get('type') == 'websocket.disconnect':
                return False  # the page is gone: nobody waits for the words
            audio = message.get('bytes')
            if audio:
                room = limit - sent
                if room > 0:
                    await upstream.send(json.dumps({'type': 'input_audio_buffer.append',
                                                    'audio': base64.b64encode(audio[:room]).decode()}))  # fmt: skip
                    sent += min(len(audio), room)
                if sent >= limit:
                    await browser.send_json({'type': 'limit', 'message': limit_message(max_minutes)})
                    break
                continue
            text = message.get('text')
            if text and json.loads(text).get('type') == 'stop':
                break
        if sent:
            await upstream.send(json.dumps({'type': 'input_audio_buffer.commit'}))
        return True

    async def from_upstream():
        async for raw in upstream:
            event = json.loads(raw)
            kind = event.get('type', '')
            if kind == 'conversation.item.input_audio_transcription.delta':
                await browser.send_json({'type': 'delta', 'text': event.get('delta', '')})
            elif kind == 'conversation.item.input_audio_transcription.completed':
                await browser.send_json({'type': 'done', 'text': event.get('transcript', '')})
                return
            elif kind in ('conversation.item.input_audio_transcription.failed', 'error'):
                raise service_error(event)
        raise VoiceError(DROPPED, 'retry')

    reading = asyncio.create_task(from_browser())
    words = asyncio.create_task(from_upstream())
    try:
        # Words flow until the person stops; then the final text has a while to arrive.
        done, _ = await asyncio.wait({reading, words}, return_when=asyncio.FIRST_COMPLETED)
        if words in done:
            words.result()  # an error or a dropped connection, said plainly
            return
        if not reading.result():
            return
        if not sent:
            await browser.send_json({'type': 'done', 'text': ''})
            return
        try:
            await asyncio.wait_for(asyncio.shield(words), settle_seconds)
        except TimeoutError:
            raise VoiceError(
                'The transcription service did not finish in time. What was heard so far is kept.', 'retry'
            ) from None
    finally:
        for task in (reading, words):
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


EXTENSIONS = {'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'm4a', 'audio/mpeg': 'mp3', 'audio/wav': 'wav',
              'audio/x-wav': 'wav'}  # fmt: skip


def file_name(content_type: str) -> str:
    """The service tells formats apart by the file's extension."""
    return 'speech.' + EXTENSIONS.get(content_type.split(';')[0].strip().lower(), 'webm')


async def transcribe_file(client, model: str, name: str, data: bytes):
    """The words of one recording, streamed as {"type": "delta"|"done", "text"} as the model writes them."""
    stream = await client.audio.transcriptions.create(model=model, file=(name, data), stream=True)
    async for event in stream:
        if event.type == 'transcript.text.delta':
            yield {'type': 'delta', 'text': event.delta}
        elif event.type == 'transcript.text.done':
            yield {'type': 'done', 'text': event.text}
