"""A house style for workbooks built from a format, and save warnings about how a workbook looks in Excel."""

import ast
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from app.engine.checks import run_checks
from app.engine.goal import goal_card
from app.engine.house_style import apply, check_style
from app.engine.jobspec import Deliverable, JobSpec
from app.engine.looks import warnings
from app.engine.rules import check_format_definition

FORMAT = {
    'sheets': [
        {
            'name': 'Journal',
            'columns': [
                {'name': 'Ref', 'type': 'text'},
                {'name': 'Account', 'type': 'text'},
                {'name': 'Date', 'type': 'date'},
                {'name': 'Amount', 'type': 'number'},
            ],
        }
    ]
}
FILES = [{'path': 'pay.txt', 'bytes': 10, 'type': 'txt'}]


def packet(tmp_path) -> Path:
    path = tmp_path / 'packet.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('pay.txt', 'x')
    return path


def decided(outputs: Path) -> None:
    entry = {'path': 'pay.txt', 'type': 'txt', 'decision': 'used', 'reason': 'r', 'summary': 's'}
    (outputs / 'document_map.json').write_text(json.dumps({'files': [entry]}), encoding='utf-8')


def built(outputs: Path, rows) -> Path:
    book = Workbook()
    sheet = book.active
    sheet.title = 'Journal'
    sheet.append(['Ref', 'Account', 'Date', 'Amount'])
    for row in rows:
        sheet.append(row)
    path = outputs / 'journal.xlsx'
    book.save(path)
    return path


def test_a_workbook_built_from_a_format_gets_its_look_from_the_helper_and_passes(tmp_path):
    outputs = tmp_path / 'out'
    outputs.mkdir()
    decided(outputs)
    path = built(outputs, [['PAY-1', '2100', datetime(2026, 10, 31), 4200.5], ['PAY-2', '1010', '2026-10-31', '1,250.75']])
    spec = JobSpec(id='j', module='payroll', goal='g', zip=packet(tmp_path),
                   deliverables=(Deliverable('journal.xlsx', format=FORMAT, format_path=tmp_path / 'f.json'),))  # fmt: skip
    before = run_checks(outputs, spec, FILES, 'none')
    assert before['summary']['looks_right'] is False
    assert any('column D: numbers with decimals have no number format' in line for line in before['looks'])
    assert apply(path, FORMAT) == 'House style applied to Journal.'
    after = run_checks(outputs, spec, FILES, 'none')
    assert after['passed'] is True and after['looks'] == [] and after['summary']['looks_right'] is True
    sheet = load_workbook(path)['Journal']
    assert sheet['A1'].font.b and sheet['A1'].fill.fgColor.rgb == 'FFD9E1F2' and sheet.freeze_panes == 'A2'
    assert sheet['C3'].value == datetime(2026, 10, 31) and sheet['C3'].number_format == 'dd/mm/yyyy'
    assert sheet['D3'].value == 1250.75 and sheet['D3'].number_format == '#,##0.00'
    assert sheet['B2'].value == '2100'  # a code in a text column stays text
    assert sheet.column_dimensions['C'].width >= 12


def test_a_format_can_set_its_own_style_and_a_wrong_one_is_refused(tmp_path):
    styled = {**FORMAT, 'style': {'header': {'fill': '000000', 'font_color': 'FFFFFF'}, 'date_format': 'd mmm yyyy',
                                  'column_widths': {'Ref': 20}, 'freeze_header': False}}  # fmt: skip
    check_format_definition(styled)
    path = built(tmp_path, [['PAY-1', '2100', datetime(2026, 10, 31), 4200.5]])
    apply(path, styled)
    sheet = load_workbook(path)['Journal']
    assert sheet['A1'].font.color.rgb == 'FFFFFFFF' and sheet['C2'].number_format == 'd mmm yyyy'
    assert sheet.column_dimensions['A'].width == 20 and sheet.freeze_panes is None
    for wrong in ({'colour': 'red'}, {'header': {'fill': 'red'}}, {'column_widths': {'Ref': 0}}):
        with pytest.raises(ValueError):
            check_style(wrong)
    with pytest.raises(ValueError, match='Unknown style setting'):
        check_format_definition({**FORMAT, 'style': {'colour': 'red'}})


