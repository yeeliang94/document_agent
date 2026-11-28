"""The Files panel: previews of uploads, the template and outputs."""

import functools
import io
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.config import Settings
from app.main import create_app

from helpers import LOCAL, create_job, workbook_bytes


@functools.cache
def pdf_bytes():
    # Made once: the PDF records the second it was made, so two made a second apart differ, and a test that writes
    # the same output twice would see it change.
    from PIL import Image

    out = io.BytesIO()
    Image.new('RGB', (200, 280), 'white').save(out, format='PDF')
    return out.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(base_url=LOCAL, app=create_app(Settings(data_dir=tmp_path, api_key=''))) as c:
        c.root = tmp_path
        yield c


def upload(client, name, data):
    job = create_job(client, files=[('files', (name, data))])
    return job, next(f['path'] for f in job['files'] if f['role'] == 'inputs')


def get(client, job, path, **more):
    return client.get(f'/api/jobs/{job["id"]}/preview', params={'path': path, **more})


def test_a_workbook_previews_as_rows_with_its_sheet_names(client):
    job = create_job(client)
    template = next(f['path'] for f in job['files'] if f['role'] == 'template')
    body = get(client, job, template).json()
    assert body['kind'] == 'sheet' and body['name'] == 'template.xlsx'
    assert body['sheet'] in [s['name'] for s in body['sheets']]
    assert body['columns'][0] == 'A' and all(len(row) <= len(body['columns']) for row in body['rows'])
    other = body['sheets'][-1]['name']
    assert get(client, job, template, sheet=other).json()['sheet'] == other


def test_a_formula_without_a_stored_result_shows_its_worked_out_value_and_dates_read_plainly(client):
    from datetime import datetime

    book = Workbook()
    book.active.append(['Pay', 4200, datetime(2026, 10, 1)])
    book.active.append(['EPF', '=B1*0.11', '=C1+30'])
    book.active.append(['Bonus', '=Rates!A1', '=MYNAME*2'])
    book.active['B2'].number_format = '#,##0.00'
    book.active['C2'].number_format = 'd mmm yyyy'
    book.create_sheet('Rates').append([0.5])
    out = io.BytesIO()
    book.save(out)
    job, path = upload(client, 'HR/pay.xlsx', out.getvalue())
    body = get(client, job, path).json()
    # A defined name it cannot read keeps its formula text, as before.
    assert body['rows'] == [['Pay', 4200, '2026-10-01'], ['EPF', 462, '2026-10-31'], ['Bonus', 0.5, '=MYNAME*2']]
    assert body['formats']['B2'] == {'formula': '=B1*0.11', 'text': '462.00'}
    assert body['formats']['C2']['text'] == '31 Oct 2026' and body['formats']['B3'] == {'formula': '=Rates!A1'}
    assert 'C3' not in body['formats']


def test_a_huge_formatted_number_and_a_date_of_a_blank_cell_never_stop_the_preview(client):
    book = Workbook()
    book.active.append([1e30, '=YEAR(C1)'])
    book.active['A1'].number_format = '0.00'
    out = io.BytesIO()
    book.save(out)
    job, path = upload(client, 'big.xlsx', out.getvalue())
    body = get(client, job, path).json()
    assert body['rows'] == [[1e30, '=YEAR(C1)']]
    assert body['formats']['A1']['text'] == '1' + '0' * 30 + '.00'


def test_text_csv_pdf_and_image_files_each_get_a_fitting_preview(client):
    job, path = upload(client, 'notes.txt', b'Letters win over the register.')
    assert get(client, job, path).json() | {'size': 0} == {
        'path': path, 'name': 'notes.txt', 'size': 0, 'kind': 'text', 'text': 'Letters win over the register.', 'more': False}  # fmt: skip
    job, path = upload(client, 'bank.csv', b'id,net\nE1,3900\n')
    body = get(client, job, path).json()
    assert body['kind'] == 'sheet' and body['rows'] == [['id', 'net'], ['E1', '3900']] and body['columns'] == ['A', 'B']
    job, path = upload(client, 'letter.pdf', pdf_bytes())
    assert get(client, job, path).json()['pages'] == 1
    page = client.get(f'/api/jobs/{job["id"]}/page', params={'path': path, 'page': 1})
    assert page.status_code == 200 and page.headers['content-type'] == 'image/png' and page.content[:4] == b'\x89PNG'
    assert client.get(f'/api/jobs/{job["id"]}/page', params={'path': path, 'page': 2}).status_code == 400
    job, path = upload(client, 'scan.png', b'\x89PNG not really')
    assert get(client, job, path).json()['kind'] == 'image'
    raw = client.get(f'/api/jobs/{job["id"]}/file', params={'path': path})
    assert raw.status_code == 200 and raw.content == b'\x89PNG not really'


