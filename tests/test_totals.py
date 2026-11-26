"""Every save works out what the workbook's formulas come to, and names a total that leaves rows out."""

import json
import zipfile

from openpyxl import Workbook

from app.engine.checks import run_checks
from app.engine.jobspec import Deliverable, JobSpec
from app.engine.totals import work_out

FILES = [{'path': 'pay.txt', 'bytes': 10, 'type': 'txt'}]


def template(path):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Pay'
    sheet.append(['Name', 'Amount'])
    sheet.append([None, None])
    sheet.append([None, None])
    sheet['A4'], sheet['B4'] = 'Total', '=SUM(B2:B3)'
    book.save(path)
    return path


def spec(tmp_path):
    packet = tmp_path / 'packet.zip'
    with zipfile.ZipFile(packet, 'w') as archive:
        archive.writestr('pay.txt', 'Ann 100')
    deliverable = Deliverable('pay_output.xlsx', template=template(tmp_path / 'pay.xlsx'))
    return JobSpec(id='j', module='payroll', goal='Fill the pay sheet.', zip=packet, deliverables=(deliverable,))


def outputs_with(tmp_path, rows, total):
    outputs = tmp_path / 'out'
    outputs.mkdir(exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = 'Pay'
    sheet.append(['Name', 'Amount'])
    for row in rows:
        sheet.append(row)
    sheet.append(['Total', total])
    book.save(outputs / 'pay_output.xlsx')
    entry = {'path': 'pay.txt', 'type': 'txt', 'decision': 'used', 'reason': 'r', 'summary': 's'}
    (outputs / 'document_map.json').write_text(json.dumps({'files': [entry]}), encoding='utf-8')
    return outputs


def test_a_save_lists_each_total_with_its_value(tmp_path):
    outputs = outputs_with(tmp_path, [['Ann', 100], ['Bo', 250.5]], '=SUM(B2:B3)')
    check = run_checks(outputs, spec(tmp_path), FILES, 'none')
    assert check['passed'] is True, check['issues']
    assert check['totals'] == ['pay_output.xlsx Pay!B4 = 350.50 (=SUM(B2:B3))']
    assert (check['summary']['totals'], check['summary']['totals_problems']) == (1, 0)


def test_a_total_that_misses_the_last_row_is_named_and_the_save_does_not_pass(tmp_path):
    # A row was inserted for a third person, but the total still stops at row 3.
    outputs = outputs_with(tmp_path, [['Ann', 100], ['Bo', 250], ['Cy', 75]], '=SUM(B2:B3)')
    check = run_checks(outputs, spec(tmp_path), FILES, 'none')
    assert check['passed'] is False
    missed = [line for line in check['issues'] if 'leaves out' in line]
    assert missed == [
        'pay_output.xlsx: Pay!B5 (=SUM(B2:B3)) leaves out B4, a number between the range and the total. Stretch the '
        'range over every row it should total.'
    ]
    assert check['summary']['totals_problems'] == 1


def sheet_of(tmp_path, cells: dict):
    book = Workbook()
    for place, value in cells.items():
        book.active[place] = value
    book.save(tmp_path / 'layout.xlsx')
    return work_out(tmp_path / 'layout.xlsx')


def test_correct_layouts_are_not_mistaken_for_a_total_that_leaves_rows_out(tmp_path):
    # A subtotal, a tax line on it, and a total of the two.
    subtotal = {'B2': 100, 'B3': 200, 'B4': 300, 'B5': '=SUM(B2:B4)', 'B6': '=B5*0.2', 'B7': '=SUM(B5:B6)'}
    # An opening balance above the movements it is added to, and a year heading above a column total.
    balance = {'B1': 2026, 'B2': 1000, 'B3': 50, 'B4': -20, 'B5': '=B2+SUM(B3:B4)'}
    # Two ranges that between them cover every row.
    split = {'B2': 100, 'B3': 200, 'B4': 300, 'B5': 400, 'B6': '=SUM(B2:B3,B4:B5)'}
    for cells in (subtotal, balance, split):
        assert sheet_of(tmp_path, cells).problems == [], cells
    # A total above its rows still finds a number left between them.
    above = {'B1': '=SUM(B3:B4)', 'B2': 99, 'B3': 1, 'B4': 2}
    assert [p.split(' leaves out ')[1][:2] for p in sheet_of(tmp_path, above).problems] == ['B2']


def test_a_template_whose_own_total_skips_a_row_does_not_block_its_filled_copies(tmp_path):
    from app.engine.totals import template_faults

    book = Workbook()
    sheet = book.active
    sheet.title = 'Pay'
    sheet['B2'], sheet['B3'], sheet['B4'], sheet['B5'] = None, 5, None, '=SUM(B2:B2)'  # skips a fixed fee in B3
    book.save(tmp_path / 'odd.xlsx')
    known = template_faults(tmp_path / 'odd.xlsx')
    assert known == {('Pay', '=SUM(B2:B2)')}
    sheet['B2'] = 100
    book.save(tmp_path / 'filled.xlsx')
    assert work_out(tmp_path / 'filled.xlsx').problems != []
    assert work_out(tmp_path / 'filled.xlsx', known).problems == []


def test_a_formula_showing_an_error_is_a_problem_unless_the_template_showed_it(tmp_path):
    outputs = outputs_with(tmp_path, [['Ann', 100], ['Bo', 0]], '=SUM(B2:B3)')
    book = Workbook()
    book.active['A1'], book.active['A2'] = 0, '=10/A1'
    book.save(outputs / 'extra.xlsx')
    found = work_out(outputs / 'extra.xlsx')
    assert found.problems == ['Sheet!A2 shows #DIV/0! (=10/A1). Fix the formula or the cells it reads.']
    assert work_out(outputs / 'extra.xlsx', frozenset({('Sheet', '=10/A1')})).problems == []


def test_a_formula_the_calculator_cannot_work_out_is_counted_not_guessed(tmp_path):
    book = Workbook()
    book.active['A1'] = '=SOMEFUTUREFUNCTION(1)'
    book.save(tmp_path / 'odd.xlsx')
    found = work_out(tmp_path / 'odd.xlsx')
    assert (found.problems, found.unsupported, found.totals) == ([], 1, [])