def test_a_hash_column_and_a_date_stored_as_text_each_get_a_warning(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet.append(['Name', 'Paid on', 'Amount'])
    sheet.append(['Ann', '31/10/2026', 123456789.25])
    sheet['C2'].number_format = '#,##0.00'
    sheet.column_dimensions['C'].width = 6
    sheet.append(['Bo', datetime(2026, 10, 1), 5.5])
    sheet['B3'].number_format = 'dd/mm/yyyy'
    sheet.column_dimensions['B'].width = 12
    sheet['C3'].number_format = '#,##0.00'
    sheet.row_dimensions[3].hidden = True
    book.save(tmp_path / 'w.xlsx')
    found = warnings(tmp_path / 'w.xlsx', {'Sheet': {2, 3}})
    assert found == [
        'Sheet: 1 value(s) are too wide for column(s) C, so Excel shows #### (e.g. C2 "123,456,789.25"). Widen the '
        'column.',
        'Sheet column B: 1 dates stored as text (e.g. B2 "31/10/2026"). Write them as real dates with a date format.',
        'Sheet: rows 3 hold data and are hidden. Show them, or say why in notes.md.',
    ]


def test_a_filled_template_is_warned_only_about_cells_the_agent_wrote(tmp_path):
    template = Workbook()
    sheet = template.active
    sheet.title = 'Pay'
    sheet.append(['Name', 'Amount'])
    sheet.append([None, None])
    sheet['D1'] = 987654321.5  # the template's own figure, in a narrow column: its look, not the agent's
    sheet.column_dimensions['D'].width = 4
    template.save(tmp_path / 'pay.xlsx')
    filled = load_workbook(tmp_path / 'pay.xlsx')
    filled['Pay']['A2'], filled['Pay']['B2'] = 'Ann', 4200.25
    outputs = tmp_path / 'out'
    outputs.mkdir()
    filled.save(outputs / 'pay_output.xlsx')
    decided(outputs)
    spec = JobSpec(id='j', module='payroll', goal='g', zip=packet(tmp_path),
                   deliverables=(Deliverable('pay_output.xlsx', template=tmp_path / 'pay.xlsx'),))  # fmt: skip
    check = run_checks(outputs, spec, FILES, 'none')
    assert check['passed'] is True
    assert check['looks'] == [
        'pay_output.xlsx: Pay column B: numbers with decimals have no number format (e.g. B2). Give the column one, '
        'such as #,##0.00.'
    ]


def test_the_brief_offers_the_helper_for_built_workbooks_only():
    remote = {'packet.zip': 'p', 'f.json': '/mnt/data/f.json', 'house_style.py': '/mnt/data/house_style.py'}
    call = "import sys; sys.path.insert(0, '/mnt/data'); from house_style import apply; apply('/mnt/data/outputs/"
    formatted = JobSpec(id='j', module='payroll', goal='g', zip=Path('p.zip'),
                        deliverables=(Deliverable('j.xlsx', format=FORMAT, format_path=Path('f.json')),))  # fmt: skip
    assert call + "j.xlsx', '/mnt/data/f.json')" in goal_card(formatted, remote, 'none')
    free = JobSpec(id='j', module='payroll', goal='g', zip=Path('p.zip'), deliverables=())
    assert call + "<file>.xlsx')" in goal_card(free, remote, 'none')
    # A run with a template never gets the helper: the template keeps its own look.
    assert 'house_style' not in goal_card(free, {'packet.zip': 'p'}, 'none')


def test_the_helper_needs_nothing_but_openpyxl_in_the_sandbox():
    source = Path(__file__).parent.parent / 'app' / 'engine' / 'house_style.py'
    imported = set()
    for node in ast.walk(ast.parse(source.read_text(encoding='utf-8'))):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, 'the helper must not import from the app'
            imported.add(node.module.split('.')[0])
        elif isinstance(node, ast.Import):
            imported |= {alias.name.split('.')[0] for alias in node.names}
    assert imported <= {'__future__', 'json', 're', 'datetime', 'pathlib', 'openpyxl'}


def test_a_run_uploads_the_helper_and_offers_look_at_output_unless_it_fills_a_template(tmp_path, monkeypatch):
    from test_skill_engine import run_with_fake_sandbox, spec_with_skills

    (tmp_path / 'a').mkdir()
    templated = spec_with_skills(tmp_path / 'a', skills=())
    _, seen, box = run_with_fake_sandbox(monkeypatch, templated, tmp_path / 'a')
    assert 'house_style.py' not in box.uploads
    assert 'look_at_output' in [getattr(t, 'name', '') for t in seen['agent'].tools]
    assert 'look_at_output' in seen['agent'].instructions and 'matplotlib' not in seen['agent'].instructions
    (tmp_path / 'b').mkdir()
    built_spec = JobSpec(**{**templated.__dict__, 'deliverables': ()})
    _, seen, box = run_with_fake_sandbox(monkeypatch, built_spec, tmp_path / 'b')
    assert box.uploads['house_style.py'] == (Path(__file__).parent.parent / 'app/engine/house_style.py').read_bytes()
    assert 'from house_style import apply' in seen['agent'].instructions


def test_the_helper_formats_a_column_that_ends_in_a_total_and_keeps_formats_the_agent_chose(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet.append(['Item', 'Amount', 'Share'])
    sheet.append(['Rent', 1200.5, 0.25])
    sheet.append(['Staff', 33000.25, 0.75])
    sheet['C2'].number_format = sheet['C3'].number_format = '0%'
    sheet.append(['Total', '=SUM(B2:B3)', None])
    book.save(tmp_path / 'summary.xlsx')
    apply(tmp_path / 'summary.xlsx')
    styled = load_workbook(tmp_path / 'summary.xlsx').active
    assert [styled[f'B{r}'].number_format for r in (2, 3, 4)] == ['#,##0.00'] * 3
    assert styled['C2'].number_format == '0%'


def test_long_ids_stay_text_and_times_stay_shown(tmp_path):
    from datetime import datetime

    from openpyxl import Workbook, load_workbook

    from app.engine.house_style import apply, as_number

    assert as_number('12345678901234567890') is None and as_number('4,200.50') == 4200.5
    book = Workbook()
    book.active.append(['Account', 'Posted'])
    book.active.append(['12345678901234567890', datetime(2026, 10, 31, 14, 30)])
    book.active.append(['12345678901234567891', datetime(2026, 10, 31)])
    book.save(tmp_path / 'b.xlsx')
    apply(tmp_path / 'b.xlsx')
    sheet = load_workbook(tmp_path / 'b.xlsx').active
    assert sheet['A2'].value == '12345678901234567890'
    assert (sheet['B2'].number_format, sheet['B3'].number_format) == ('dd/mm/yyyy hh:mm', 'dd/mm/yyyy')