def test_a_damaged_or_unknown_file_says_so_instead_of_failing(client):
    job, path = upload(client, 'broken.xlsx', b'not a workbook')
    body = get(client, job, path).json()
    assert body['kind'] == 'none' and body['error']
    job, path = upload(client, 'archive.bin', b'123')
    assert get(client, job, path).json()['kind'] == 'none'


@pytest.mark.parametrize(
    'path', ['../jobs.db', 'working/state.db', 'engine/run-01/record.json', '/etc/passwd', 'inputs', '.']
)
def test_only_uploads_the_template_and_outputs_can_be_read(client, path):
    job = create_job(client)
    folder = client.root / 'jobs' / job['id']
    (folder / 'working').mkdir(exist_ok=True)
    (folder / 'working' / 'state.db').write_bytes(b'private')
    for route in ('preview', 'file', 'page'):
        assert client.get(f'/api/jobs/{job["id"]}/{route}', params={'path': path}).status_code in (400, 404)
    assert client.get('/api/jobs/missing/preview', params={'path': 'inputs/a.txt'}).status_code == 404


def test_an_output_sheet_names_the_uploaded_files_behind_each_row(client):
    job, source = upload(client, 'HR/Reports/employees.txt', b'EMP001')
    outputs = client.root / 'jobs' / job['id'] / 'outputs'
    outputs.mkdir(exist_ok=True)
    (outputs / 'filled.xlsx').write_bytes(workbook_bytes())
    sheet = get(client, job, 'outputs/filled.xlsx').json()['sheet']
    assert get(client, job, 'outputs/filled.xlsx').json()['row_sources'] == {}  # no record yet
    records = [
        {'file': 'filled.xlsx', 'sheet': sheet, 'rows': '2-3', 'sources': ['HR/Reports/employees.txt']},
        {'file': 'filled.xlsx', 'sheet': sheet, 'rows': 3, 'sources': ['/mnt/data/packet/elsewhere.pdf']},
        {
            'file': 'filled.xlsx',
            'sheet': sheet,
            'rows': '4-9999999999',
            'sources': ['/mnt/data/packet/HR/Reports/employees.txt'],
        },
        {'file': 'another.xlsx', 'sheet': sheet, 'rows': '9', 'sources': ['HR/Reports/employees.txt']},
        {'sheet': 'broken record'},
    ]
    (outputs / 'provenance.json').write_text(json.dumps(records), encoding='utf-8')
    found = get(client, job, 'outputs/filled.xlsx').json()['row_sources']
    assert found['2'] == [source] and found['3'] == [source, '/mnt/data/packet/elsewhere.pdf']
    # A path with the sandbox folder in front still finds the upload, and a huge range stops at the last row shown.
    assert found['4'] == [source] and found['500'] == [source] and len(found) == 499


def test_a_long_word_file_is_read_only_as_far_as_the_preview_shows(tmp_path, monkeypatch):
    from docx import Document

    from app import preview

    document = Document()
    for number in range(200):
        document.add_paragraph(f'Paragraph {number}')
    path = tmp_path / 'long.docx'
    document.save(str(path))
    monkeypatch.setattr(preview, 'MAX_CHARS', 100)
    text = preview._docx(path)
    assert text.startswith('Paragraph 0') and 100 < len(text) < 130
    monkeypatch.setattr(preview, 'MAX_DOCX_BYTES', 10)
    with pytest.raises(ValueError, match='too large'):
        preview._docx(path)


