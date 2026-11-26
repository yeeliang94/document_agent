"""Speak instead of type: the live relay and the after-stopping fallback, against a fake transcription service."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from app import voice
from app.config import Settings
from app.main import create_app
from tests.helpers import LOCAL

ORIGIN = {'origin': LOCAL}
LIVE = 'ws://127.0.0.1/api/voice/live'  # the test client names its own host on a WebSocket unless told
SECOND = b'\x01\x00' * voice.RATE  # one second of audio


class FakeService:
    """Stands in for the gateway's live session: one word back for each piece of audio, the whole text on commit."""

    def __init__(self, script=None, refuse=None):
        self.script = script  # events to send at once instead of following the audio
        self.refuse = refuse
        self.sent = []
        self.url = self.headers = None
        self.events = asyncio.Queue()

    def __call__(self, url, additional_headers=None, **_):
        if self.refuse:
            raise self.refuse
        self.url, self.headers = url, additional_headers
        return self

    async def __aenter__(self):
        for event in self.script or []:
            self.events.put_nowait(event)
        return self

    async def __aexit__(self, *_):
        return False

    async def send(self, text):
        event = json.loads(text)
        self.sent.append(event)
        if self.script is not None:
            return
        appended = [e for e in self.sent if e['type'] == 'input_audio_buffer.append']
        if event['type'] == 'input_audio_buffer.append':
            await self.events.put({'type': 'conversation.item.input_audio_transcription.delta', 'delta': f'word{len(appended)} '})
        if event['type'] == 'input_audio_buffer.commit':
            text = ''.join(f'word{n + 1} ' for n in range(len(appended))).strip()
            await self.events.put({'type': 'conversation.item.input_audio_transcription.completed', 'transcript': text})

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.events.get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)


@pytest.fixture
def make(tmp_path):
    def build(service=None, **settings):
        app = create_app(Settings(data_dir=tmp_path, api_key='test-key', **settings))
        app.state.voice_connect = service or FakeService()
        return TestClient(base_url=LOCAL, app=app)

    return build


def start(client, token=''):
    # Off by default: not every gateway offers it. Switched on with the app's own token, whatever the socket sends.
    token_header = {'authorization': f'Bearer {client.app.state.settings.access_token}'}
    client.put('/api/settings', json={'values': {'voice_live': True}}, headers=token_header)
    ws = client.websocket_connect(LIVE, headers=ORIGIN)
    socket = ws.__enter__()
    socket.send_json({'type': 'start', 'token': token})
    return ws, socket


def test_live_words_are_off_until_switched_on_and_voice_then_waits_for_the_stop(make):
    with make() as client:
        assert client.get('/api/voice').json()['mode'] == 'after'  # the saved choice is live, but live is off
        ws = client.websocket_connect(LIVE, headers=ORIGIN)
        socket = ws.__enter__()
        socket.send_json({'type': 'start', 'token': ''})
        refused = socket.receive_json()
        assert refused == {'type': 'error', 'message': 'Show words while you speak is turned off in Settings.', 'action': 'settings'}
        ws.__exit__(None, None, None)
        client.put('/api/settings', json={'values': {'voice_live': True}})
        assert client.get('/api/voice').json()['mode'] == 'live'


def test_words_arrive_in_order_while_audio_is_still_sent(make, tmp_path):
    service = FakeService()
    with make(service) as client:
        before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob('*') if p.is_file())
        ws, socket = start(client)
        assert socket.receive_json() == {'type': 'ready'}
        heard = []
        for _ in range(3):
            socket.send_bytes(SECOND[:4800])
            heard.append(socket.receive_json())  # each word comes back before the next piece is sent
        assert [h['text'] for h in heard] == ['word1 ', 'word2 ', 'word3 ']
        socket.send_json({'type': 'stop'})
        assert socket.receive_json() == {'type': 'done', 'text': 'word1 word2 word3'}
        ws.__exit__(None, None, None)
        after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob('*') if p.is_file())
    assert before == after  # nothing written: no recording is kept
    assert service.url == 'wss://api.openai.com/v1/realtime?intent=transcription'
    assert service.headers == {'Authorization': 'Bearer test-key'}
    assert service.sent[0]['session']['audio']['input']['transcription']['model'] == 'gpt-live-transcribe'
    assert service.sent[0]['session']['audio']['input']['turn_detection'] is None


