from app.store import Store


def test_job_and_events_survive_reopen(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'Prepare payroll', 'Team context')
    first = store.event(job['id'], 'tool.start', 'Read worksheet', arguments={'sheet': 'Employees'})
    second = store.event(job['id'], 'tool.finish', 'Read complete', result={'rows': 2})
    store.update(job['id'], status='paused', summary='Two employees extracted')
    reopened = Store(tmp_path)
    assert reopened.get(job['id'])['status'] == 'paused'
    assert reopened.events(job['id'], after=first['seq']) == [second]
    assert reopened.get(job['id'])['instructions'] == 'Prepare payroll'


def test_logging_redacts_credentials_and_image_payloads(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '', '', '')
    event = store.event(
        job['id'],
        'model',
        'Request',
        arguments={
            'api_key': 'secret',
            'authorization': 'Bearer secret',
            'image': 'data:image/png;base64,AAAA',
            'text': 'sk-abcdefghijklmnop',
        },
    )
    assert event['arguments']['api_key'] == '[redacted]'
    assert event['arguments']['authorization'] == '[redacted]'
    assert event['arguments']['image'] == '[image payload omitted]'
    assert 'sk-' not in event['arguments']['text']
    mirror = (store.path(job['id']) / 'events.jsonl').read_text()
    assert 'Bearer secret' not in mirror
