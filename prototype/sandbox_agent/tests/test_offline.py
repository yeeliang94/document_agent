"""Offline tests: no network, no model. They prove the packet, checks, scorer, run folder and harness helpers agree."""

import asyncio
import json
import zipfile
from types import SimpleNamespace

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.datavalidation import DataValidation
from sandbox_agent import packets
from sandbox_agent.experiment import summary
from sandbox_agent.score import score_questions
from sandbox_agent.selftest import perfect_outputs, selftest

from app.engine.agent import _slug, _tool_error, ask, digest, folder_zip, run_job, sync_outputs
from app.engine.checks import inventory, run_checks
from app.engine.endpoint import Settings
from app.engine.goal import goal_card
from app.engine.jobspec import DEFAULT_ANSWER, Answerer, Deliverable, JobSpec, load_job
from app.engine.rules import check_format, check_rules
from app.engine.runstore import RunStore

WORKBOOK = 'filled_output.xlsx'


@pytest.fixture(scope='module')
def packet(tmp_path_factory):
    out = tmp_path_factory.mktemp('packets')
    key = packets.build('payroll_consolidation', out)
    return out / 'payroll_consolidation', key


def job_of(folder):
    return load_job(folder)


def check(folder, target):
    return run_checks(target, job_of(folder), inventory(folder / 'packet.zip'))


def test_packet_is_nested_and_messy(packet):
    out, key = packet
    files = inventory(out / 'packet.zip')
    assert [f['path'] for f in files] == sorted(key['files'])
    assert all(f['member'] == 'packet/' + f['path'] for f in files)
    assert max(f['path'].count('/') for f in files) >= 5
    assert any(f['junk'] for f in files)
    sg = load_workbook(out / 'packet' / 'Region_APAC/Singapore/Payroll/2026/10_October/SG01_Payroll_Oct2026_FINAL.xlsx')
    assert sg['_codes'].sheet_state == 'hidden' and len(sg.sheetnames) == 5
    assert len(key['excluded_leavers']) == 3  # left 3, 20 and 31 Oct; the 14 Nov leaver stays in
    rows = key['deliverables'][WORKBOOK]['sheets']['Employees']['rows']
    assert len(rows) == 48 + 37 + 63 + 29 + 41 - 3


def test_perfect_output_passes_checks_and_scores_full_marks(packet, tmp_path):
    out, key = packet
    result = selftest(job_of(out), key, tmp_path / 'outputs')
    assert result['passed'], result
    assert result['score']['relevance_accuracy'] == 1


@pytest.mark.parametrize('name', [n for n in packets.available() if n != 'large_packet'])
def test_every_packet_is_consistent(name, tmp_path):
    key = packets.build(name, tmp_path)
    result = selftest(load_job(tmp_path / name), key, tmp_path / 'perfect')
    assert result['passed'], result['check']['issues'][:5]


def test_checks_catch_broken_output(packet, tmp_path):
    out, key = packet
    target = tmp_path / 'outputs'
    perfect_outputs(job_of(out), key, target)
    wb = load_workbook(target / WORKBOOK)
    wb['Entity Summary']['B2'] = 46  # formula replaced by a value
    wb['Lists']['A3'] = 'renamed role'  # hidden reference value changed
    wb['Lists'].sheet_state = 'visible'
    wb['Employees']['I2'] = 'n/a'  # number-only cell
    wb['Employees']['D3'] = 'Legal'  # not in the dropdown
    wb['Employees']['G4'] = None  # required number left blank
    wb['Employees'].append(list(next(wb['Employees'].iter_rows(min_row=5, max_row=5, values_only=True))))
    wb.save(target / WORKBOOK)
    (target / 'document_map.json').write_text(
        '{"files": [{"path": "Region_AMER/USA/payroll/2026/Q4/october/us01_payroll_register_2026-10.csv", '
        '"decision": "maybe", "reason": "x"}]}',
        encoding='utf-8',
    )
    text = '\n'.join(check(out, target)['issues'])
    for expected in [
        'Entity Summary!B2 was', 'Lists!A3 was', 'visibility changed', 'I2', 'must be a number',
        "'Legal' is not in its dropdown", 'G4 is blank', 'duplicates row 5', 'no entry with "path" for 10 file',
        '"decision" must be one of',
    ]:  # fmt: skip
        assert expected in text, expected


