"""Offline tests: no network, no model. They prove the packet, checks, scorer, run folder and harness helpers agree."""

import asyncio
import json
import time
import zipfile
from types import SimpleNamespace

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.cell_range import MultiCellRange
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.formula import ArrayFormula
from sandbox_agent import packets
from sandbox_agent.experiment import summary
from sandbox_agent.score import score, score_questions
from sandbox_agent.selftest import perfect_outputs, selftest

from app.engine.agent import _slug, _tool_error, ask, digest, folder_zip, run_job, sync_outputs
from app.engine.checks import _list_values, describe_template, inventory, parse_rows, run_checks
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
    result = check(out, target)
    text = '\n'.join(result['issues'])
    assert 'Sheet "Lists": template row 3' in '\n'.join(
        result['template_changes']
    )  # reported, for the agent to explain
    for expected in [
        'Entity Summary!B2 held the template formula', 'visibility changed', 'I2', 'must be a number',
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
    result = run_checks(outputs, job, files)
    text = '\n'.join(result['issues'])
    assert "marks it 'not_relevant'" in text and 'rows 2-3' in text  # a row citing a set-aside file has no source
    assert (result['summary']['rows_sourced'], result['summary']['rows_written']) == (0, 2)
    (outputs / 'provenance.json').write_text(
        json.dumps([{'file': 'j.xlsx', 'sheet': 'Journal', 'rows': '2-3', 'sources': ['ghost.csv']}]), encoding='utf-8'
    )
    assert run_checks(outputs, job, files)['summary']['rows_sourced'] == 0  # a file that does not exist is no source
    (outputs / 'provenance.json').unlink()
    assert run_checks(outputs, job, files)['summary']['rows_written'] == 2  # still counted when the sources are missing
    (outputs / 'provenance.json').write_text(
        json.dumps([{'file': 'j.xlsx', 'sheet': 'Journal', 'rows': '2-3', 'sources': ['/mnt/data/packet/x.csv']}]),
        encoding='utf-8',
    )
    result = run_checks(outputs, job, files)
    assert result['passed'] and result['issues'] == []
    assert result['summary'] == {'deliverable_problems': 0, 'files_decided': 2, 'files': 2, 'rows_sourced': 2,
                                 'rows_written': 2}  # fmt: skip


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


def test_console_answers_take_a_choice_number_or_typed_text(monkeypatch, capsys):
    from sandbox_agent.__main__ import _console_ask

    details = {'choices': [{'label': 'November', 'detail': 'Later approval', 'suggested': True}],
               'evidence': [{'value': '1 Nov', 'file': 'finance.txt', 'where': 'line 2'}]}  # fmt: skip
    replies = iter(['1', 'Use October'])
    monkeypatch.setattr('builtins.input', lambda _: next(replies))
    answerer = Answerer(ask=_console_ask)
    assert asyncio.run(answerer.answer('Which month?', details)) == 'November'
    shown = capsys.readouterr().out
    assert '1. November (suggested)' in shown and '1 Nov  (finance.txt, line 2)' in shown
    assert asyncio.run(answerer.answer('Which month?')) == 'Use October'  # no details: typed text as is


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
    job = SimpleNamespace(files=files, max_scouts=8, scouts_started=0, store=RunStore(tmp_path), usage=[],
                          scout_usage=[], review_usage=[], max_tokens=10_000)  # fmt: skip
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
    assert events == ['code.started']  # code.finished comes with the whole item (tests/test_sandbox_engine.py)


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

    assert check_map({'a.csv': {'path': 'a.csv', 'decision': 'used', 'reason': 'r'}}, inventory(zip_path)) == ([], 1)


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
    return SimpleNamespace(usage=[], scout_usage=[], review_usage=[], turns_used=0, max_turns=60, files=[],
                           max_tokens=10_000, tools_running=[], **limits)  # fmt: skip


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


class SlowStream(FakeStream):
    """A turn that is still going (a long script in the sandbox) until it is cancelled."""

    def __init__(self, items=None):
        super().__init__(final=None)
        self.items, self.stopped = items or [], asyncio.Event()
        self.new_items = self.items

    async def stream_events(self):  # nothing streams in while the script runs
        await self.stopped.wait()
        return
        yield

    def cancel(self, mode):
        self.cancelled = mode
        self.stopped.set()

    def to_input_list(self):
        return list(self.items)


class InterruptOnce(SimpleNamespace):
    def stop_requested(self):
        return None

    def interrupt_requested(self):
        return bool(self.waiting)

    def new_messages(self):
        taken, self.waiting = self.waiting, []
        return taken


def test_send_now_stops_the_turn_where_it_is_and_hands_over_the_message(tmp_path, monkeypatch):
    import app.engine.agent as engine

    monkeypatch.setattr(engine, 'INTERRUPT_POLL_SECONDS', 0.01)
    done = SimpleNamespace(status='complete')
    whole = [{'role': 'user', 'content': 'go'}, {'type': 'reasoning', 'id': 'rs_1'},
             {'type': 'function_call', 'call_id': 'c1', 'name': 'save_outputs'}]  # fmt: skip
    streams = [SlowStream(whole), FakeStream(final=done)]
    prompts = []

    def run(agent, prompt, **_):
        prompts.append(prompt)
        return streams.pop(0)

    monkeypatch.setattr(engine.Runner, 'run_streamed', run)
    (tmp_path / 'rates.csv').write_bytes(b'MYR,4.4')
    uploads = []

    class Box:
        async def upload_file(self, local, name=None):
            uploads.append(local.read_bytes())
            return f'/mnt/data/{name}'

    store = RunStore(tmp_path)
    job = _drive_job(tmp_path, main=Box(), added=[], store=store)
    message = {'text': 'Use November rates.', 'images': [],
               'files': [{'packet': 'Added/rates.csv', 'local': tmp_path / 'rates.csv'}]}  # fmt: skip
    control = InterruptOnce(waiting=[message])
    result = asyncio.run(engine._drive(None, 'go', job, store, print, control))
    assert result.final_output is done and prompts[0] == 'go'
    # The turn was cut short: the tool call that never got its result goes, with the reasoning before it.
    assert prompts[1][:-1] == [{'role': 'user', 'content': 'go'}]
    sent = prompts[1][-1]['content']
    assert sent.startswith(engine_control_interrupted()) and 'Use November rates.' in sent
    assert '/mnt/data/added__rates.csv (packet path Added/rates.csv)' in sent and uploads == [b'MYR,4.4']
    assert job.files[-1]['path'] == 'Added/rates.csv' and job.added[0]['packet'] == 'Added/rates.csv'
    lines = [json.loads(line) for line in (store.dir / 'events.jsonl').read_text().splitlines()]
    delivered = [e for e in lines if e['event'] == 'instructions.delivered']
    assert delivered and delivered[0]['interrupted'] is True


def test_a_file_already_in_the_runs_packet_is_not_added_again(tmp_path):
    import app.engine.agent as engine

    (tmp_path / 'rates.csv').write_bytes(b'MYR,4.4')
    job = _drive_job(tmp_path, main=None, added=[], store=RunStore(tmp_path))
    job.files.append({'path': 'Added/rates.csv'})  # sent while the run was queued, so the packet holds it
    message = {'text': 'x', 'images': [], 'files': [{'packet': 'Added/rates.csv', 'local': tmp_path / 'rates.csv'}]}
    assert asyncio.run(engine._place_files(job, [message])) == [] and len(job.files) == 1 and job.added == []


def test_send_now_waits_for_the_agents_own_tool_to_finish(tmp_path, monkeypatch):
    import app.engine.agent as engine

    monkeypatch.setattr(engine, 'INTERRUPT_POLL_SECONDS', 0.01)
    job = _drive_job(tmp_path)
    cancels, fired = [], []
    result = SimpleNamespace(cancel=lambda mode: cancels.append(mode))
    control = InterruptOnce(waiting=['Stop and use November rates.'])
    hook = SimpleNamespace(context=job)

    async def go():
        await engine.TOOL_WATCH.on_tool_start(hook, None, SimpleNamespace(name='save_outputs'))
        watcher = asyncio.create_task(engine._watch_for_interrupt(result, job, control, fired))
        await asyncio.sleep(0.05)
        assert cancels == [] and fired == []  # a save is never cut off part way
        await engine.TOOL_WATCH.on_tool_end(hook, None, SimpleNamespace(name='save_outputs'), '{}')
        await asyncio.sleep(0.05)
        assert cancels == ['immediate'] and fired == [True]  # then the message goes in at once
        await watcher
        # A tool whose end was never reported cannot hold Send now back past the next model call.
        await engine.TOOL_WATCH.on_tool_start(hook, None, SimpleNamespace(name='scout_folders'))
        await engine.TOOL_WATCH.on_llm_start(hook, None, None, [])
        assert job.tools_running == []

    asyncio.run(go())


def test_the_agent_is_told_what_the_stopped_script_was_doing():
    from app.engine.control import INTERRUPTED, instructions

    sent = instructions(['Use November rates.'], interrupted=True, running=['Fill the October sheet'])
    assert sent.startswith(INTERRUPTED) and 'It was running a script that: Fill the October sheet' in sent
    assert 'in your summary' in sent
    assert not instructions(['Use November rates.']).startswith(INTERRUPTED)


def test_a_scout_of_a_folder_reads_a_file_added_during_the_run(tmp_path):
    import io as _io
    import zipfile as _zipfile

    import app.engine.agent as engine

    packet = tmp_path / 'packet.zip'
    with _zipfile.ZipFile(packet, 'w') as z:
        z.writestr('HR/a.csv', 'x')
    (tmp_path / 'rates.csv').write_bytes(b'MYR,4.4')
    files = [{'path': 'HR/a.csv', 'member': 'HR/a.csv'},
             {'path': 'Added/rates.csv', 'member': '', 'local': str(tmp_path / 'rates.csv')}]  # fmt: skip
    data, count = engine.folder_zip(packet, files, 'Added')
    with _zipfile.ZipFile(_io.BytesIO(data)) as z:
        assert count == 1 and z.read('Added/rates.csv') == b'MYR,4.4'


def engine_control_interrupted():
    from app.engine.control import INTERRUPTED

    return INTERRUPTED


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


def test_a_packet_over_the_upload_limit_is_split_into_whole_file_parts(tmp_path):
    import os

    from app.engine.agent import split_zip

    path = tmp_path / 'packet.zip'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as archive:
        for n in range(6):
            archive.writestr(f'f{n}.bin', os.urandom(40_000))
    parts = split_zip(path, limit=100_000)
    assert len(parts) == 3 and all(p.stat().st_size <= 110_000 for p in parts)
    names = [n for p in parts for n in zipfile.ZipFile(p).namelist()]
    assert sorted(names) == [f'f{n}.bin' for n in range(6)]
    assert split_zip(path, limit=10**9) == [path]
    with pytest.raises(ValueError, match='cannot be uploaded'):
        split_zip(path, limit=1_000)


def test_a_dropped_reply_is_retried_from_the_finished_items(tmp_path, monkeypatch):
    from openai import APIError

    import app.engine.agent as engine

    class Dropping(FakeStream):
        new_items = ['one finished item']

        async def stream_events(self):
            yield SimpleNamespace(type='run_item_stream_event', name='other', item=None)
            raise APIError('There was an issue with your request.', None, body=None)

    streams = [Dropping(), Dropping(), FakeStream(final=SimpleNamespace(status='complete'))]
    prompts, waits = [], []

    def run(_agent, prompt, **_kw):
        prompts.append(prompt)
        return streams.pop(0)

    real_sleep = asyncio.sleep

    async def no_sleep(seconds):
        if seconds == engine.INTERRUPT_POLL_SECONDS:  # the Send now watcher looking in, not a pause before a retry
            return await real_sleep(0)
        waits.append(seconds)

    monkeypatch.setattr(engine.Runner, 'run_streamed', run)
    monkeypatch.setattr(engine.asyncio, 'sleep', no_sleep)
    store = RunStore(tmp_path)
    result = asyncio.run(engine._drive(None, 'go', _drive_job(tmp_path), store, print, StopAfter(stop=None)))
    assert result.final_output.status == 'complete' and prompts[1] == [{'role': 'user', 'content': 'so far'}]
    assert (store.dir / 'events.jsonl').read_text(encoding='utf-8').count('model.retry') == 2
    assert waits == [2, 10]  # a longer pause each time, so a busy service is given room
    streams[:] = [Dropping()] * 4
    with pytest.raises(APIError):  # three retries, then the error stands
        asyncio.run(engine._drive(None, 'go', _drive_job(tmp_path), store, print, StopAfter(stop=None)))
    assert waits[2:] == [2, 10, 30]

    class Refused(Dropping):
        async def stream_events(self):
            error = APIError('Incorrect API key.', None, body=None)
            error.status_code = 401
            raise error
            yield

    streams[:] = [Refused()]
    with pytest.raises(APIError):  # a refused request is not tried again
        asyncio.run(engine._drive(None, 'go', _drive_job(tmp_path), store, print, StopAfter(stop=None)))
    assert len(waits) == 5

    class Paused(StopAfter):
        def stop_requested(self):
            return 'paused'

    streams[:] = [Dropping(), Dropping()]
    with pytest.raises(engine.RunStopped):  # a pause asked for during the wait is honoured before another request
        asyncio.run(engine._drive(None, 'go', _drive_job(tmp_path), store, print, Paused(stop=None)))
    assert len(streams) == 1


def test_a_run_the_ai_service_fails_keeps_its_saved_work_and_can_continue(tmp_path, monkeypatch):
    from openai import APIError

    def failing(status=None):
        async def drive(*_args, **_kw):
            error = APIError('The server is overloaded.', None, body=None)
            error.status_code = status
            raise error

        return drive

    outputs = tmp_path / 'job-outputs'
    outputs.mkdir()
    (outputs / 'o.xlsx').write_bytes(b'last good workbook')
    store = _run_with_fakes(tmp_path, monkeypatch, FakeSandbox(), failing(529), resume='Continue.', outputs=outputs)
    assert store.record['status'] == 'stopped' and 'The AI service stopped answering' in store.record['error']
    assert (outputs / 'o.xlsx').read_bytes() == b'last good workbook'
    assert 'model.unavailable' in (store.dir / 'events.jsonl').read_text(encoding='utf-8')
    # A wrong key or address is a set-up fault the user must fix: that run fails, with the reason.
    (tmp_path / 'again').mkdir()
    refused = _run_with_fakes(tmp_path / 'again', monkeypatch, FakeSandbox(), failing(401))
    assert refused.record['status'] == 'failed' and 'APIError' in refused.record['error']
    assert (store.record['cut_short'], refused.record['cut_short']) == ('service', None)

    async def unreachable(*_args, **_kw):
        import httpx
        from openai import APIConnectionError

        raise APIConnectionError(request=httpx.Request('POST', 'http://wrong.example/v1'))

    # So is an address nothing answers at: the service was never reached once, so there is nothing to continue.
    (tmp_path / 'third').mkdir()
    lost = _run_with_fakes(tmp_path / 'third', monkeypatch, FakeSandbox(), unreachable)
    assert lost.record['status'] == 'failed' and 'APIConnectionError' in lost.record['error']


def test_number_rules_in_the_template_are_enforced_as_excel_would(tmp_path):
    template = tmp_path / 't.xlsx'
    wb = Workbook()
    ws = wb.active
    ws.title = 'Pay'
    ws.append(['Amount', 'Month'])
    amounts = DataValidation(type='decimal', operator='greaterThanOrEqual', formula1='0')
    amounts.add('A2:A100')
    months = DataValidation(type='whole', operator='between', formula1='1', formula2='12')
    months.add('B2:B100')
    for rule in (amounts, months):
        ws.add_data_validation(rule)
    wb.save(template)
    job = _tiny_job(tmp_path, Deliverable('out.xlsx', template=template))
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    for row in ([-500, 1.5], [10, 13], [0, 12]):
        ws.append(row)
    wb.save(outputs / 'out.xlsx')
    _save_map(outputs, {'x.csv': 'used', 'y.csv': 'not_relevant'})
    assert run_checks(outputs, job, inventory(job.zip), proof='none')['issues'] == [
        'Pay!A2 = -500 must be 0 or more.',
        'Pay!B2 = 1.5 must be a whole number.',
        'Pay!B3 = 13 must be between 1 and 12.',
    ]


def test_an_instruction_taken_just_before_a_dropped_reply_still_reaches_the_agent(tmp_path, monkeypatch):
    from openai import APIError

    import app.engine.agent as engine

    class Dropping(FakeStream):
        new_items = ['one finished item']

        async def stream_events(self):
            yield SimpleNamespace(type='run_item_stream_event', name='other', item=None)
            yield SimpleNamespace(type='run_item_stream_event', name='other', item=None)
            raise APIError('There was an issue with your request.', None, body=None)

    class Told(StopAfter):
        def new_messages(self):
            return self.queue.pop(0) if self.queue else []

    streams = [Dropping(), FakeStream(final=SimpleNamespace(status='complete'))]
    prompts = []

    def run(_agent, prompt, **_kw):
        prompts.append(prompt)
        return streams.pop(0)

    monkeypatch.setattr(engine.Runner, 'run_streamed', run)
    monkeypatch.setattr(engine, 'RETRY_WAITS', (0,))
    control = Told(stop=None, queue=[['use the October rates']])
    result = asyncio.run(engine._drive(None, 'go', _drive_job(tmp_path), RunStore(tmp_path), print, control))
    assert result.final_output.status == 'complete'
    assert prompts[1][0] == {'role': 'user', 'content': 'so far'}
    assert 'use the October rates' in prompts[1][-1]['content']


def test_no_scout_starts_once_the_token_limit_is_used_up(tmp_path):
    import app.engine.agent as agent_module

    live = SimpleNamespace(requests=1, input_tokens=9_000, output_tokens=1_001, input_tokens_details=None,
                           output_tokens_details=None)  # fmt: skip
    job = SimpleNamespace(files=[{'path': 'a/b.csv'}], max_scouts=8, scouts_started=0, store=RunStore(tmp_path),
                          usage=[], scout_usage=[], review_usage=[], max_tokens=10_000)  # fmt: skip
    out = asyncio.run(agent_module.scout_many(job, ['a'], '', live))
    assert 'token limit' in out[0]['error'] and job.scouts_started == 0


def _template_with_rules(tmp_path):
    template = tmp_path / 't.xlsx'
    wb = Workbook()
    ws = wb.active
    ws.title = 'Pay'
    ws.append(['Code', 'Amount'])
    codes = DataValidation(type='list', formula1='"A,B"', allow_blank=True)
    codes.add('A2:A100')
    amounts = DataValidation(type='decimal', operator='greaterThanOrEqual', formula1='0')
    amounts.add('B2:B100')
    for rule in (codes, amounts):
        ws.add_data_validation(rule)
    return template, wb


def test_a_formula_the_template_keeps_in_a_number_only_cell_is_not_called_a_bad_number(tmp_path):
    template, wb = _template_with_rules(tmp_path)
    wb.active['B2'] = '=SUM(B3:B100)'
    wb.active['B3'] = ArrayFormula('B3', '=SUM(B4:B100*1)')
    wb.save(template)
    job = _tiny_job(tmp_path, Deliverable('out.xlsx', template=template))
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    wb.active['A4'], wb.active['B4'] = 'A', 12.5
    wb.save(outputs / 'out.xlsx')
    _save_map(outputs, {'x.csv': 'used', 'y.csv': 'not_relevant'})
    assert run_checks(outputs, job, inventory(job.zip), proof='none')['issues'] == []


def test_an_output_that_drops_or_changes_a_template_dropdown_is_refused(tmp_path):
    template, wb = _template_with_rules(tmp_path)
    wb.save(template)
    job = _tiny_job(tmp_path, Deliverable('out.xlsx', template=template))
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    ws = wb.active
    ws.append(['A', 3])
    ws.data_validations.dataValidation = [r for r in ws.data_validations.dataValidation if r.type != 'list']
    wb.save(outputs / 'out.xlsx')
    _save_map(outputs, {'x.csv': 'used', 'y.csv': 'not_relevant'})
    assert run_checks(outputs, job, inventory(job.zip), proof='none')['issues'] == [
        'Sheet "Pay": 1 of the template\'s entry rules do not cover their cells, e.g. A2:A100 (dropdown). Keep the '
        "template's dropdowns and number rules, and stretch them over any rows you insert among them (openpyxl's "
        'insert_rows does not move or stretch them).'
    ]


def test_a_far_away_formatted_cell_does_not_slow_the_checks(tmp_path):
    from openpyxl.styles import Font

    template, wb = _template_with_rules(tmp_path)
    wb.active.cell(1_048_576, 300).font = Font(bold=True)  # formatting only, a million rows down
    wb.save(template)
    job = _tiny_job(tmp_path, Deliverable('out.xlsx', template=template))
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    wb.active['A2'], wb.active['B2'] = 'C', 4
    wb.save(outputs / 'out.xlsx')
    _save_map(outputs, {'x.csv': 'used', 'y.csv': 'not_relevant'})
    started = time.monotonic()
    assert describe_template(template).startswith('Sheet "Pay": content in rows 1\n')
    (outputs / 'provenance.json').write_text(
        json.dumps([{'file': 'out.xlsx', 'sheet': 'Pay', 'rows': '2-9999999999', 'sources': ['x.csv']}])
    )
    issues = run_checks(outputs, job, inventory(job.zip))['issues']
    assert issues == ["Pay!A2 = 'C' is not in its dropdown ['A', 'B']."]
    assert time.monotonic() - started < 5


def test_row_ranges_are_clipped_before_they_are_expanded():
    assert parse_rows('2-9999999999, 7', 5) == {2, 3, 4, 5}
    assert parse_rows([1, '3-4', 0, 99], 10) == {1, 3, 4}
    for bad in ('7-', '-5'):
        with pytest.raises(ValueError):
            parse_rows(bad, 10)


def test_a_new_formula_or_an_empty_required_column_does_not_slip_past_the_template_rules(tmp_path):
    template = tmp_path / 't.xlsx'
    wb = Workbook()
    ws = wb.active
    ws.title = 'Staff'
    ws.append(['Employee'])
    status = DataValidation(type='list', formula1='"Approved"', allow_blank=False)
    status.add('B2:B100')
    ws.add_data_validation(status)
    wb.save(template)
    job = _tiny_job(tmp_path, Deliverable('out.xlsx', template=template))
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    ws.append(['Alice'])  # B2, required, left blank and past the last column that holds anything
    ws.append(['Bob', '="Rejected"'])
    wb.save(outputs / 'out.xlsx')
    _save_map(outputs, {'x.csv': 'used', 'y.csv': 'not_relevant'})
    issues = run_checks(outputs, job, inventory(job.zip), proof='none')['issues']
    assert issues[0] == 'Staff!B2 is blank; the template requires a value.'
    assert issues[1].startswith('Staff!B3 = \'="Rejected"\' is a formula') and len(issues) == 2


def test_a_dropdown_saved_as_split_ranges_still_counts_as_kept():
    from openpyxl.worksheet.cell_range import CellRange

    from app.engine.checks import _covered

    whole = CellRange('A2:B100')
    assert _covered(whole, [CellRange('A2:B50'), CellRange('A51:A100'), CellRange('B51:B200')])
    assert not _covered(whole, [CellRange('A2:B50'), CellRange('A51:A100')])
    assert not _covered(whole, [])


def test_a_dropdown_that_names_a_whole_column_reads_only_the_cells_that_hold_a_value(tmp_path):
    template = tmp_path / 't.xlsx'
    wb = Workbook()
    ws = wb.active
    ws.title = 'Data'
    ws.append(['Code'])
    lists = wb.create_sheet('Lists')
    for code in ('A', 'B'):
        lists.append([code])
    rule = DataValidation(type='list', formula1='Lists!$A$1:$A$1048576', allow_blank=True)
    rule.add('A2:A1048576')
    ws.add_data_validation(rule)
    wb.save(template)
    job = _tiny_job(tmp_path, Deliverable('out.xlsx', template=template))
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    ws.append(['B'])
    ws.append(['C'])
    wb.save(outputs / 'out.xlsx')
    _save_map(outputs, {'x.csv': 'used', 'y.csv': 'not_relevant'})
    issues = run_checks(outputs, job, inventory(job.zip), proof='none')['issues']
    assert issues == ["Data!A3 = 'C' is not in its dropdown ['A', 'B']."]
    opened = load_workbook(template)
    for whole in ('Lists!$A$1:$A$1048576', 'Lists!$A:$A'):
        assert _list_values(whole, opened) == ['A', 'B']
    assert len(opened['Lists']._cells) == 2  # no empty cells were made along the way


def _claims_template(tmp_path, stray=False, spacer=False):
    """A title, a note, a three-row table with rules, a total and a closing note: a template a person fills by
    inserting rows inside the table when it is too short."""
    template = tmp_path / 't.xlsx'
    wb = Workbook()
    ws = wb.active
    ws.title = 'Pay'
    ws['A1'], ws['A2'] = 'Expense claims', 'Fill one row per claim.'
    ws['A4'], ws['B4'] = 'Code', 'Amount'
    total = 9 if spacer else 8  # a blank spacer row may sit between the table and its total
    ws[f'A{total}'], ws[f'B{total}'] = 'Total', '=SUM(B5:B7)'
    ws[f'A{total + 2}'] = 'Approved by finance'
    if stray:
        ws['D12'] = 'old draft value'
    codes = DataValidation(type='list', formula1='"A,B"', allow_blank=True)
    codes.add('A5:A7')
    amounts = DataValidation(type='decimal', operator='greaterThanOrEqual', formula1='0', allow_blank=False)
    amounts.add('B5:B7')
    for rule in (codes, amounts):
        ws.add_data_validation(rule)
    wb.save(template)
    job = _tiny_job(tmp_path, Deliverable('out.xlsx', template=template))
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    _save_map(outputs, {'x.csv': 'used', 'y.csv': 'not_relevant'})
    return job, outputs, wb


def _fill_five_claims(ws, stretch=True, codes=('A', 'B', 'A', 'B', 'A')):
    ws.insert_rows(8, 2)  # openpyxl moves the cells, not the rules or the formula's range
    for offset, code in enumerate(codes):
        ws.cell(5 + offset, 1, code)
        ws.cell(5 + offset, 2, 10 * (offset + 1))
    ws['B10'] = '=SUM(B5:B9)'
    if stretch:
        for rule in ws.data_validations.dataValidation:
            column = 'A' if rule.type == 'list' else 'B'
            rule.sqref = MultiCellRange(f'{column}5:{column}9')


def test_rows_inserted_inside_a_table_pass_and_the_moved_template_rows_are_reported(tmp_path):
    job, outputs, wb = _claims_template(tmp_path)
    _fill_five_claims(wb.active)
    wb.save(outputs / 'out.xlsx')
    result = run_checks(outputs, job, inventory(job.zip), proof='none')
    assert result['issues'] == []
    assert result['template_changes'] == ['out.xlsx: Sheet "Pay": template rows 8-10 moved 2 rows down (now 10-12).']
    assert result['empty_rows'] == []  # a filled table leaves no gaps behind


def _side_note_template(tmp_path):
    """A table with headings in row 4, a source note beside the table in J5, and a second table lower down."""
    wb = Workbook()
    ws = wb.active
    ws.title = 'Pay'
    for column, heading in enumerate(['Company', 'ID', 'Name', 'Amount'], 1):
        ws.cell(4, column, heading)
        ws.cell(12, column, heading)  # the second table's headings
    ws['J5'] = 'source from the monthly file'
    template = tmp_path / 'template.xlsx'
    wb.save(template)
    return template, load_workbook(template)


def test_rows_left_empty_among_the_records_are_reported_with_what_they_hold(tmp_path):
    from app.engine.checks import check_template

    template, out = _side_note_template(tmp_path)
    ws = out.active
    for row in (6, 7, 9, 13, 14):  # row 5 skipped for its side note; row 8 left blank; 10-11 part the two tables
        for column in range(1, 5):
            ws.cell(row, column, f'v{row}{column}')
    ws['J7'] = 'checked'  # a remark beside one record does not make column J part of the table
    assert check_template(out, template).gaps == [
        'Sheet "Pay": Row 5 sits between row 4 and your record at row 6, with nothing in the columns your records '
        'fill (A-D); it holds only J: "source from the monthly file".',
        'Sheet "Pay": Row 8 sits between row 7 and your record at row 9, with nothing in the columns your records '
        'fill (A-D); otherwise blank.',
    ]  # the spacer rows 10-11 before the second table's headings are not gaps
    for row in (5, 8):
        for column in range(1, 5):
            ws.cell(row, column, 'x')
    assert check_template(out, template).gaps == []


def test_single_filled_fields_around_a_table_are_not_records(tmp_path):
    from app.engine.checks import check_template

    template, out = _side_note_template(tmp_path)
    ws = out.active
    for row in (5, 6, 7):
        for column in range(1, 5):
            ws.cell(row, column, f'v{row}{column}')
    ws['B1'] = 'Acme'  # a "Company:" field above the table
    ws['B20'] = 'Approved by Finance'  # a sign-off line far below it
    assert check_template(out, template).gaps == []


def test_the_template_outline_shows_which_columns_each_run_of_rows_uses(tmp_path):
    template, _ = _side_note_template(tmp_path)
    assert describe_template(template) == (
        'Sheet "Pay": content in rows 4-5, 12\n  row 4: A-D\n  row 5: J\n  row 12: A-D'
    )
    wb = load_workbook(template)
    wb.active['A5'] = 'example'  # a note on the same row as table content still stands apart
    wb.save(template)
    assert '  row 5: A, J' in describe_template(template)


def test_rules_left_behind_by_inserted_rows_are_reported_and_still_apply_to_the_new_rows(tmp_path):
    job, outputs, wb = _claims_template(tmp_path)
    _fill_five_claims(wb.active, stretch=False, codes=('A', 'B', 'A', 'B', 'C'))
    wb.save(outputs / 'out.xlsx')
    issues = run_checks(outputs, job, inventory(job.zip), proof='none')['issues']
    assert issues[0].startswith('Sheet "Pay": 2 of the template\'s entry rules do not cover their cells, e.g. A5:A9')
    assert issues[1:] == ["Pay!A9 = 'C' is not in its dropdown ['A', 'B']."]


def test_a_stray_template_value_may_be_cleared_when_the_agent_says_so(tmp_path):
    job, outputs, wb = _claims_template(tmp_path, stray=True)
    ws = wb.active
    ws['A5'], ws['B5'] = 'A', 10
    ws['D12'] = None
    wb.save(outputs / 'out.xlsx')
    result = run_checks(outputs, job, inventory(job.zip), proof='none')
    assert result['issues'] == []
    assert result['template_changes'] == [
        'out.xlsx: Sheet "Pay": template row 12 (\'old draft value\') is no longer in the output as it was.'
    ]


def test_a_template_formula_replaced_by_a_value_is_refused(tmp_path):
    job, outputs, wb = _claims_template(tmp_path)
    ws = wb.active
    ws['A5'], ws['B5'], ws['B8'] = 'A', 10, 10
    wb.save(outputs / 'out.xlsx')
    issues = run_checks(outputs, job, inventory(job.zip), proof='none')['issues']
    assert issues == [
        "Pay!B8 held the template formula '=SUM(B5:B7)' and is now 10. Keep the template's formulas; adjust their "
        'ranges if you insert rows.'
    ]


def test_rules_must_stretch_over_inserted_rows_even_with_a_blank_row_before_the_total(tmp_path):
    job, outputs, wb = _claims_template(tmp_path, spacer=True)
    _fill_five_claims(wb.active, stretch=False, codes=('A', 'B', 'A', 'B', 'C'))
    wb.save(outputs / 'out.xlsx')
    issues = run_checks(outputs, job, inventory(job.zip), proof='none')['issues']
    assert issues[0].startswith('Sheet "Pay": 2 of the template\'s entry rules do not cover their cells, e.g. A5:A9')
    assert "Pay!A9 = 'C' is not in its dropdown ['A', 'B']." in issues


def test_a_moved_total_whose_label_was_edited_keeps_its_formula(tmp_path):
    job, outputs, wb = _claims_template(tmp_path)
    _fill_five_claims(wb.active)
    wb.active['A10'] = 'Grand total'
    wb.save(outputs / 'out.xlsx')
    result = run_checks(outputs, job, inventory(job.zip), proof='none')
    assert result['issues'] == []
    assert any('template row 8' in change for change in result['template_changes'])


def test_rows_inserted_below_an_edited_row_are_not_pulled_into_the_table_above(tmp_path):
    job, outputs, wb = _claims_template(tmp_path)
    ws = wb.active
    ws['A5'], ws['B5'] = 'A', 10
    ws['A8'] = 'Grand total'  # the row below the table changes, so it no longer anchors the table's end
    ws.insert_rows(9, 2)
    ws['A9'], ws['A10'] = 'Travel', 'Hotel'  # notes the agent added below the total, outside the table
    wb.save(outputs / 'out.xlsx')
    issues = run_checks(outputs, job, inventory(job.zip), proof='none')['issues']
    assert issues == []


def test_a_hardcoded_total_is_refused_even_when_another_formula_sits_nearby(tmp_path):
    job, outputs, wb = _claims_template(tmp_path)
    ws = wb.active
    _fill_five_claims(ws)
    ws['A10'], ws['B10'] = 'Grand total', 150
    ws['A11'], ws['B11'] = 'GST', '=B10*0.07'
    wb.save(outputs / 'out.xlsx')
    issues = run_checks(outputs, job, inventory(job.zip), proof='none')['issues']
    assert any("held the template formula '=SUM(B5:B7)'" in issue for issue in issues)


def test_job_rules_skip_the_template_rows_but_not_the_rows_the_agent_wrote(tmp_path):
    job, outputs, wb = _claims_template(tmp_path)
    job = JobSpec(**{**job.__dict__, 'rules': ({'type': 'required', 'sheet': 'Pay', 'header_row': 4,
                                                 'columns': ['Amount']},)})  # fmt: skip
    _fill_five_claims(wb.active)
    wb.active['B6'] = None  # a claim the agent wrote without its amount
    wb.save(outputs / 'out.xlsx')
    issues = run_checks(outputs, job, inventory(job.zip), proof='none')['issues']
    # The title rows above the header are not checked either way; the total and closing note are template rows.
    assert issues == [
        'Pay!B6 is blank; the template requires a value.',
        'out.xlsx: Pay!Amount row 6 is blank; it is required.',
    ]


def test_provenance_covers_inserted_rows_not_moved_template_rows(tmp_path):
    from app.engine.checks import check_deliverable

    job, outputs, wb = _claims_template(tmp_path)
    _fill_five_claims(wb.active)
    wb.save(outputs / 'out.xlsx')
    assert check_deliverable(outputs, job.deliverables[0], ()).written == {'Pay': {5, 6, 7, 8, 9}}


def _unlabelled_total(tmp_path):
    """A table whose total row holds only a formula, with no label beside it."""
    job, outputs, wb = _claims_template(tmp_path)
    ws = wb.active
    ws['A8'] = None
    wb.save(job.deliverables[0].template)
    return job, outputs, wb


def test_an_unlabelled_total_is_not_mistaken_for_a_record(tmp_path):
    job, outputs, wb = _unlabelled_total(tmp_path)
    ws = wb.active
    for row, (code, amount) in enumerate([('A', 10), ('B', 20), ('A', 30)], start=5):
        ws.cell(row, 1, code), ws.cell(row, 2, amount)
    wb.save(outputs / 'out.xlsx')
    result = run_checks(outputs, job, inventory(job.zip), proof='none')
    assert result['issues'] == [] and result['template_changes'] == []


def test_a_cleared_template_formula_is_refused_like_a_replaced_one(tmp_path):
    job, outputs, wb = _claims_template(tmp_path)
    ws = wb.active
    ws['A5'], ws['B5'], ws['B8'] = 'A', 10, None
    wb.save(outputs / 'out.xlsx')
    issues = run_checks(outputs, job, inventory(job.zip), proof='none')['issues']
    assert issues == [
        "Pay!B8 held the template formula '=SUM(B5:B7)' and is now None. Keep the template's formulas; adjust their "
        'ranges if you insert rows.'
    ]


def test_removing_every_empty_table_row_does_not_break_the_checks(tmp_path):
    job, outputs, wb = _claims_template(tmp_path)
    ws = wb.active
    ws.delete_rows(5, 3)  # nothing to record, so the agent drops the blank table rows
    ws['B5'] = '=SUM(B5:B4)'
    ws.data_validations.dataValidation = []
    wb.save(outputs / 'out.xlsx')
    result = run_checks(outputs, job, inventory(job.zip), proof='none')
    assert result['issues'] == []
    assert result['template_changes'] == ['out.xlsx: Sheet "Pay": template rows 8-10 moved 3 rows up (now 5-7).']


def test_a_job_rule_finds_its_header_after_rows_are_inserted_above_it(tmp_path):
    job, outputs, wb = _claims_template(tmp_path)
    job = JobSpec(**{**job.__dict__, 'rules': ({'type': 'required', 'sheet': 'Pay', 'header_row': 4,
                                                 'columns': ['Amount']},)})  # fmt: skip
    ws = wb.active
    ws.insert_rows(3, 2)  # room for a longer note above the table
    ws['A3'] = 'Claims must be approved before they are listed.'
    for rule in ws.data_validations.dataValidation:
        column = 'A' if rule.type == 'list' else 'B'
        rule.sqref = MultiCellRange(f'{column}7:{column}9')
    ws['A7'], ws['B7'], ws['B10'] = 'A', 10, '=SUM(B7:B9)'
    wb.save(outputs / 'out.xlsx')
    assert run_checks(outputs, job, inventory(job.zip), proof='none')['issues'] == []


def test_loop_packets_run_through_the_app_with_comments_and_a_remembered_rule(tmp_path, monkeypatch):
    """The comment and remember packets, with the engine run replaced by one that writes the expected output."""
    from types import SimpleNamespace

    from sandbox_agent.loop import loop_run
    from sandbox_agent.selftest import perfect_outputs

    from app.engine import service

    packets.build('payroll_comment_fix', tmp_path / 'p')
    packets.build('payroll_remember', tmp_path / 'p')
    seen = []

    async def fake_run_job(spec, endpoint, runs_root, *, run_id, outputs, control, resume, **options):
        key = json.loads((tmp_path / 'p' / current['packet'] / 'answer_key.json').read_text(encoding='utf-8'))
        perfect_outputs(spec, key, outputs)
        seen.append(SimpleNamespace(resume=resume, context=spec.context, report=control.after_save()))
        (runs_root / run_id).mkdir(parents=True)
        record = {'status': 'complete', 'final_check': {'passed': True, 'issues': []}, 'usage': {'lead': [{'requests': 2, 'input_tokens': 100, 'output_tokens': 10}]},
                  'summary': {'status': 'complete', 'summary': 'Done.', 'open_questions': []}}  # fmt: skip
        (runs_root / run_id / 'run.json').write_text(json.dumps(record), encoding='utf-8')
        return SimpleNamespace(record=record)

    monkeypatch.setattr(service, 'run_job', fake_run_job)
    settings = Settings(api_key='unused')
    current = {'packet': 'payroll_comment_fix'}
    row = asyncio.run(
        loop_run(tmp_path / 'p' / 'payroll_comment_fix', 'A', settings, tmp_path / 'runs', echo=lambda _: None)
    )
    assert (row['status'], row['cell_accuracy'], row['row_precision']) == ('complete', 1.0, 1.0)
    assert (row['comments'], row['comments_changed'], row['changed_elsewhere'], row['round_status']) == (
        1,
        1,
        0,
        'done',
    )
    assert row['proved'] is True
    assert 'This salary does not match the joiners list' in seen[0].resume
    assert len(seen[0].report['comment_changes']['changed_where_asked']) == 1

    current['packet'] = 'payroll_remember'
    row = asyncio.run(
        loop_run(tmp_path / 'p' / 'payroll_remember', 'A', settings, tmp_path / 'runs', echo=lambda _: None)
    )
    assert (row['status'], row['cell_accuracy'], row['remembered_version'], row['playbook_version']) == (
        'complete',
        1.0,
        2,
        2,
    )
    assert row['requests'] == 4  # both runs are counted
    assert row['proved'] is True and row['first_run']['status'] == 'complete'
    fresh = seen[-1]
    assert fresh.resume is None and fresh.context.endswith(
        'Write every name as SURNAME, First name, for example ' + fresh.context.rsplit('example ', 1)[1]
    )
    assert 'Write every name as SURNAME, First name' in fresh.context and fresh.report == {}


def test_a_packet_with_skills_can_be_loaded_with_or_without_them(tmp_path):
    packets.build('payroll_skill_rule', tmp_path)
    with_skills = load_job(tmp_path / 'payroll_skill_rule')
    assert [s['name'] for s in with_skills.skills] == ['first-month-pay'] and len(with_skills.skills[0]['sha256']) == 64
    assert 'first month of pay' in with_skills.skills[0]['description']
    assert (with_skills.skills_dir / 'first-month-pay' / 'reference' / 'cost_centres.csv').is_file()
    assert load_job(tmp_path / 'payroll_skill_rule', skills=False).skills == ()


def test_a_loop_run_that_does_nothing_or_gets_the_style_wrong_is_not_counted_as_proof(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sandbox_agent.loop import exact_text, loop_run
    from sandbox_agent.selftest import perfect_outputs

    from app.engine import service

    packets.build('payroll_comment_fix', tmp_path / 'p')
    packets.build('payroll_remember', tmp_path / 'p')

    async def lazy(spec, endpoint, runs_root, *, run_id, **options):  # finishes without touching the output
        (runs_root / run_id).mkdir(parents=True)
        record = {'status': 'complete', 'final_check': {'passed': True, 'issues': []}, 'usage': {'lead': []},
                  'summary': {'status': 'complete', 'summary': 'Done.', 'open_questions': []}}  # fmt: skip
        return SimpleNamespace(record=record)

    monkeypatch.setattr(service, 'run_job', lazy)
    row = asyncio.run(
        loop_run(
            tmp_path / 'p' / 'payroll_comment_fix',
            'A',
            Settings(api_key='unused'),
            tmp_path / 'runs',
            echo=lambda _: None,
        )
    )
    assert row['status'] == 'complete' and row['cell_accuracy'] > 0.9  # the score alone looks fine
    assert row['proved'] is False and row['comments_changed'] == 0
    # Names in the right order but not in capitals pass the forgiving scorer and fail the exact check.
    job_dir = tmp_path / 'p' / 'payroll_remember'
    key = json.loads((job_dir / 'answer_key.json').read_text(encoding='utf-8'))
    perfect_outputs(load_job(job_dir), key, tmp_path / 'right')
    assert exact_text(tmp_path / 'right', key) is True
    soft = json.loads(json.dumps(key))
    for sheet in soft['deliverables']['joiner_setup_output.xlsx']['sheets'].values():
        for r in sheet['rows']:
            r['Name'] = r['Name'].title()
    perfect_outputs(load_job(job_dir), soft, tmp_path / 'soft')
    assert score(tmp_path / 'soft', key)['cell_accuracy'] == 1.0 and exact_text(tmp_path / 'soft', key) is False
    with pytest.raises(SystemExit, match='no effort "xhigh"'):
        asyncio.run(loop_run(job_dir, 'A', Settings(api_key='unused'), tmp_path / 'runs', effort='xhigh'))


def test_a_packet_whose_skills_cannot_be_read_says_so(tmp_path):
    packets.build('payroll_skill_rule', tmp_path)
    (tmp_path / 'payroll_skill_rule' / 'skills' / 'first-month-pay' / 'SKILL.md').unlink()
    with pytest.raises(SystemExit, match='cannot be read'):
        load_job(tmp_path / 'payroll_skill_rule')


def test_the_summary_sets_an_earlier_scorecard_beside_this_one_and_marks_runs_without_skills():
    from sandbox_agent.experiment import summary

    base = {'shape': 'A', 'status': 'complete', 'row_recall': 1.0, 'row_precision': 1.0, 'relevance_accuracy': 1.0,
            'asked_expected': None, 'unneeded_questions': 0, 'run': 'r'}  # fmt: skip
    before = [
        {
            **base,
            'packet': 'payroll_noise',
            'cell_accuracy': 1.0,
            'seconds': 120,
            'requests': 2,
            'input_tokens': 50_000,
            'output_tokens': 4000,
        }
    ]
    now = [
        {**base, 'packet': 'payroll_noise', 'cell_accuracy': 0.9, 'seconds': 180, 'requests': 3, 'input_tokens': 60_000, 'output_tokens': 5000},
        {**base, 'packet': 'payroll_skill_rule', 'cell_accuracy': 0.667, 'seconds': 80, 'requests': 3, 'input_tokens': 30_000,
         'output_tokens': 1000, 'without_skills': True, 'skills_offered': [], 'skills_read': [], 'proved': None},
    ]  # fmt: skip
    text = summary(now, before)
    assert (
        '| payroll_noise / A | 1 -> 1 | 1.000 -> 0.900 | 1.000 -> 1.000 | 1.000 -> 1.000 | 2.000 -> 3.000 | 2.000 -> 3.000 | 50.000 -> 60.000 | 4.000 -> 5.000 |'
        in text
    )
    assert '| payroll_skill_rule / A | r | - | run without its skills, for comparison: cells right 0.667 |' in text


def test_a_packet_with_an_earlier_run_can_be_loaded_with_or_without_it(tmp_path):
    from openpyxl import load_workbook
    from sandbox_agent.experiment import _proved, summary

    key = packets.build('payroll_like_last_month', tmp_path)
    folder = tmp_path / 'payroll_like_last_month'
    spec = load_job(folder)
    assert [(r['label'], r['title']) for r in spec.past_runs] == [('earlier-1', 'SG01 new joiners, October 2026')]
    assert load_job(folder, past_runs=False).past_runs == ()
    # The earlier sheet shows both habits for every department, for other people than this month's.
    rows = list(load_workbook(spec.past_runs[0]['outputs'] / 'joiners_sheet_output.xlsx')['Joiners'].values)[1:]
    expected = key['deliverables']['joiners_sheet_output.xlsx']['sheets']['Joiners']['rows']
    assert len({r[4] for r in rows}) == 5 and all(', ' in r[1] and r[1].split(',')[0].isupper() for r in rows)
    assert not {r[0] for r in rows} & {r['Employee ID'] for r in expected}
    assert all(r['Name'].split(',')[0].isupper() and r['Dept Code'].startswith('D-') for r in expected)
    # Proven only when the earlier run was opened and the output is right.
    assert _proved(spec, {'past_runs_opened': ['earlier-1']}, {'cell_accuracy': 1.0}) is True
    assert _proved(spec, {'past_runs_opened': []}, {'cell_accuracy': 1.0}) is False
    assert _proved(load_job(folder, past_runs=False), {}, {'cell_accuracy': 1.0}) is None
    base = {'shape': 'A', 'status': 'complete', 'row_recall': 1.0, 'row_precision': 1.0, 'relevance_accuracy': 1.0,
            'asked_expected': None, 'unneeded_questions': 0, 'run': 'r', 'packet': 'payroll_like_last_month',
            'seconds': 60, 'requests': 2, 'input_tokens': 1000, 'output_tokens': 100}  # fmt: skip
    text = summary([
        {**base, 'cell_accuracy': 1.0, 'past_runs_offered': ['earlier-1'], 'past_runs_opened': ['earlier-1'], 'proved': True},
        {**base, 'cell_accuracy': 0.6, 'without_past_runs': True, 'proved': None},
    ])  # fmt: skip
    assert "| r | yes | earlier runs offered ['earlier-1'], opened ['earlier-1'], cells right 1.0 |" in text
    assert '| r | - | run without its earlier runs, for comparison: cells right 0.6 |' in text


def test_a_packet_whose_skill_holds_a_program_is_proven_only_when_the_program_ran(tmp_path):
    from sandbox_agent.experiment import _proved
    from sandbox_agent.packets.payroll_skill_program import staff_levy

    key = packets.build('payroll_skill_program', tmp_path)
    spec = load_job(tmp_path / 'payroll_skill_program')
    assert spec.skills[0]['programs'] == ['scripts/levy.py']
    # The calculator has a floor, bands and rounds to 50 cents: not something to guess.
    assert (staff_levy(1000), staff_levy(4500), staff_levy(5200), staff_levy(9000)) == (61.5, 61.5, 71.0, 176.0)
    rows = key['deliverables']['levy_sheet_output.xlsx']['sheets']['Levy']['rows']
    assert all(r['Staff Levy'] == staff_levy(r['Monthly Salary']) for r in rows)
    right = {'cell_accuracy': 1.0}
    assert (
        _proved(spec, {'skills_read': {'main': ['staff-levy']}, 'skills_run': ['staff-levy/scripts/levy.py']}, right)
        is True
    )
    assert _proved(spec, {'skills_read': {'main': ['staff-levy']}, 'skills_run': []}, right) is False
    assert _proved(spec, {'skills_run': ['staff-levy/scripts/levy.py']}, {'cell_accuracy': 0.9}) is False
