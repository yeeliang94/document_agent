from openpyxl import Workbook

from app import links


def files(tmp_path):
    root = tmp_path / 'job'
    (root / 'outputs').mkdir(parents=True)
    (root / 'inputs' / 'March').mkdir(parents=True)
    book = Workbook()
    book.active.title = 'Payroll'
    for _ in range(1, 8):
        book.active.append(['x'] * 7)
    book.create_sheet('Pay summary')['B2'] = 1
    book.save(root / 'outputs' / 'payroll.xlsx')
    other = Workbook()
    other.active.title = 'Summary'
    other.save(root / 'outputs' / 'other.xlsx')
    (root / 'inputs' / 'March' / 'timesheet.pdf').write_bytes(b'%PDF')
    (root / 'inputs' / 'notes.txt').write_text('x')
    (root / 'outputs' / 'notes.txt').write_text('x')
    paths = [
        'outputs/payroll.xlsx',
        'outputs/other.xlsx',
        'inputs/March/timesheet.pdf',
        'inputs/notes.txt',
        'outputs/notes.txt',
    ]
    return root, paths


def shown(text, found):
    return [(text[f['start'] : f['end']], f['path'], f.get('cell')) for f in found]


def test_only_real_cells_and_files_become_links(tmp_path):
    root, paths = files(tmp_path)
    text = (
        'Changed Payroll!E3 and Payroll!$G$3:$G$7 from timesheet.pdf (March/timesheet.pdf). '
        "Not Wages!B4, not Payroll!Z99, see 'Pay summary'!B2 and notes.txt."
    )
    assert shown(text, links.find(root, paths, text)) == [
        ('Payroll!E3', 'outputs/payroll.xlsx', 'E3'),
        ('Payroll!$G$3:$G$7', 'outputs/payroll.xlsx', 'G3:G7'),
        ('timesheet.pdf', 'inputs/March/timesheet.pdf', None),
        ('March/timesheet.pdf', 'inputs/March/timesheet.pdf', None),
        ("'Pay summary'!B2", 'outputs/payroll.xlsx', 'B2'),
    ]  # a missing sheet, a cell past the sheet, and a name two files share stay plain text


def test_a_sheet_in_two_workbooks_links_to_neither(tmp_path):
    root, paths = files(tmp_path)
    book = Workbook()
    book.active.title = 'Payroll'
    book.save(root / 'outputs' / 'second.xlsx')
    assert links.find(root, [*paths, 'outputs/second.xlsx'], 'See Payroll!A1.') == []
    assert links.find(root, paths, '') == [] and links.find(root, ['outputs/missing.xlsx'], 'Payroll!A1') == []


def test_a_cell_that_cannot_exist_is_plain_text_not_an_error(tmp_path):
    root, paths = files(tmp_path)
    assert links.find(root, paths, 'See Payroll!A0 and Payroll!A1.') == [
        {'start': 19, 'end': 29, 'path': 'outputs/payroll.xlsx', 'sheet': 'Payroll', 'cell': 'A1'}
    ]