def _tiny_job(tmp_path, deliverable, rules=()):
    zip_path = tmp_path / 'p.zip'
    with zipfile.ZipFile(zip_path, 'w') as archive:
        archive.writestr('x.csv', 'a')
        archive.writestr('y.csv', 'b')
    return JobSpec(id='t', module='ap', goal='g', zip=zip_path, deliverables=(deliverable,), rules=tuple(rules))


def _save_map(outputs, decisions):
    files = [{'path': p, 'decision': d, 'reason': 'because'} for p, d in decisions.items()]
    (outputs / 'document_map.json').write_text(json.dumps({'files': files}), encoding='utf-8')


def test_checks_work_on_any_template(tmp_path):
    template = tmp_path / 't.xlsx'
    wb = Workbook()
    wb.active.title = 'Data'
    wb.active.append(['Code', 'Amount'])
    rule = DataValidation(type='list', formula1='"A,B"', allow_blank=True)
    rule.add('A2:A100')
    wb.active.add_data_validation(rule)
    wb.save(template)
    job = _tiny_job(tmp_path, Deliverable('out.xlsx', template=template))
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    wb.active.append(['A', 'free text is fine'])
    wb.active.append(['C', 1])
    wb.save(outputs / 'out.xlsx')
    _save_map(outputs, {'x.csv': 'used', 'y.csv': 'not relevant'})
    issues = run_checks(outputs, job, inventory(job.zip), proof='none')['issues']
    assert issues == ["Data!A3 = 'C' is not in its dropdown ['A', 'B']."]


JOURNAL = {
    'sheets': [{'name': 'Journal', 'columns': [
        {'name': 'Ref', 'type': 'text', 'required': True, 'pattern': '^PAY-'},
        {'name': 'Date', 'type': 'date', 'required': True},
        {'name': 'Account', 'type': 'text', 'allowed': ['2100', '1010']},
        {'name': 'Debit', 'type': 'number', 'min': 0},
        {'name': 'Credit', 'type': 'number', 'min': 0}], 'unique': [['Ref', 'Account']]}],
    'rules': [{'type': 'balanced', 'sheet': 'Journal', 'debit': 'Debit', 'credit': 'Credit', 'group_by': 'Ref'},
              {'type': 'one_side', 'sheet': 'Journal', 'columns': ['Debit', 'Credit']}],
}  # fmt: skip


def _journal(rows):
    wb = Workbook()
    wb.active.title = 'Journal'
    wb.active.append(['Ref', 'Date', 'Account', 'Debit', 'Credit'])
    for row in rows:
        wb.active.append(row)
    return wb


def test_a_format_deliverable_is_checked_against_its_specification():
    good = _journal([['PAY-1', '2026-11-20', '2100', 100, 0], ['PAY-1', '2026-11-20', 1010, 0, 100]])
    assert check_format(good, JOURNAL, 'j.xlsx') == [] and check_rules(good, JOURNAL['rules'], 'j.xlsx') == []
    bad = _journal([['X-1', 'soon', '9999', -5, 0], ['X-1', '2026-11-20', '9999', 3, 3]])
    text = '\n'.join(check_format(bad, JOURNAL, 'j.xlsx') + check_rules(bad, JOURNAL['rules'], 'j.xlsx'))
    for expected in ['does not match the pattern', "'soon' must be a date", 'is not one of', 'below the minimum',
                     'repeats', 'differ by', 'exactly one of Debit or Credit']:  # fmt: skip
        assert expected in text, expected
    wrong_headers = Workbook()
    wrong_headers.active.title = 'Journal'
    wrong_headers.active.append(['Account', 'Ref'])
    assert 'must hold the headers' in check_format(wrong_headers, JOURNAL, 'j.xlsx')[0]


def test_provenance_must_cover_every_written_row_with_used_files(tmp_path):
    spec_path = tmp_path / 'format.json'
    spec_path.write_text(json.dumps(JOURNAL), encoding='utf-8')
    job = _tiny_job(tmp_path, Deliverable('j.xlsx', format=JOURNAL, format_path=spec_path))
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    _journal([['PAY-1', '2026-11-20', '2100', 100, 0], ['PAY-1', '2026-11-20', '1010', 0, 100]]).save(
        outputs / 'j.xlsx'
    )
    _save_map(outputs, {'x.csv': 'used', 'y.csv': 'not_relevant'})
    files = inventory(job.zip)
    (outputs / 'provenance.json').write_text(
        json.dumps([{'file': 'j.xlsx', 'sheet': 'Journal', 'rows': '2', 'sources': ['y.csv']}]), encoding='utf-8'
    )
    text = '\n'.join(run_checks(outputs, job, files)['issues'])
    assert "marks it 'not_relevant'" in text and 'rows 3' in text
    (outputs / 'provenance.json').write_text(
        json.dumps([{'file': 'j.xlsx', 'sheet': 'Journal', 'rows': '2-3', 'sources': ['/mnt/data/packet/x.csv']}]),
        encoding='utf-8',
    )
    assert run_checks(outputs, job, files) == {'passed': True, 'issues': []}