def test_the_models_and_gateway_header_come_from_settings(make):
    service = FakeService()
    with make(service) as client:
        client.put('/api/settings', json={'values': {'voice_live_model': 'openai.eu.gpt-live-transcribe', 'litellm_provider': 'openai'}})
        ws, socket = start(client)
        socket.receive_json()
        socket.send_json({'type': 'stop'})
        assert socket.receive_json() == {'type': 'done', 'text': ''}
        ws.__exit__(None, None, None)
    assert service.sent[0]['session']['audio']['input']['transcription']['model'] == 'openai.eu.gpt-live-transcribe'
    assert service.headers['custom-llm-provider'] == 'openai'


def test_a_recording_past_the_limit_is_stopped_with_a_sentence(make):
    service = FakeService()
    with make(service) as client:
        client.put('/api/settings', json={'values': {'voice_max_minutes': 1}})
        ws, socket = start(client)
        socket.receive_json()
        socket.send_bytes(SECOND * 61)
        replies = [socket.receive_json()]
        while replies[-1]['type'] != 'done':  # what was heard up to the limit still settles
            replies.append(socket.receive_json())
        ws.__exit__(None, None, None)
    # The notice and the last word race each other, so only what arrives is checked, not its order.
    assert {'type': 'limit', 'message': 'Stopped at 1 minute, the longest recording set in Settings.'} in replies
    assert [r['type'] for r in replies].count('delta') == 1
    audio = sum(len(e['audio']) for e in service.sent if e['type'] == 'input_audio_buffer.append')
    assert audio == len(__import__('base64').b64encode(SECOND * 60))  # nothing past the limit is passed on


def test_a_refused_model_says_where_to_look(make):
    error = {'type': 'error', 'error': {'message': 'Model not found: gpt-nope.'}}
    with make(FakeService(script=[error])) as client:
        ws, socket = start(client)
        socket.receive_json()
        reply = socket.receive_json()
        ws.__exit__(None, None, None)
    assert reply == {'type': 'error', 'message': 'The transcription service refused the request: Model not found: gpt-nope. Check the voice models in Settings.', 'action': 'settings'}


def test_a_lost_connection_says_so(make):
    with make(FakeService(script=[{'type': 'conversation.item.input_audio_transcription.delta', 'delta': 'Hello '}, None])) as client:
        ws, socket = start(client)
        socket.receive_json()
        assert socket.receive_json() == {'type': 'delta', 'text': 'Hello '}
        reply = socket.receive_json()
        ws.__exit__(None, None, None)
    assert reply['type'] == 'error' and 'dropped' in reply['message'] and 'kept' in reply['message']
    assert reply['action'] == 'retry'


def test_a_gateway_that_refuses_the_live_connection_points_to_the_fallback(make):
    refused = InvalidStatus(Response(403, 'Forbidden', Headers()))
    with make(FakeService(refuse=refused)) as client:
        ws, socket = start(client)
        reply = socket.receive_json()
        ws.__exit__(None, None, None)
    assert reply['type'] == 'error' and 'HTTP 403' in reply['message'] and 'After you stop' in reply['message']


def test_the_access_token_is_checked_from_the_first_message(make):
    service = FakeService()
    with make(service, access_token='secret') as client:
        ws, socket = start(client, token='wrong')
        assert socket.receive_json() == {'type': 'error', 'message': 'Access token required.', 'action': ''}
        ws.__exit__(None, None, None)
        ws, socket = start(client, token='secret')
        assert socket.receive_json() == {'type': 'ready'}
        ws.__exit__(None, None, None)
    assert service.url  # only the right token opened a session


