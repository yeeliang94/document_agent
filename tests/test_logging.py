"""Event logging, history compaction and live event streaming."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessagesTypeAdapter

from app.config import Settings
from app.main import create_app
from app.store import Store

from helpers import LOCAL, create_job, final, run_tool, scripted, wait_for_job


def test_model_events_log_only_new_messages(tmp_path):
    model, _ = scripted([('list_files', {}), ('list_files', {}), ('list_files', {}), final('partial', ['test'])])
    with TestClient(
        create_app(Settings(data_dir=tmp_path, api_key='k'), model_override=model), base_url=LOCAL
    ) as client:
        job = create_job(client)
        client.post(f'/api/jobs/{job["id"]}/start')
        wait_for_job(client, job['id'])
        started = [e for e in client.get(f'/api/jobs/{job["id"]}/events').json() if e['name'] == 'model.started']
    assert len(started) == 4
    assert 'instructions' in started[0] and all('instructions' not in e for e in started[1:])
    assert [e['history_messages'] for e in started] == [1, 3, 5, 7]
    assert all(len(e['input']) <= 2 for e in started)


def test_compaction_reuses_checkpoint_instead_of_new_copies(tmp_path):
    source = ('Synthetic payroll source row. ' * 300).encode()
    model, _ = scripted(
        [('read_document', {'path': 'inputs/large.txt', 'offset': i}) for i in range(8)] + [final('partial', ['test'])]
    )
    settings = Settings(data_dir=tmp_path, api_key='k', context_chars=3000)
    with TestClient(create_app(settings, model_override=model), base_url=LOCAL) as client:
        job = create_job(client, files=[('files', ('large.txt', source, 'text/plain'))])
        client.post(f'/api/jobs/{job["id"]}/start')
        wait_for_job(client, job['id'])
        events = client.get(f'/api/jobs/{job["id"]}/events').json()
    assert any(e['name'] == 'context.compact' for e in events)
    assert not list((tmp_path / 'jobs' / job['id'] / 'working').glob('context-*.json'))


def test_compacted_result_pointers_survive_checkpoint_replacement(tmp_path):
    files = [('files', (f'source-{i}.txt', ('Source details. ' * 200 + f'UNIQUE_TAIL_{i}').encode())) for i in range(8)]
    model, _ = scripted(
        [('read_document', {'path': f'inputs/source-{i}.txt'}) for i in range(8)] + [final('partial', ['test'])]
    )
    settings = Settings(data_dir=tmp_path, api_key='test', context_chars=3000)
    app = create_app(settings, model_override=model)
    with TestClient(app, base_url=LOCAL) as client:
        job = create_job(client, files=files)
        client.post(f'/api/jobs/{job["id"]}/start')
        assert wait_for_job(client, job['id'])['status'] == 'partial'
    # Reopen the store, just as after an application restart.
    store = Store(tmp_path)
    root = store.path(job['id'])
    history = json.loads((root / 'working/history.json').read_text())
    pointers = [
        part['content']
        for msg in history
        for part in msg['parts']
        if isinstance(part.get('content'), dict) and 'stored_history' in part['content']
    ]
    assert pointers
    for pointer in pointers:
        index = int(pointer['tool_call_id'].split('-')[-1]) - 1
        archived = root / pointer['stored_history']
        messages = ModelMessagesTypeAdapter.validate_json(archived.read_bytes())
        assert f'UNIQUE_TAIL_{index}' in messages[0].parts[0].content['text']
        reread = run_tool(store, job['id'], 'read_document', {'path': pointer['stored_history']})
        assert f'UNIQUE_TAIL_{index}' in reread['text']
    assert len(list((root / 'working/tool-results').glob('*.json'))) <= 8


@pytest.mark.parametrize('status', ['completed', 'waiting'])
def test_terminal_sse_drains_all_batches_and_honors_replay_cursor(tmp_path, status):
    app = create_app(Settings(data_dir=tmp_path, api_key=''))
    with TestClient(app, base_url=LOCAL) as client:
        store = app.state.store
        job = store.create('payroll', '2026-10', '', '')
        events = [store.event(job['id'], 'test.activity', str(i)) for i in range(1005)]
        store.update(job['id'], status=status)
        url = f'/api/jobs/{job["id"]}/stream'
        response = client.get(url)
        ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith('id: ')]
        assert ids == [event['seq'] for event in events]
        assert response.text.count('event: state') == 1
        response = client.get(url, headers={'Last-Event-ID': str(events[499]['seq'])})
        ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith('id: ')]
        assert ids == [event['seq'] for event in events[500:]]


def test_malformed_last_event_id_replays_from_the_start(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key=''))
    with TestClient(app, base_url=LOCAL) as client:
        store = app.state.store
        job = store.create('payroll', '2026-10', '', '')
        event = store.event(job['id'], 'test.activity', 'one')
        store.update(job['id'], status='completed')
        response = client.get(f'/api/jobs/{job["id"]}/stream', headers={'Last-Event-ID': 'not-a-number'})
        assert response.status_code == 200
        assert f'id: {event["seq"]}' in response.text