def test_missing_outputs_are_reported(packet, tmp_path):
    out, _ = packet
    issues = run_checks(tmp_path, job_of(out), [])['issues']
    assert issues == [f'{WORKBOOK} has not been saved yet.', 'document_map.json has not been saved yet.',
                      'provenance.json has not been saved yet.']  # fmt: skip


def test_goal_card_states_goal_rules_and_done(packet, tmp_path):
    out, _ = packet
    job = job_of(out)
    card = goal_card(job, {'output_template.xlsx': '/mnt/data/t.xlsx'}, 'row')
    for expected in ['# Goal', 'Consolidate the October 2026', 'House rules for payroll', '/mnt/data/t.xlsx',
                     'Sheet "Lists" (hidden)', 'dropdown from', 'provenance.json']:  # fmt: skip
        assert expected in card, expected
    spec_path = tmp_path / 'f.json'
    spec_path.write_text(json.dumps(JOURNAL), encoding='utf-8')
    journal = _tiny_job(tmp_path, Deliverable('j.xlsx', format=JOURNAL, format_path=spec_path))
    card = goal_card(journal, {'f.json': '/mnt/data/f.json'}, 'none')
    assert 'balance within each Ref' in card and 'provenance.json' not in card


def test_scripted_answers_and_the_question_limit(tmp_path):
    answerer = Answerer(({'keywords': ['E104'], 'answer': 'Use 6,500.'},))
    assert asyncio.run(answerer.answer('Which salary is right for e104?')) == 'Use 6,500.'
    assert asyncio.run(answerer.answer('Anything else?')) == DEFAULT_ANSWER
    store = RunStore(tmp_path)
    job = SimpleNamespace(store=store, answerer=answerer)
    assert asyncio.run(ask(job, 'Third?')) == DEFAULT_ANSWER
    reply = asyncio.run(ask(job, 'Fourth?'))
    assert 'limit' in reply and len(answerer.log) == 3


def test_questions_are_scored_by_keywords():
    expected = [{'topic': 'salary', 'keywords': ['E104']}]
    assert score_questions(expected, [{'question': 'What about E104?'}])['asked_expected'] is True
    assert score_questions(expected, [])['asked_expected'] is False
    assert score_questions([], [{'question': 'x'}])['unneeded_questions'] == 1


def test_experiment_summary_groups_by_shape_and_packet():
    row = {'packet': 'p', 'shape': 'A', 'run': 'r', 'status': 'complete', 'error': None, 'seconds': 120,
           'requests': 10, 'input_tokens': 5000, 'row_recall': 1.0, 'row_precision': 1.0, 'cell_accuracy': 0.9,
           'relevance_accuracy': 1.0, 'asked_expected': None, 'unneeded_questions': 0}  # fmt: skip
    text = summary([row, {**row, 'shape': 'B', 'status': 'partial', 'error': 'turn limit'}])
    assert '| A | 1 | 1/1 | 1.000 | 1.000 | 0.900' in text and 'turn limit' in text


def test_inventory_handles_zip_without_top_folder(tmp_path):
    path = tmp_path / 'flat.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('a.csv', 'x')
        archive.writestr('sub/b.csv', 'y')
        archive.writestr('__MACOSX/._a.csv', 'junk')
    assert [(f['path'], f['member']) for f in inventory(path)] == [('a.csv', 'a.csv'), ('sub/b.csv', 'sub/b.csv')]


def test_scout_zip_takes_the_right_files_even_when_names_collide(tmp_path):
    path = tmp_path / 'flat.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('A/x.csv', 'from A')
        archive.writestr('B/A/x.csv', 'from B/A')
    data, count = folder_zip(path, inventory(path), 'A')
    with zipfile.ZipFile(__import__('io').BytesIO(data)) as archive:
        assert count == 1 and archive.namelist() == ['A/x.csv'] and archive.read('A/x.csv') == b'from A'