def test_a_workbook_sends_each_cells_formatting_and_shown_text(client):
    from datetime import datetime

    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.styles.colors import Color

    book = Workbook()
    sheet = book.active
    sheet.append(['Vendor', 'Amount', 'Change', 'Date', 'Plain'])
    sheet.append(['Apex', 12400, -0.031, datetime(2026, 7, 14), 7])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor=Color(theme=4))  # the theme's first accent colour
        cell.alignment = Alignment(horizontal='center')
    sheet['A2'].font = Font(italic=True)
    sheet['B2'].number_format = '$#,##0.00'
    sheet['B2'].border = Border(top=Side(style='double'))
    sheet['C2'].number_format = '0.0%;[Red](0.0%)'
    sheet['D2'].number_format = 'd mmm yyyy'
    out = io.BytesIO()
    book.save(out)
    job, path = upload(client, 'spend.xlsx', out.getvalue())
    body = get(client, job, path).json()

    assert body['rows'][1] == ['Apex', 12400, -0.031, '2026-07-14', 7]  # the values stay as they were
    formats, styles = body['formats'], body['styles']
    header = styles[formats['A1']['style']]
    assert header == {'bold': True, 'color': '#ffffff', 'fill': '#4f81bd', 'align': 'center'}  # fill: theme accent 1
    assert all(formats[f'{c}1']['style'] == formats['A1']['style'] for c in 'BCDE')  # one shared style, sent once
    assert styles[formats['A2']['style']] == {'italic': True}
    assert formats['B2']['text'] == '$12,400.00'
    assert styles[formats['B2']['style']] == {'border': {'top': '3px double #18181b'}}
    assert formats['C2'] == {'text': '(3.1%)', 'color': '#b42318'}  # a number format with no other style
    assert formats['D2'] == {'text': '14 Jul 2026'}
    assert 'E2' not in formats  # a plain cell sends nothing


def test_light_text_is_dropped_when_its_fill_cannot_be_drawn():
    from openpyxl.styles import Font, PatternFill

    from app.cell_format import style

    sheet = Workbook().active
    sheet['A1'].font = Font(color='FFFFFF')
    sheet['A1'].fill = PatternFill('darkGrid', fgColor='1F4E79')  # a pattern the preview does not draw
    sheet['B1'].font = Font(color='FFFFFF')
    sheet['B1'].fill = PatternFill('solid', fgColor='1F4E79')
    assert style(sheet['A1'], []) == {}
    assert style(sheet['B1'], []) == {'color': '#ffffff', 'fill': '#1f4e79'}


@pytest.mark.parametrize(
    ('value', 'code', 'shown'),
    [
        (12400, '$#,##0.00', '$12,400.00'),
        (-3.5, '$#,##0.00', '-$3.50'),
        (0.082, '0.0%', '8.2%'),
        (0, '#,##0;(#,##0);"-"', '-'),
        (1234567, '#,##0,"K"', '1,235K'),
        (5.25, '0.##', '5.25'),
        (5, '0.##', '5'),
        (7, '000', '007'),
        (1234.5, '[$€-407]#,##0.00', '€1,234.50'),
        (1.125, '$0.00', '$1.13'),  # Excel rounds halves up, not to the even digit
        (-1.125, '0.00', '-1.13'),
        (2.5, '0', '3'),
        (-1234.5, '_($* #,##0.00_);_($* (#,##0.00);_($* "-"??_);_(@_)', '$(1,234.50)'),
        ('2026-07-14 15:05', 'yyyy-mm-dd h:mm AM/PM', '2026-07-14 3:05 PM'),
        ('2026-07-14 09:05', 'mmm d, h:mm', 'Jul 14, 9:05'),
        ('2026-07-14', 'dddd, mmmm d', 'Tuesday, July 14'),
    ],
)
def test_number_formats_show_as_excel_shows_them(value, code, shown):
    from datetime import datetime

    from app.cell_format import shown_text

    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    assert shown_text(value, code)[0] == shown


@pytest.mark.parametrize(
    'code',
    ['General', '@', '# ?/?', '0.00E+00', '[>100]0;0', 'd/m/yyyy', '000-00-0000', '00000-0000', '(###) ###-####',
     '0 "in" 0', '0.0%0'],
)  # fmt: skip
def test_formats_the_preview_cannot_follow_fall_back_to_the_plain_value(code):
    from app.cell_format import shown_text

    assert shown_text(123456789, code) is None


def test_the_page_files_are_checked_for_a_newer_version_on_every_load(client):
    first = client.get('/comments.js')
    assert first.headers['cache-control'] == 'no-cache'
    assert client.get('/comments.js', headers={'if-none-match': first.headers['etag']}).status_code == 304