def test_another_site_cannot_open_the_microphone_relay(make):
    with make() as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(LIVE, headers={'origin': 'https://evil.example'}) as socket:
                socket.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(LIVE) as socket:  # no origin at all
                socket.receive_json()


def test_without_a_key_the_relay_says_what_to_do(tmp_path):
    with TestClient(base_url=LOCAL, app=create_app(Settings(data_dir=tmp_path, api_key=''))) as client:
        ws, socket = start(client)
        reply = socket.receive_json()
        ws.__exit__(None, None, None)
    assert 'OPENAI_API_KEY' in reply['message']


# --- After the person stops ------------------------------------------------------------------------------------


def lines(response):
    return [json.loads(line) for line in response.text.splitlines()]


def test_the_fallback_streams_the_text(make, monkeypatch):
    seen = {}

    async def fake(client, model, name, data):
        seen.update(model=model, name=name, size=len(data))
        yield {'type': 'delta', 'text': 'Hello '}
        yield {'type': 'done', 'text': 'Hello there.'}

    monkeypatch.setattr(voice, 'transcribe_file', fake)
    with make() as client:
        response = client.post('/api/voice/transcribe', content=b'abc', headers={'content-type': 'audio/webm'})
    assert lines(response) == [{'type': 'delta', 'text': 'Hello '}, {'type': 'done', 'text': 'Hello there.'}]
    assert seen == {'model': 'gpt-transcribe', 'name': 'speech.webm', 'size': 3}
    assert voice.file_name('audio/mp4') == 'speech.m4a' and voice.file_name('audio/ogg;codecs=opus') == 'speech.ogg'


def test_the_fallback_says_when_the_service_refuses(make, monkeypatch):
    async def fake(*_):
        raise RuntimeError('model not found for key sk-abcdefghijklmnopqrstuvwx')
        yield

    monkeypatch.setattr(voice, 'transcribe_file', fake)
    with make() as client:
        response = client.post('/api/voice/transcribe', content=b'abc', headers={'content-type': 'audio/ogg'})
    [reply] = lines(response)
    assert reply['type'] == 'error' and reply['message'].endswith('Check the voice models in Settings.')
    assert reply['action'] == 'settings'
    assert 'sk-abc' not in reply['message']  # a gateway that echoes a key does not show it on the page


def test_the_fallback_refuses_a_recording_past_the_limit(make):
    with make() as client:
        client.put('/api/settings', json={'values': {'voice_max_minutes': 1}})
        response = client.post('/api/voice/transcribe', content=SECOND * 61, headers={'content-type': 'audio/webm'})
        empty = client.post('/api/voice/transcribe', content=b'', headers={'content-type': 'audio/webm'})
    assert response.status_code == 413
    assert response.json()['detail'] == 'The recording is longer than 1 minute, the longest recording set in Settings.'
    assert empty.status_code == 400


def test_the_live_address_follows_the_service_address():
    assert voice.live_url('https://gw.example.com/v1/') == 'wss://gw.example.com/v1/realtime?intent=transcription'
    assert voice.live_url('http://127.0.0.1:4000/v1') == 'ws://127.0.0.1:4000/v1/realtime?intent=transcription'


def test_a_page_that_goes_away_does_not_hold_the_session(make):
    """The relay returns at once when the page disconnects, rather than waiting for words nobody will see."""

    class Browser:
        def __init__(self):
            self.messages = [{'type': 'websocket.receive', 'bytes': SECOND[:4800]}, {'type': 'websocket.disconnect'}]

        async def receive(self):
            return self.messages.pop(0)

        async def send_json(self, _):
            pass

    async def run():
        service = FakeService(script=[])
        async with service:
            await asyncio.wait_for(voice.relay(Browser(), service, 2), 2)
        return service

    service = asyncio.run(run())
    assert [e['type'] for e in service.sent] == ['input_audio_buffer.append']  # no commit for a page that left