def test_slugs_stay_unique_for_similar_folders():
    assert _slug('A_B') != _slug('A-B')
    long = 'Region/' + 'x' * 80
    assert _slug(long + '/one') != _slug(long + '/two')


def test_digest_keeps_the_coordinator_result_small():
    sheet = {'name': 'S', 'visibility': 'visible', 'header_row': 4, 'data_rows': 9, 'columns': ['a'] * 30,
             'sample_rows': [['v'] * 30] * 3}  # fmt: skip
    report = {'folder': 'F', 'files': [{'path': 'F/a.xlsx', 'apparent_role': 'register', 'sheets': [sheet]}]}
    small = digest(report, '/mnt/data/x.json')
    assert small['files'][0]['sheets'] == ['S (visible, header row 4, 9 rows, 30 columns)']
    assert len(json.dumps(small)) < len(json.dumps(report)) / 2


def test_tool_errors_reach_the_agent_and_the_log(tmp_path):
    store = RunStore(tmp_path)
    message = _tool_error(SimpleNamespace(context=SimpleNamespace(store=store)), FileNotFoundError('gone.xlsx'))
    assert 'FileNotFoundError: gone.xlsx' in message
    assert 'gone.xlsx' in (store.dir / 'events.jsonl').read_text(encoding='utf-8')


class FakeBox:
    def __init__(self, files):
        self.files = files

    async def list_files(self):
        return [{'id': path, 'path': path} for path in self.files]

    async def download_id(self, file_id):
        return self.files[file_id]


def test_sync_replaces_outputs_and_drops_deleted_files(tmp_path):
    store = RunStore(tmp_path)
    asyncio.run(sync_outputs(FakeBox({'/mnt/data/outputs/a.txt': b'1', '/mnt/data/outputs/sub/b.txt': b'2',
                                      '/mnt/data/packet/in.xlsx': b'no'}), store))  # fmt: skip
    assert sorted(p.relative_to(store.outputs).as_posix() for p in store.outputs.rglob('*') if p.is_file()) == [
        'a.txt', 'sub/b.txt']  # fmt: skip
    asyncio.run(sync_outputs(FakeBox({'/mnt/data/outputs/a.txt': b'new'}), store))
    assert [p.name for p in store.outputs.rglob('*') if p.is_file()] == ['a.txt']
    assert (store.outputs / 'a.txt').read_bytes() == b'new'
    with pytest.raises(ValueError):
        asyncio.run(sync_outputs(FakeBox({'/mnt/data/outputs/../../escape.txt': b'x'}), store))
    assert (store.outputs / 'a.txt').read_bytes() == b'new'  # a refused save leaves the previous outputs intact


def test_a_bad_upload_is_recorded_as_failed(tmp_path):
    bad = tmp_path / 'not_a.zip'
    bad.write_text('this is not a zip', encoding='utf-8')
    template = tmp_path / 't.xlsx'
    Workbook().save(template)
    spec = JobSpec(
        id='bad', module='payroll', goal='g', zip=bad, deliverables=(Deliverable('o.xlsx', template=template),)
    )
    store = run_job(spec, Settings(api_key='unused'), tmp_path / 'runs', echo=lambda _: None)
    store = asyncio.run(store)
    record = json.loads((store.dir / 'run.json').read_text(encoding='utf-8'))
    assert record['status'] == 'failed' and 'BadZipFile' in record['error']
    assert (
        json.loads((store.dir / 'events.jsonl').read_text(encoding='utf-8').splitlines()[-1])['event'] == 'run.finished'
    )


def test_settings_never_print_the_key():
    assert 'secret' not in repr(Settings(api_key='secret'))


def test_model_calls_and_sandbox_calls_use_the_same_endpoint():
    settings = Settings(api_key='k', base_url='https://proxy.example/v1', provider='azure')
    model = settings.model_for()
    client = model._client
    assert str(client.base_url) == 'https://proxy.example/v1/'
    assert client.default_headers['custom-llm-provider'] == 'azure'
    assert model.model == settings.model and settings.model_for('other').model == 'other'


def test_one_client_per_run_and_model_on_create():
    settings = Settings(api_key='k', model='alias', model_on_create=True)
    assert settings.client() is settings.client() is settings.model_for()._client
    assert settings.sandbox_create_extra == {'model': 'alias'}
    assert Settings(api_key='k').sandbox_create_extra is None


