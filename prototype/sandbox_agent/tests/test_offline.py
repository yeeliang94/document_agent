"""Offline tests: no network, no model. They prove the packet, checks, scorer, run folder and harness helpers agree."""

import asyncio
import json
import zipfile
from types import SimpleNamespace

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.datavalidation import DataValidation

from sandbox_agent.agent import _slug, _tool_error, digest, folder_zip, run_job, sync_outputs
from sandbox_agent.checks import inventory, run_checks
from sandbox_agent.config import Settings
from sandbox_agent.packet import build
from sandbox_agent.score import score
from sandbox_agent.store import RunStore

WORKBOOK = 'filled_output.xlsx'


@pytest.fixture(scope='module')
def packet(tmp_path_factory):
    out = tmp_path_factory.mktemp('fixture')
    key = build(out)
    return out, key


def perfect_output(out, key, target):
    wb = load_workbook(out / 'output_template.xlsx')
    for r in key['employees']:
        wb['Employees'].append(
            [r['entity'], r['employee_id'], r['name'], r['department'], r['currency'],
             r['base_local'], r['allowances_local'], r['fx_rate'], r['base_usd']]
        )  # fmt: skip
    for path in key['files']:
        wb['File Map'].append([path, path.rsplit('.', 1)[-1], '', key['roles'].get(path, 'payroll input'), '', ''])
    target.mkdir(parents=True, exist_ok=True)
    wb.save(target / WORKBOOK)
    hierarchy = {'files': [{'path': '/mnt/data/packet/packet/' + p} for p in key['files']]}  # absolute paths match too
    (target / 'document_hierarchy.json').write_text(json.dumps(hierarchy), encoding='utf-8')


def check(out, target):
    return run_checks(target, out / 'output_template.xlsx', inventory(out / 'packet.zip'), WORKBOOK)


def test_packet_is_nested_and_messy(packet):
    out, key = packet
    files = inventory(out / 'packet.zip')
    assert [f['path'] for f in files] == key['files']
    assert all(f['member'] == 'packet/' + f['path'] for f in files)
    assert max(f['path'].count('/') for f in files) >= 5
    assert any(f['junk'] for f in files)
    sg = load_workbook(out / 'packet' / 'Region_APAC/Singapore/Payroll/2026/10_October/SG01_Payroll_Oct2026_FINAL.xlsx')
    assert sg['_codes'].sheet_state == 'hidden' and len(sg.sheetnames) == 5
    assert len(key['excluded_leavers']) == 3  # left 3, 20 and 31 Oct; the 14 Nov leaver stays in
    assert len(key['employees']) == 48 + 37 + 63 + 29 + 41 - 3


def test_perfect_output_passes_checks_and_scores_full_marks(packet, tmp_path):
    out, key = packet
    run = tmp_path / 'run'
    perfect_output(out, key, run / 'outputs')
    assert check(out, run / 'outputs') == {'passed': True, 'issues': []}
    result = score(run, out / 'answer_key.json')
    assert result['row_recall'] == 1 and result['row_precision'] == 1 and result['cell_accuracy'] == 1
    assert all(result['key_roles_correct'].values())


def test_checks_catch_broken_output(packet, tmp_path):
    out, key = packet
    target = tmp_path / 'outputs'
    perfect_output(out, key, target)
    wb = load_workbook(target / WORKBOOK)
    wb['Entity Summary']['B2'] = 46  # formula replaced by a value
    wb['Lists']['A3'] = 'renamed role'  # hidden reference value changed
    wb['Lists'].sheet_state = 'visible'
    wb['Employees']['I2'] = 'n/a'  # number-only cell
    wb['Employees']['D3'] = 'Legal'  # not in the dropdown
    wb['Employees']['G4'] = None  # required number left blank
    wb['Employees'].append(list(next(wb['Employees'].iter_rows(min_row=5, max_row=5, values_only=True))))
    wb.save(target / WORKBOOK)
    (target / 'document_hierarchy.json').write_text('{"files": [{"path": "nope.xlsx"}]}', encoding='utf-8')
    text = '\n'.join(check(out, target)['issues'])
    for expected in [
        'Entity Summary!B2 was', 'Lists!A3 was', 'visibility changed', 'I2', 'must be a number',
        "'Legal' is not in its dropdown", 'G4 is blank', 'duplicates row 5', 'does not include 12 file',
    ]:  # fmt: skip
        assert expected in text, expected


def test_checks_work_on_any_template(tmp_path):
    template = tmp_path / 't.xlsx'
    wb = Workbook()
    wb.active.title = 'Data'
    wb.active.append(['Code', 'Amount'])
    rule = DataValidation(type='list', formula1='"A,B"', allow_blank=True)
    rule.add('A2:A100')
    wb.active.add_data_validation(rule)
    wb.save(template)
    zip_path = tmp_path / 'p.zip'
    with zipfile.ZipFile(zip_path, 'w') as archive:
        archive.writestr('x.csv', 'a')
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    wb.active.append(['A', 'free text is fine'])
    wb.active.append(['C', 1])
    wb.save(outputs / 'out.xlsx')
    (outputs / 'document_hierarchy.json').write_text('[{"path": "x.csv"}]', encoding='utf-8')
    issues = run_checks(outputs, template, inventory(zip_path), 'out.xlsx')['issues']
    assert issues == ["Data!A3 = 'C' is not in its dropdown ['A', 'B']."]


def test_missing_outputs_are_reported(packet, tmp_path):
    out, _ = packet
    issues = run_checks(tmp_path, out / 'output_template.xlsx', [], WORKBOOK)['issues']
    assert issues == [f'{WORKBOOK} has not been saved yet.', 'document_hierarchy.json has not been saved yet.']


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
    store = run_job(bad, template, Settings(api_key='unused'), tmp_path / 'runs', echo=lambda _: None)
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
    monkeypatch.setattr('sandbox_agent.store.time.sleep', lambda _: None)
    with pytest.raises(PermissionError):
        store.replace_outputs({'a.xlsx': b'new'})
    assert (store.outputs / 'a.xlsx').read_bytes() == b'old'


def test_a_slow_scout_is_cut_off_and_the_rest_still_return(tmp_path, monkeypatch):
    import sandbox_agent.agent as agent_module

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
    from sandbox_agent.agent import _log_stream

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
