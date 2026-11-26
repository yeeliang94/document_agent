"""The agent sees its output as Excel shows it: the sheet picture, the look_at_output tool and the look before
finishing."""

import asyncio
import io
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agents import ToolOutputImage, ToolOutputText
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from PIL import Image

from app.engine import looking
from app.engine.jobspec import Deliverable, JobSpec
from app.engine.runstore import RunStore
from app.sheet_picture import draw, tokens

THIN = Side(style='thin', color='000000')


def sample(path: Path, border: bool = True, rows: int = 2) -> Path:
    book = Workbook()
    sheet = book.active
    sheet.title = 'Payroll'
    sheet['A1'] = 'Payroll register'
    sheet['A1'].font = Font(bold=True, size=14)
    sheet['A1'].alignment = Alignment(horizontal='center')
    sheet.merge_cells('A1:D1')
    for column, name in enumerate(['Employee', 'Start date', 'Salary', 'Annual'], 1):
        cell = sheet.cell(2, column, name)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='D9E1F2')
        if border:
            cell.border = Border(bottom=THIN)
    for r in range(3, 3 + rows):
        sheet.cell(r, 1, f'EMP{r:03d}')
        sheet.cell(r, 2, datetime(2026, 10, 31)).number_format = 'dd/mm/yyyy'
        sheet.cell(r, 3, 4200.5).number_format = '#,##0.00'
        sheet.cell(r, 4, f'=C{r}*12').number_format = '#,##0.00'
    total = 3 + rows
    sheet.cell(total, 3, f'=SUM(C3:C{total - 1})').number_format = '#,##0.00'
    for letter, width in zip('ABCD', (12, 12, 12, 14), strict=True):
        sheet.column_dimensions[letter].width = width
    book.save(path)
    return path


def pixel(png: bytes, x: int, y: int):
    with Image.open(io.BytesIO(png)) as image:
        return image.convert('RGB').getpixel((x, y))


def test_a_sheet_is_drawn_with_its_merged_title_shaded_header_dates_and_totals(tmp_path):
    picture = draw(sample(tmp_path / 'pay.xlsx'))
    assert picture.sheet == 'Payroll' and picture.tiles == ['A1:D5'] and len(picture.images) == 1
    title = picture.cells['A1']
    assert (title.text, title.span, title.bold, title.align) == ('Payroll register', 'A1:D1', True, 'center')
    header = picture.cells['A2']
    assert (header.text, header.bold, header.fill, header.borders) == ('Employee', True, '#d9e1f2', {'bottom': '#000000'})
    assert picture.cells['B3'].text == '31/10/2026' and picture.cells['B3'].align == 'right'
    assert picture.cells['D3'].text == '50,406.00'  # a formula's result, in its number format
    assert picture.cells['C5'].text == '8,401.00'  # the total, worked out
    assert picture.notes == []
    # The header's shade is really painted: the right end of B2, past its text, has the fill colour.
    png = picture.images[0]
    assert Image.open(io.BytesIO(png)).format == 'PNG' and 85 < tokens(png) < 2000
    assert pixel(png, 265, 60) == (0xD9, 0xE1, 0xF2)


def test_a_number_too_wide_for_its_column_shows_hashes_and_text_spills_into_empty_cells(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet['A1'] = 123456789.25
    sheet['A1'].number_format = '#,##0.00'
    sheet['A2'] = 'A long label that runs on'
    book.save(tmp_path / 'narrow.xlsx')
    picture = draw(tmp_path / 'narrow.xlsx')
    assert set(picture.cells['A1'].text) == {'#'}
    assert picture.cells['A2'].text == 'A long label that runs on'


def test_hidden_rows_and_what_cannot_be_drawn_are_named_not_guessed(tmp_path):
    path = sample(tmp_path / 'pay.xlsx', rows=4)
    from openpyxl import load_workbook

    book = load_workbook(path)
    sheet = book['Payroll']
    sheet.row_dimensions[4].hidden = True
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=3, min_row=2, max_row=6), titles_from_data=True)
    sheet.add_chart(chart, 'F2')
    book.save(path)
    picture = draw(path)
    assert 'A4' not in picture.cells and 'A5' in picture.cells
    assert picture.notes == ['Hidden rows left out: 4.', '1 chart(s) on this sheet are not drawn.']