def _settings_env(tmp_path, monkeypatch, env_text):
    """Point load_settings at a throwaway project .env, with no leftovers from the real one."""
    import sandbox_agent.config as config

    for name in ('OPENAI_BASE_URL', 'OPENAI_MODEL', 'OPENAI_API_KEY', 'PROXY_API_KEY'):
        monkeypatch.setenv(name, '')  # records the original value, so load_dotenv's writes are undone afterwards
        monkeypatch.delenv(name)
    (tmp_path / '.env').write_text(env_text, encoding='utf-8')
    monkeypatch.setattr(config, 'PROJECT_ROOT', tmp_path)
    import argparse

    parser = argparse.ArgumentParser()
    config.add_endpoint_args(parser)
    return config.load_settings, parser


def test_proxy_comes_from_the_main_app_env_like_the_app(tmp_path, monkeypatch):
    load_settings, parser = _settings_env(
        tmp_path, monkeypatch, 'OPENAI_API_KEY=k\nOPENAI_BASE_URL=https://proxy.corp/v1\nOPENAI_MODEL=alias\n'
    )
    settings = load_settings(parser.parse_args([]), tmp_path / 'no_file.json')
    assert (settings.base_url, settings.model, settings.origin) == (
        'https://proxy.corp/v1',
        'alias',
        'OPENAI_BASE_URL (.env)',
    )
    assert load_settings(parser.parse_args(['--model', 'other']), tmp_path / 'no_file.json').model == 'other'


def test_settings_file_adds_litellm_options_and_can_override(tmp_path, monkeypatch):
    load_settings, parser = _settings_env(
        tmp_path, monkeypatch, 'OPENAI_API_KEY=k\nOPENAI_BASE_URL=https://proxy.corp/v1\n'
    )
    path = tmp_path / 'sandbox_settings.json'
    path.write_bytes(b'\xef\xbb\xbf' + b'{"provider": "azure", "model_on_create": true}')  # Notepad's byte-order mark
    settings = load_settings(parser.parse_args([]), path)
    assert (settings.base_url, settings.provider, settings.model_on_create) == ('https://proxy.corp/v1', 'azure', True)
    assert load_settings(parser.parse_args(['--no-model-on-create']), path).model_on_create is False
    path.write_text('{"base_url": "https://other.corp/v1"}', encoding='utf-8')
    assert load_settings(parser.parse_args([]), path).origin == 'sandbox_settings.json'
    for body, message in [
        ('{"api_key": "no"}', 'unknown keys'),
        ('{"model_on_create": "false"}', 'true or false'),
        ('{"base_url": "http://proxy.corp/v1"}', 'must use HTTPS'),
    ]:
        path.write_text(body, encoding='utf-8')
        with pytest.raises(SystemExit, match=message):
            load_settings(parser.parse_args([]), path)
    path.write_text('{"base_url": "http://localhost:4000/v1"}', encoding='utf-8')
    assert load_settings(parser.parse_args([]), path).base_url == 'http://localhost:4000/v1'  # the app allows loopback


def test_no_endpoint_anywhere_never_means_public_openai(tmp_path, monkeypatch):
    load_settings, parser = _settings_env(tmp_path, monkeypatch, 'OPENAI_API_KEY=k\n')
    with pytest.raises(SystemExit, match='Set OPENAI_BASE_URL'):
        load_settings(parser.parse_args([]), tmp_path / 'no_file.json')
    explicit = load_settings(parser.parse_args(['--base-url', 'https://api.openai.com/v1']), tmp_path / 'no_file.json')
    assert explicit.origin == 'command line'


def test_an_output_open_in_excel_gives_a_clear_message(tmp_path, monkeypatch):
    from pathlib import Path

    store = RunStore(tmp_path)
    store.replace_outputs({'a.xlsx': b'old'})
    real_rename = Path.rename

    def locked(self, target):
        if self == store.outputs:
            raise PermissionError(13, 'Access is denied')
        return real_rename(self, target)

    monkeypatch.setattr(Path, 'rename', locked)
    with pytest.raises(PermissionError, match='open in another program, usually Excel'):
        store.replace_outputs({'a.xlsx': b'new'})
    assert (store.outputs / 'a.xlsx').read_bytes() == b'old'
    assert not (store.dir / 'outputs.staging').exists()


