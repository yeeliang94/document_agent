"""Working out formulas a program wrote, which have no result saved in the file."""

import io
from datetime import datetime

import pytest
from openpyxl import Workbook, load_workbook

from app.formula_calc import Calculator, Unsupported


def calculator(rows, more=None):
    book = Workbook()
    book.active.title = 'Main'
    for row in rows:
        book.active.append(row)
    for title, extra in (more or {}).items():
        sheet = book.create_sheet(title)
        for row in extra:
            sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    data = out.getvalue()
    return Calculator(
        load_workbook(io.BytesIO(data), read_only=True), load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    )


DATA = [
    ['Name', 'Dept', 'Pay', 'Paid'],
    ['Ali', 'Ops', 1000, True],
    ['Bea', 'Fin', 2500.5, False],
    ['Cy', 'Ops', 300, True],
    ['Dee', 'fin', 'n/a', None],
]


@pytest.mark.parametrize(
    ('formula', 'expected'),
    [
        ('=SUM(C2:C5)', 3800.5),
        ('=SUM(C:C)', 3800.5),
        ('=C2+C3*2', 6001),
        ('=-2^2', 4),
        ('=(1+2)*3%', 0.09),
        ('=0.1+0.2', 0.3),
        ('=AVERAGE(C2:C4)', 1266.833333333333),
        ('=MIN(C2:C5)&"-"&MAX(C2:C5)', '300-2500.5'),
        ('=COUNT(C2:C5)', 3),
        ('=COUNTA(A2:A5)', 4),
        ('=ROUND(2.5,0)', 3),
        ('=ROUND(-1.125,2)', -1.13),
        ('=ROUNDDOWN(1234.567,-2)', 1200),
        ('=IF(C2>500,"high","low")', 'high'),
        ('=IF(D3,"yes","no")', 'no'),
        ('=IFERROR(C5*2,0)', 0),
        ('=SUMIF(B2:B5,"ops",C2:C5)', 1300),
        ('=SUMIF(B2:B5,"ops",C2)', 1300),  # one cell starts a sum range the size of the criteria range
        ('=COUNTIF(B2:B5,"fin")', 2),
        ('=COUNTIF(A2:A5,"?e*")', 2),
        ('=SUMIFS(C2:C5,B2:B5,"Ops",C2:C5,">500")', 1000),
        ('=COUNTIFS(D2:D5,TRUE)', 2),
        ('=SUMPRODUCT(C2:C4,C2:C4)', 1000**2 + 2500.5**2 + 300**2),
        ('=VLOOKUP("bea",A2:C5,3,FALSE)', 2500.5),
        ('=INDEX(C2:C5,MATCH("Cy",A2:A5,0))', 300),
        ('=XLOOKUP("Zed",A2:A5,C2:C5,"none")', 'none'),
        ('=LEFT(A2,2)&UPPER(RIGHT(A3))&LEN(A4)&MID("abcdef",2,3)', 'AlA2bcd'),
        ('=TRIM("  a   b ")', 'a b'),
        ('=Other!A1*2', 14),
        ("='Other'!A1+Main!C2", 1007),
        ('=YEAR(DATE(2026,14,1))', 2027),
        ('=C2/0', '#DIV/0!'),
        ('=C5+1', '#VALUE!'),
        ('=VLOOKUP("x",A2:C5,2,FALSE)', '#N/A'),
        ('=_xlfn.CONCAT(A2,"/",B2)', 'Ali/Ops'),
        ('=AND(D2,D4)', True),
    ],
)
def test_common_formulas_come_out_as_excel_shows_them(formula, expected):
    calc = calculator([*DATA, [formula]], {'Other': [[7]]})
    value = calc.result('Main', 6, 1)
    assert value == pytest.approx(expected) if isinstance(expected, float) else value == expected


def test_a_formula_reads_other_formulas_and_dates():
    calc = calculator([[datetime(2026, 1, 31), '=A1+1', '=B1*2', '=EOMONTH(A1,1)']])
    assert calc.result('Main', 1, 2) == 46054
    assert calc.result('Main', 1, 3) == 92108
    assert calc.result('Main', 1, 4) == datetime(2026, 2, 28)


