"""A run that starts with only a goal and files: no template, no format and no month."""

import json

from openpyxl import Workbook
from test_sandbox_engine import FakeRun, client_for, finished

from app.engine import service
from app.engine.checks import run_checks
from app.engine.goal import goal_card

from helpers import wait_for_job, workbook_bytes

GOAL = 'Summarise the three supplier contracts and list every renewal date.\nOne page.'


def upload(client, files=None, **data):
    files = [('files', ('contract.txt', b'Renews on 1 March 2027.'))] + (files or [])
    return client.post('/api/jobs', data={'playbook_id': 'payroll', **data}, files=files)


def test_a_run_needs_a_goal_or_a_template_and_a_month_only_when_it_has_one(tmp_path):
    with client_for(tmp_path) as client:
        made = upload(client, instructions=GOAL)
        assert made.status_code == 201
        job = made.json()
        assert (job['period'], [f['role'] for f in job['files']]) == ('', ['inputs'])
        events = client.get(f'/api/jobs/{job["id"]}/events').json()
        assert events[0]['message'] == '1 source files received'
        # Neither a goal nor a template: there is nothing to decide the output from.
        refused = upload(client)
        assert refused.status_code == 400 and 'Say what the run should produce' in refused.json()['detail']
        assert upload(client, instructions='   ').status_code == 400
        # A template still works without a goal or a month; a month, when given, must be a month.
        template = [('template', ('template.xlsx', workbook_bytes()))]
        assert upload(client, files=template).status_code == 201
        assert upload(client, files=template, period='2026-10').json()['period'] == '2026-10'
        assert upload(client, instructions=GOAL, period='October').status_code == 400
        assert upload(client, instructions=GOAL, files=[('template', ('notes.docx', b'x'))]).status_code == 400
        # The run list tells runs without a month apart by how the goal begins.
        listed = {j['id']: j for j in client.get('/api/jobs').json()}
        assert listed[job['id']]['title'] == GOAL[:80] and listed[job['id']]['period'] == ''
        # What the agent is told: no deliverable is named, the form is its call, and no month is invented.
        store = client.app.state.store
        spec = service.job_spec(store, store.get(job['id']))
        assert spec.deliverables == () and spec.inputs == [] and spec.goal == GOAL
        card = goal_card(spec, {'packet.zip': '/mnt/data/packet.zip'}, 'none')
        assert 'The user gave no template and no written format' in card and 'Reporting month' not in card
        assert '- The files the goal calls for are saved and save_outputs reports no problems.' in card
        assert 'template' not in card.split('# Done means')[1]
        assert (
            service.job_title(store.get(job['id']))
            == 'Summarise the three supplier contracts and list every renewal date. One page.'
        )


def test_the_save_check_of_a_free_form_run_wants_usable_outputs_decisions_and_row_sources(tmp_path):
    with client_for(tmp_path) as client:
        store = client.app.state.store
        job = upload(client, instructions=GOAL).json()
        spec = service.job_spec(store, store.get(job['id']))
        assert 'every filled row below it counts as a row you wrote' in goal_card(spec, {'packet.zip': 'p'}, 'row')
    outputs = tmp_path / 'out'
    (outputs / 'reports').mkdir(parents=True)
    files = [{'path': 'contract.txt', 'bytes': 23, 'type': 'txt'}]
    decided = {'files': [{'path': 'contract.txt', 'type': 'txt', 'decision': 'used', 'reason': 'r', 'summary': 's'}]}
    (outputs / 'document_map.json').write_text(json.dumps(decided), encoding='utf-8')
    (outputs / 'notes.md').write_text('Made a one-page summary.', encoding='utf-8')
    first = run_checks(outputs, spec, files, 'none')
    assert first['passed'] is False and first['issues'] == [
        'No output file has been saved yet. Save what the goal calls for under /mnt/data/outputs/.'
    ]
    # A file that is empty, or is not what its name says, is not an output the user can use.
    (outputs / 'summary.xlsx').write_bytes(b'')
    (outputs / 'reports' / 'summary.docx').write_bytes(b'not a word file')
    (outputs / 'summary.pdf').write_bytes(b'plain text')
    assert run_checks(outputs, spec, files, 'none')['issues'] == [
        'reports/summary.docx: the file does not open as a docx file. Write it again.',
        'summary.pdf: the file does not open as a pdf file. Write it again.',
        'summary.xlsx: the file is empty.',
    ]
    for name in ('summary.xlsx', 'reports/summary.docx', 'summary.pdf'):
        (outputs / name).unlink()
    (outputs / 'reports' / 'summary.md').write_text('Renewal dates: 1 March 2027.', encoding='utf-8')
    assert run_checks(outputs, spec, files, 'none')['passed'] is True
    # A workbook the agent built: with row proof on, every row below the headings needs its sources.
    book = Workbook()
    sheet = book.active
    sheet.title = 'Renewals'
    for row in (['Supplier', 'Renews'], ['Acme', '2027-03-01'], ['Birch', '2027-06-30']):
        sheet.append(row)
    book.create_sheet('Lookup').sheet_state = 'hidden'
    book.save(outputs / 'renewals.xlsx')
    assert run_checks(outputs, spec, files, 'none')['passed'] is True
    unproven = run_checks(outputs, spec, files, 'row')
    assert unproven['passed'] is False and unproven['issues'] == ['provenance.json has not been saved yet.']
    proof = [{'file': 'renewals.xlsx', 'sheet': 'Renewals', 'rows': '2', 'sources': ['contract.txt']}]
    (outputs / 'provenance.json').write_text(json.dumps(proof), encoding='utf-8')
    partly = run_checks(outputs, spec, files, 'row')
    assert partly['passed'] is False and (partly['summary']['rows_written'], partly['summary']['rows_sourced']) == (
        2,
        1,
    )
    proof[0]['rows'] = '2-3'
    (outputs / 'provenance.json').write_text(json.dumps(proof), encoding='utf-8')
    assert run_checks(outputs, spec, files, 'row')['passed'] is True
    (outputs / 'document_map.json').unlink()
    assert run_checks(outputs, spec, files, 'row')['passed'] is False  # every file still needs its decision


def test_a_free_form_run_goes_through_the_engine_with_the_proof_level_of_the_settings_page(tmp_path, monkeypatch):
    async def behaviour(outputs, **_):
        (outputs / 'reports').mkdir(parents=True, exist_ok=True)
        (outputs / 'reports' / 'summary.md').write_text('Renewal dates: 1 March 2027.', encoding='utf-8')
        return finished()

    fake = FakeRun(behaviour)
    monkeypatch.setattr(service, 'run_job', fake)
    with client_for(tmp_path) as client:
        job = upload(client, instructions=GOAL).json()
        assert client.post(f'/api/jobs/{job["id"]}/start').status_code == 200
        done = wait_for_job(client, job['id'], {'completed'})
        # A file the agent put in a folder of its own is listed, and can be opened.
        assert [(a['name'], a['path']) for a in done['artifacts']] == [
            ('reports/summary.md', 'outputs/reports/summary.md'),
        ]
        shown = client.get(f'/api/jobs/{job["id"]}/preview', params={'path': 'outputs/reports/summary.md'})
        assert shown.status_code == 200 and 'Renewal dates' in shown.text
        assert fake.calls[0].options['proof'] == 'row' and fake.calls[0].spec.deliverables == ()
        client.put('/api/settings', json={'values': {'proof': 'none'}})
        again = upload(client, instructions=GOAL).json()
        client.post(f'/api/jobs/{again["id"]}/start')
        wait_for_job(client, again['id'], {'completed'})
        assert fake.calls[1].options['proof'] == 'none'