def test_a_failed_swap_puts_the_last_good_outputs_back(tmp_path, monkeypatch):
    from pathlib import Path

    store = RunStore(tmp_path)
    store.replace_outputs({'a.xlsx': b'old'})
    real_rename = Path.rename

    def flaky(self, target):
        if self.name == 'outputs.staging':
            raise PermissionError(13, 'held by antivirus')
        return real_rename(self, target)

    monkeypatch.setattr(Path, 'rename', flaky)
    monkeypatch.setattr('app.engine.runstore.time.sleep', lambda _: None)
    with pytest.raises(PermissionError):
        store.replace_outputs({'a.xlsx': b'new'})
    assert (store.outputs / 'a.xlsx').read_bytes() == b'old'


def test_a_slow_scout_is_cut_off_and_the_rest_still_return(tmp_path, monkeypatch):
    import app.engine.agent as agent_module

    async def fake_scout(job, folder, focus):
        if folder == 'slow':
            await asyncio.sleep(5)
        return {'folder': folder, 'files': []}

    monkeypatch.setattr(agent_module, '_scout', fake_scout)
    monkeypatch.setattr(agent_module, 'SCOUT_TIMEOUT_SECONDS', 0.2)
    files = [{'path': 'slow/a.csv'}, {'path': 'fast/b.csv'}]
    job = SimpleNamespace(files=files, max_scouts=8, scouts_started=0, store=RunStore(tmp_path))
    out = asyncio.run(agent_module.scout_many(job, ['slow', 'fast'], ''))
    assert out[0]['folder'] == 'slow' and 'took over 0.2 s' in out[0]['error']
    assert out[1] == {'folder': 'fast', 'files': []}


def test_code_progress_is_shown_while_a_long_reply_runs(tmp_path):
    from app.engine.agent import _log_stream

    store, lines = RunStore(tmp_path), []
    for kind in ('response.code_interpreter_call.in_progress', 'response.code_interpreter_call.completed'):
        _log_stream(SimpleNamespace(type='raw_response_event', data=SimpleNamespace(type=kind)), store, lines.append)
    assert lines == ['  ...   running code in the sandbox']
    events = [
        json.loads(line)['event'] for line in (store.dir / 'events.jsonl').read_text(encoding='utf-8').splitlines()
    ]
    assert events == ['code.started', 'code.finished']


def test_origin_labels_and_public_warning(tmp_path, monkeypatch):
    from sandbox_agent.config import describe

    load_settings, parser = _settings_env(
        tmp_path, monkeypatch, 'OPENAI_API_KEY=k\nOPENAI_BASE_URL=https://api.openai.com/v1\n'
    )
    monkeypatch.setenv('OPENAI_MODEL', 'from-windows')  # a real environment variable beats .env, as in the main app
    (tmp_path / 's.json').write_text('{"model": "  "}', encoding='utf-8')  # blank values count as unset
    settings = load_settings(parser.parse_args([]), tmp_path / 's.json')
    text = describe(settings)
    assert (
        'OPENAI_BASE_URL (.env)' in text and 'from-windows  (from OPENAI_MODEL (Windows environment variable))' in text
    )
    assert 'WARNING:  this is public OpenAI' in text
    proxy = load_settings(parser.parse_args(['--base-url', 'https://proxy.corp/v1']), tmp_path / 's.json')
    assert 'WARNING' not in describe(proxy)


def test_settings_tests_leave_no_environment_behind():
    import os

    assert os.environ.get('OPENAI_BASE_URL') != 'https://proxy.corp/v1'


@pytest.mark.parametrize(
    ('outcomes', 'expected'),
    [
        ({'A1': False, 'B1': True, 'B2': True, 'B3': False, 'B4': True, 'B5': True}, ['as printed text only', 'yes']),
        ({'A1': False, 'B1': True, 'B2': True, 'B3': True, 'B4': None, 'B5': None}, ['by download', 'not tested']),
        ({'A1': False, 'B1': True, 'B2': False, 'B3': None, 'B4': None, 'B5': None}, ['not tested', 'not tested']),
    ],
)
def test_proxy_check_summary_says_what_was_actually_shown(outcomes, expected, capsys):
    import proxy_check

    proxy_check.RESULTS[:] = [(f'{step} x', ok, '') for step, ok in outcomes.items()]
    code = proxy_check.summary()
    out = capsys.readouterr().out
    assert f'files out:       {expected[0]}' in out and f'kept between calls: {expected[1]}' in out
    assert code == (0 if outcomes['B2'] and (outcomes['B3'] or outcomes['B5']) else 1)
    proxy_check.RESULTS.clear()


