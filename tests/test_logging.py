"""Live event streaming."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from helpers import LOCAL


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