@pytest.mark.parametrize(
    'formula', ['=TotalPay*2', '=SUM(Table1[Pay])', '={1,2}', '=OFFSET(A1,1,1)', '=A1', '=NOPE!A1+1']
)
def test_what_it_cannot_work_out_is_left_to_the_formula_text(formula):
    calc = calculator([[formula]])
    if formula == '=NOPE!A1+1':
        assert calc.result('Main', 1, 1) == '#REF!'  # Excel too shows #REF! for a sheet that is gone
        return
    with pytest.raises(Unsupported):
        calc.result('Main', 1, 1)


def test_a_long_running_total_is_worked_out_row_by_row():
    rows = [[1, '=A1']] + [[1, f'=B{r - 1}+A{r}'] for r in range(2, 3001)]
    calc = calculator(rows)
    assert [calc.result('Main', r, 2) for r in range(1, 3001)][-1] == 3000


@pytest.mark.parametrize(
    ('formula', 'expected'),
    [
        ('=SUMIF(A1:A2,">0",B1:B2)', '#DIV/0!'),
        ('=SUMIFS(B1:B2,A1:A2,">0")', '#DIV/0!'),
        ('=SUMPRODUCT(A1:A2,B1:B2)', '#DIV/0!'),
        ('=YEAR(C1)', None),
        ('=10^400', '#NUM!'),
    ],
)
def test_an_error_inside_a_total_shows_and_a_blank_date_is_left_to_the_formula(formula, expected):
    calc = calculator([[1, '=1/0'], [1, 5], [formula]])
    if expected is None:
        with pytest.raises(Unsupported):
            calc.result('Main', 3, 1)
    else:
        assert calc.result('Main', 3, 1) == expected


@pytest.mark.parametrize(
    ('formula', 'expected'),
    [
        ('=SUMIFS(B1:B2,C1:C2,">="&DATE(2026,2,1))', 7),
        ('=COUNTIF(C1:C2,">"&DATE(2026,1,15))', 1),
        ('=COUNT(C1:C2)', 2),
        ('=SUM(B1,D1)', 3),
        ('=IF(0.1+0.2=0.3,"OK","Off")', 'OK'),
        ('=E1', 0),
        ('=RIGHT("abc",5)', 'abc'),
        ('=main!B2', 7),
    ],
)
def test_dates_text_references_and_rounding_compare_as_excel_does(formula, expected):
    calc = calculator([[None, 3, datetime(2026, 1, 10), 'note'], [None, 7, datetime(2026, 2, 10)], [formula]])
    assert calc.result('Main', 3, 1) == expected


def test_a_big_sheet_reads_only_what_its_formulas_need():
    import time as clock

    rows = [[r, f'=A{r}/SUM(A:A)'] for r in range(1, 20001)]
    calc = calculator(rows)
    start = clock.perf_counter()
    shares = [calc.result('Main', r, 2) for r in range(1, 501)]
    assert clock.perf_counter() - start < 3 and shares[0] == pytest.approx(1 / sum(range(1, 20001)))
    calc = calculator([[r, f'=A{r}*2'] for r in range(1, 20001)])
    assert calc.result('Main', 2, 2) == 4 and calc.sheets['Main'].reached == 2


def test_an_error_saved_in_a_cell_is_an_error_to_the_formulas_that_read_it():
    calc = calculator([['#DIV/0!', 10, '=SUM(A1:B1)', '=IFERROR(A1,0)', '=ISERROR(A1)']])
    assert [calc.result('Main', 1, c) for c in (3, 4, 5)] == ['#DIV/0!', 0, True]


def test_a_workbook_that_counts_days_from_1904_is_left_to_the_formula_text():
    from openpyxl.utils.datetime import CALENDAR_MAC_1904

    book = Workbook()
    book.epoch = CALENDAR_MAC_1904
    book.active.title = 'Main'
    book.active['A1'] = '=1+1'
    out = io.BytesIO()
    book.save(out)
    data = out.getvalue()
    calc = Calculator(
        load_workbook(io.BytesIO(data), read_only=True), load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    )
    with pytest.raises(Unsupported):
        calc.result('Main', 1, 1)


def test_many_overlapping_ranges_stop_at_a_memory_budget(monkeypatch):
    import app.formula_calc as fc

    monkeypatch.setattr(fc, 'MAX_HELD', 50)
    calc = calculator([[r, f'=SUM(A$1:A{r})'] for r in range(1, 21)])
    assert calc.result('Main', 5, 2) == 15
    with pytest.raises(Unsupported):
        [calc.result('Main', r, 2) for r in range(6, 21)]