def text_pdf(pages: list[list[str]]) -> bytes:
    """A small PDF with real text, one line of Helvetica per entry, 72 points apart down each page."""
    objects = ['<< /Type /Catalog /Pages 2 0 R >>', '', '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for lines in pages:
        body = ''.join(f'BT /F1 12 Tf 72 {720 - 72 * i} Td ({line}) Tj ET\n' for i, line in enumerate(lines))
        objects.append(f'<< /Length {len(body)} >>\nstream\n{body}endstream')
        objects.append(
            f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objects)} 0 R '
            '/Resources << /Font << /F1 3 0 R >> >> >>'
        )
        kids.append(f'{len(objects)} 0 R')
    objects[1] = f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {len(kids)} >>'
    out, offsets = b'%PDF-1.4\n', []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f'{n} 0 obj\n{obj}\nendobj\n'.encode()
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode()
    out += b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets)
    out += f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return out


INVOICE = text_pdf([['Cover letter for invoice SC26100217'], ['Tax invoice SC26100217', 'Licences 2,315.60', 'Service tax 185.25', 'Total due 2,500.85']])  # fmt: skip


def test_a_search_in_an_older_version_of_a_pdf_looks_in_that_version(client):
    job = create_job(client)
    folder = client.root / 'jobs' / job['id']
    (folder / 'outputs').mkdir(exist_ok=True)
    (folder / 'outputs' / 'report.pdf').write_bytes(text_pdf([['Total due 900.00']]))
    kept = folder / 'versions' / 'v0001' / 'files'
    kept.mkdir(parents=True)
    (kept / 'report.pdf').write_bytes(INVOICE)

    def pages(**more):
        params = {'path': 'outputs/report.pdf', 'q': 'SC26100217', **more}
        return [h['page'] for h in client.get(f'/api/jobs/{job["id"]}/find', params=params).json()['hits']]

    assert pages() == []
    assert pages(version=1) == [1, 2]


def test_a_search_finds_each_match_in_a_pdf_with_its_page_and_box(client):
    job, path = upload(client, 'invoice.pdf', INVOICE)
    found = client.get(f'/api/jobs/{job["id"]}/find', params={'path': path, 'q': 'sc26100217'}).json()
    assert [h['page'] for h in found['hits']] == [1, 2] and found['more'] is False and found['text'] is True
    first = found['hits'][1]
    # The second page's first line sits 72 points below the top margin of a 792-point page.
    assert 0.1 < first['x'] < 0.4 and 0.05 < first['y'] < 0.1 and first['w'] > 0 and first['h'] > 0
    assert client.get(f'/api/jobs/{job["id"]}/find', params={'path': path, 'q': 'nowhere'}).json()['hits'] == []
    # A scanned PDF has no text at all, and the page can say so.
    scan_job, scan = upload(client, 'scan.pdf', pdf_bytes())
    blank = client.get(f'/api/jobs/{scan_job["id"]}/find', params={'path': scan, 'q': 'total'}).json()
    assert blank == {'hits': [], 'more': False, 'text': False}
    # Only PDFs are searched on the server; other files are searched in the page.
    sheet_job, sheet_path = upload(client, 'list.csv', b'a,b\n')
    assert client.get(f'/api/jobs/{sheet_job["id"]}/find', params={'path': sheet_path, 'q': 'a'}).status_code == 400


def test_a_row_is_found_on_the_page_holding_most_of_its_values(client):
    job, path = upload(client, 'invoice.pdf', INVOICE)
    # The page sends each value as shown and as stored, so the same amount comes twice.
    row = ['Selatan Courier', 'SC26100217', '2315.60', '2,315.60', '185.25', '2,500.85', '2500.85', '0', 'x']
    found = client.post(f'/api/jobs/{job["id"]}/locate', json={'path': path, 'values': row}).json()
    # The invoice number is on both pages; the amounts are only on page 2. Zero and one letter are not looked for,
    # and the vendor's name is not on the invoice.
    assert found['page'] == 2 and found['found'] == 4 and found['of'] == 5
    assert len(found['boxes']) == 4
    assert found['wanted'] == ['Selatan Courier', 'SC26100217', '2,315.60', '2315.60', '185.25', '2,500.85', '2500.85']
    none = client.post(f'/api/jobs/{job["id"]}/locate', json={'path': path, 'values': ['Nobody']}).json()
    assert none == {'page': None, 'boxes': [], 'wanted': ['Nobody'], 'values': {}, 'spots': {}}
    # Another kind of source is not searched here; the page outlines the values in it from `wanted`.
    sheet_job, sheet = upload(client, 'vendors.csv', b'vendor,amount\nSelatan Courier,2500.85\n')
    other = client.post(f'/api/jobs/{sheet_job["id"]}/locate', json={'path': sheet, 'values': ['2500.85']}).json()
    assert other == {'page': None, 'boxes': [], 'wanted': ['2,500.85', '2500.85'], 'values': {'2500.85': 'row 2'},
                     'spots': {'2500.85': {'cell': 'B2'}}}  # fmt: skip
    too_long = client.post(f'/api/jobs/{job["id"]}/locate', json={'path': path, 'values': ['x' * 201]})
    assert too_long.status_code == 422