def test_a_big_sheet_is_drawn_as_tiles_with_their_own_labels(tmp_path):
    book = Workbook()
    for r in range(1, 151):
        book.active.cell(r, 1, r)
    book.save(tmp_path / 'long.xlsx')
    picture = draw(tmp_path / 'long.xlsx')
    assert len(picture.images) == 4 and picture.tiles[0].startswith('A1:A') and picture.tiles[-1].endswith(':A150')
    assert picture.notes == []
    for r in range(151, 301):
        book.active.cell(r, 1, r)
    book.save(tmp_path / 'long.xlsx')
    longer = draw(tmp_path / 'long.xlsx')
    assert longer.notes[0] == 'Rows 201-300 are not drawn; ask for them with cells.'
    assert longer.notes[1].startswith('Drawn as 4 of 5 pictures, up to row ')
    part = draw(tmp_path / 'long.xlsx', cells='A140:A150')
    assert part.tiles == ['A140:A150'] and 'A139' not in part.cells


class FakeBox:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    async def download(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def job_for(tmp_path, files, **extra):
    store = RunStore(tmp_path / 'runs')
    spec = JobSpec(id='j', module='payroll', goal='g', zip=tmp_path / 'p.zip',
                   deliverables=(Deliverable('pay.xlsx', template=tmp_path / 't.xlsx'),))  # fmt: skip
    return SimpleNamespace(main=FakeBox(files), store=store, spec=spec, look_before_finish=True,
                           looked_before_finish=False, **extra)  # fmt: skip


def test_look_at_output_returns_pictures_of_the_file_in_the_sandbox(tmp_path):
    data = sample(tmp_path / 'pay.xlsx').read_bytes()
    job = job_for(tmp_path, {'/mnt/data/outputs/pay.xlsx': data})
    items = asyncio.run(looking.look(job, '/mnt/data/outputs/pay.xlsx'))
    assert isinstance(items[0], ToolOutputText) and isinstance(items[1], ToolOutputImage) and len(items) == 2
    assert items[0].text.startswith('pay.xlsx, sheet "Payroll" (A1:D5).')
    assert items[1].image_url.startswith('data:image/png;base64,')
    assert 'Sheets in this workbook: Payroll.' in items[0].text
    events = (job.store.dir / 'events.jsonl').read_text(encoding='utf-8')
    assert '"output.looked"' in events and '"pictures": 1' in events
    # A wrong sheet or a file that is not a workbook gets a plain answer, not a failure.
    wrong = asyncio.run(looking.look(job, 'pay.xlsx', sheet='Nope'))
    assert wrong[0].text == 'pay.xlsx has no sheet "Nope". Its sheets: Payroll.'
    assert 'only .xlsx' in asyncio.run(looking.look(job, 'notes.md'))[0].text


def test_the_picture_shows_a_missing_border(tmp_path):
    """A border the agent was asked for and left out is visible in the picture, so it can see and fix it."""
    with_border = draw(sample(tmp_path / 'a.xlsx', border=True)).images[0]
    without = draw(sample(tmp_path / 'b.xlsx', border=False)).images[0]
    # The bottom edge of the header row, at the right end of column A, past its text.
    assert pixel(with_border, 155, 75) == (0, 0, 0)
    assert pixel(without, 155, 75) != (0, 0, 0)


def test_the_first_passing_save_shows_the_deliverables_once_and_only_when_switched_on(tmp_path):
    job = job_for(tmp_path, {})
    sample(job.store.outputs / 'pay.xlsx', rows=40)
    assert asyncio.run(looking.before_finish(job, {'passed': False})) == []
    shown = asyncio.run(looking.before_finish(job, {'passed': True}))
    texts = [i.text for i in shown if isinstance(i, ToolOutputText)]
    assert texts[0].startswith('The checks passed. Before you finish, look at these pictures')
    assert any(t.startswith('pay.xlsx, top, sheet "Payroll"') for t in texts)
    assert any(t.startswith('pay.xlsx, around its totals') for t in texts)  # the total is on row 43
    assert sum(isinstance(i, ToolOutputImage) for i in shown) == 2
    events = (job.store.dir / 'events.jsonl').read_text(encoding='utf-8')
    assert '"outputs.pictured"' in events and '"tokens"' in events
    assert asyncio.run(looking.before_finish(job, {'passed': True})) == []  # once per run
    off = job_for(tmp_path / 'off', {})
    off.look_before_finish = False
    sample(off.store.outputs / 'pay.xlsx')
    assert asyncio.run(looking.before_finish(off, {'passed': True})) == []