def test_dropdowns_compare_like_excel_and_template_rows_are_not_the_agents(tmp_path):
    template = tmp_path / 't.xlsx'
    wb = Workbook()
    ws = wb.active
    ws.title = 'Data'
    ws.append(['Code', 'Grade', 'Total'])
    ws.append(['Subtotal', None, '=SUM(C3:C9)'])  # a template row with a validated blank cell
    rule = DataValidation(type='list', formula1='"1,2,3"', allow_blank=False)
    rule.add('B2:B100')
    ws.add_data_validation(rule)
    wb.save(template)
    job = _tiny_job(tmp_path, Deliverable('out.xlsx', template=template))
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    ws.append(['A', 2, 5])  # the number 2 matches the text "2" in the dropdown, as in Excel
    ws.append(['B', None, 6])  # the agent wrote this row and left a required cell blank
    wb.save(outputs / 'out.xlsx')
    _save_map(outputs, {'x.csv': 'used', 'y.csv': 'not_relevant'})
    assert run_checks(outputs, job, inventory(job.zip), proof='none')['issues'] == [
        'Data!B4 is blank; the template requires a value.'
    ]


def test_junk_files_need_no_decision(tmp_path):
    zip_path = tmp_path / 'p.zip'
    with zipfile.ZipFile(zip_path, 'w') as archive:
        archive.writestr('a.csv', 'x')
        archive.writestr('Thumbs.db', 'junk')
    from app.engine.checks import check_map

    assert check_map({'a.csv': {'path': 'a.csv', 'decision': 'used', 'reason': 'r'}}, inventory(zip_path)) == []


def test_duplicated_output_rows_lower_precision(tmp_path):
    from sandbox_agent.score import score_sheet

    wb = Workbook()
    wb.active.title = 'S'
    wb.active.append(['ID', 'Amount'])
    wb.active.append(['A', 1])
    wb.active.append(['A', 1])
    spec = {'key': ['ID'], 'fields': ['Amount'], 'rows': [{'ID': 'A', 'Amount': 1}]}
    assert score_sheet(wb, 'S', spec)['rows_extra'] == 1


def test_broken_formats_and_rules_are_refused_before_a_run():
    from app.engine.rules import check_format_definition, check_rule_definitions

    check_format_definition(JOURNAL)
    for broken, message in [
        ({'sheets': []}, 'at least one sheet'),
        ({'sheets': [{'name': 'J', 'columns': [{'name': 'A', 'type': 'money'}]}]}, 'type must be one of'),
        ({**JOURNAL, 'rules': [{'type': 'balanced', 'sheet': 'Journal', 'debit': 'Dr', 'credit': 'Credit'}]}, 'lacks'),
        ({**JOURNAL, 'rules': [{'type': 'sums_to', 'sheet': 'Journal'}]}, 'unknown type'),
    ]:
        with pytest.raises(ValueError, match=message):
            check_format_definition(broken)
    with pytest.raises(ValueError, match='needs'):
        check_rule_definitions([{'type': 'unique', 'sheet': 'S'}])


class FakeStream:
    """Stands in for one streamed SDK run: a few events, then a result."""

    def __init__(self, final=None, turns=3, tokens=100):
        self.final_output, self.current_turn = final, turns
        self.context_wrapper = SimpleNamespace(
            usage=SimpleNamespace(requests=1, input_tokens=tokens, output_tokens=0, input_tokens_details=None,
                                  output_tokens_details=None))  # fmt: skip
        self.cancelled = None

    async def stream_events(self):
        for _ in range(3):
            yield SimpleNamespace(type='run_item_stream_event', name='other', item=None)

    def cancel(self, mode):
        self.cancelled = mode

    def to_input_list(self):
        return [{'role': 'user', 'content': 'so far'}]


class StopAfter(SimpleNamespace):
    def stop_requested(self):
        return self.stop

    def new_messages(self):
        return []


def _drive_job(tmp_path, **limits):
    return SimpleNamespace(usage=[], scout_usage=[], review_usage=[], turns_used=0, max_turns=60,
                           max_tokens=10_000, **limits)  # fmt: skip