def test_each_value_of_a_row_says_where_it_was_found_in_a_source(client):
    # A PDF names the first page each value is on: the invoice number is on page 1, the amounts on page 2.
    job, path = upload(client, 'invoice.pdf', INVOICE)
    row = ['SC26100217', '2,315.60', '2315.60', 'Selatan Courier']
    found = client.post(f'/api/jobs/{job["id"]}/locate', json={'path': path, 'values': row}).json()
    assert found['values'] == {'SC26100217': 'page 1', '2,315.60': 'page 2', '2315.60': 'page 2'}
    assert found['spots']['SC26100217'] == {'page': 1}  # a link to the source opens that page
    # A workbook names the sheet and row, and matches a number as a number: 4500 in a cell is 4,500.00.
    book = Workbook()
    book.active.title = 'Employees'
    book.active.append(['ID', 'Name', 'Salary'])
    book.active.append(['EMP001', 'Alex Example', 4500])
    buffer = io.BytesIO()
    book.save(buffer)
    sheet_job, sheet = upload(client, 'staff.xlsx', buffer.getvalue())
    values = ['EMP001', 'Alex Example', '4,500.00', '12,500.00', 'Example']
    found = client.post(f'/api/jobs/{sheet_job["id"]}/locate', json={'path': sheet, 'values': values}).json()
    assert found['values'] == {'EMP001': 'Employees, row 2', 'Alex Example': 'Employees, row 2', '4,500.00': 'Employees, row 2', 'Example': 'Employees, row 2'}  # fmt: skip
    # ...and the exact cell, so a link opens that sheet with the value ringed.
    assert found['spots']['4,500.00'] == {'sheet': 'Employees', 'cell': 'C2'}
    # A value past what the preview shows opens its sheet, with no cell to ring that is not drawn.
    from app.preview import MAX_ROWS, _spot
    assert _spot(MAX_ROWS + 1, 3, 'Employees') == {'sheet': 'Employees'} and _spot(MAX_ROWS, 3) == {'cell': f'C{MAX_ROWS}'}
    # A text file names the line, and a value is found only as a whole word or number.
    text_job, text = upload(client, 'letter.txt', b'Dear Alex,\nYour salary is 14,500.00.\nFrom 1 Oct: 4,500.00\n')
    found = client.post(f'/api/jobs/{text_job["id"]}/locate', json={'path': text, 'values': ['4500']}).json()
    assert found['values'] == {'4500': 'line 3'}
    assert found['spots'] == {'4500': {'line': 3}}
    # A picture is not searched.
    picture_job, picture = upload(client, 'letter.png', b'not really a picture')
    assert client.post(f'/api/jobs/{picture_job["id"]}/locate', json={'path': picture, 'values': ['4500']}).json()['values'] == {}


def test_amounts_are_looked_for_in_the_ways_a_source_writes_them():
    from app.preview import _forms

    assert _forms('2500.85') == ['2,500.85', '2500.85']
    assert _forms('(RM 12,960)') == ['12,960.00', '12960.00', '12,960', '12960']
    assert (
        _forms('0.00') == []
        and _forms('7') == []
        and _forms('40.00') == []
        and _forms('5000') == ['5,000.00', '5000.00', '5,000', '5000']
        and _forms('ab') == []
    )
    assert _forms('SC26100217') == ['SC26100217'] and _forms('S$ 250.5') == ['250.50']
    assert _forms('Raj Kumar') == ['Raj Kumar']