def test_a_cancel_wins_even_when_the_agent_has_just_finished(tmp_path, monkeypatch):
    import app.engine.agent as engine

    done = SimpleNamespace(status='complete')
    monkeypatch.setattr(engine.Runner, 'run_streamed', lambda *a, **k: FakeStream(final=done))
    store = RunStore(tmp_path)
    with pytest.raises(engine.RunStopped) as stopped:
        asyncio.run(engine._drive(None, 'go', _drive_job(tmp_path), store, print, StopAfter(stop='cancelled')))
    assert stopped.value.status == 'cancelled'
    # a pause that arrives as the agent finishes lets the finished result stand
    result = asyncio.run(engine._drive(None, 'go', _drive_job(tmp_path), store, print, StopAfter(stop='paused')))
    assert result.final_output is done


def test_turn_and_token_limits_hold_across_continuations(tmp_path, monkeypatch):
    import app.engine.agent as engine

    seen = []

    def run(*_, max_turns, **__):
        seen.append(max_turns)
        return FakeStream(final=SimpleNamespace(status='complete'), turns=7, tokens=12_000)

    monkeypatch.setattr(engine.Runner, 'run_streamed', run)
    job = _drive_job(tmp_path)
    job.turns_used = 50
    store = RunStore(tmp_path)
    with pytest.raises(engine.LimitReached, match='token limit'):
        asyncio.run(engine._drive(None, 'go', job, store, print, StopAfter(stop=None)))
    assert seen == [10] and job.turns_used == 57  # only the turns left were offered
    job.turns_used = 60
    with pytest.raises(engine.LimitReached, match='60-turn limit'):
        asyncio.run(engine._drive(None, 'go', job, store, print, StopAfter(stop=None)))


class FakeSandbox:
    def __init__(self, files=None):
        self.id, self.files = 'cntr_test', files or {}

    async def upload_file(self, local, name=None):
        return f'/mnt/data/{name or local.name}'

    async def upload_bytes(self, name, data):
        return f'/mnt/data/{name}'

    async def list_files(self):
        return [{'id': p, 'path': p} for p in self.files]

    async def download_id(self, file_id):
        return self.files[file_id]

    async def status(self):
        return 'running'


def _run_with_fakes(tmp_path, monkeypatch, box, drive, review=None, **options):
    import contextlib

    import app.engine.agent as engine

    @contextlib.asynccontextmanager
    async def opened(*_, **__):
        yield box

    monkeypatch.setattr(engine.Sandbox, 'open', opened)
    monkeypatch.setattr(engine, '_drive', drive)
    if review:
        monkeypatch.setattr(engine, 'review', review)
    zip_path = tmp_path / 'p.zip'
    with zipfile.ZipFile(zip_path, 'w') as archive:
        archive.writestr('a.csv', 'x')
    template = tmp_path / 't.xlsx'
    Workbook().save(template)
    spec = JobSpec(
        id='j', module='payroll', goal='g', zip=zip_path, deliverables=(Deliverable('o.xlsx', template=template),)
    )
    return asyncio.run(run_job(spec, Settings(api_key='k'), tmp_path / 'runs', echo=lambda _: None, **options))


def test_a_resumed_run_that_stops_early_keeps_the_saved_outputs(tmp_path, monkeypatch):
    import app.engine.agent as engine

    outputs = tmp_path / 'job-outputs'
    outputs.mkdir()
    (outputs / 'o.xlsx').write_bytes(b'last good workbook')

    async def stopped(*_args, **_kw):
        raise engine.RunStopped('paused', FakeStream())

    store = _run_with_fakes(tmp_path, monkeypatch, FakeSandbox(), stopped, resume='Continue.', outputs=outputs)
    assert store.record['status'] == 'paused'
    assert (outputs / 'o.xlsx').read_bytes() == b'last good workbook'


def test_a_failed_review_never_sinks_a_run_that_passed_its_checks(tmp_path, monkeypatch):
    import app.engine.agent as engine

    async def finished(*_args, **_kw):
        return FakeStream(final=engine.JobSummary(status='complete', summary='done'))

    async def broken_review(*_args, **_kw):
        raise engine.MaxTurnsExceeded('Max turns (20) exceeded')

    monkeypatch.setattr(engine, '_check', lambda job: {'passed': True, 'issues': []})
    store = _run_with_fakes(tmp_path, monkeypatch, FakeSandbox(), finished, broken_review, review_on=True)
    assert store.record['status'] == 'complete' and store.record['error'] is None
    events = (store.dir / 'events.jsonl').read_text(encoding='utf-8')
    assert 'review.failed' in events
