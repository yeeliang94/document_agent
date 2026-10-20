"""Oversized native sheet content stays accessible through bounded, lossless reads."""

import io

import pytest
from openpyxl import Workbook
from openpyxl.comments import Comment
from test_loop_hardening import call, job_with

from app.config import Settings
from app.state import encode


def workbook_bytes(rows, note=None):
    workbook = Workbook()
    workbook.active.title = 'Data'
    for row in rows:
        workbook.active.append(row)
    if note:
        workbook.active['A1'].comment = Comment(note, 'Reviewer')
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_oversized_single_cell_does_not_advance_the_sheet_cursor(tmp_path):
    settings = Settings(data_dir=tmp_path, tool_result_bytes=1024)
    _, _, deps = job_with(tmp_path, 'long.xlsx', workbook_bytes([['x' * 2000]]), settings)
    result = call(deps, 'read_sheet', {'path': 'inputs/long.xlsx', 'sheet': 'Data', 'end_row': 1, 'end_column': 1})
    assert 'truncated' not in result
    assert result['rows'] == []
    assert result['next_row'] == 1 and result['last_row'] == 0 and result['more_rows']
    assert 'read_cell' in result['instruction']
    assert len(encode(result).encode()) <= settings.tool_result_bytes


@pytest.mark.parametrize('field', ['value', 'note'])
def test_large_cell_content_is_retrieved_without_missing_characters(tmp_path, field):
    settings = Settings(data_dir=tmp_path, tool_result_bytes=1024)
    text = ('记录🙂\\"\n' * 300) + 'final constraint'
    payload = workbook_bytes([[text if field == 'value' else 'label']], note=text if field == 'note' else None)
    _, _, deps = job_with(tmp_path, 'long.xlsx', payload, settings)
    chunks, offset = [], 0
    while offset is not None:
        result = call(
            deps,
            'read_cell',
            {'path': 'inputs/long.xlsx', 'sheet': 'Data', 'cell': 'A1', 'field': field, 'offset': offset},
        )
        assert 'error' not in result, result
        assert 'truncated' not in result
        assert len(encode(result).encode()) <= settings.tool_result_bytes
        assert result['next_offset'] is None or result['next_offset'] > offset
        chunks.append(result['text'])
        offset = result['next_offset']
    assert len(chunks) > 1 and ''.join(chunks) == text


def test_wide_row_can_be_read_by_narrowing_columns_without_losing_cells(tmp_path):
    settings = Settings(data_dir=tmp_path, tool_result_bytes=4000)
    values = [f'Value {number}: ' + 'x' * 100 for number in range(40)]
    _, _, deps = job_with(tmp_path, 'wide.xlsx', workbook_bytes([values]), settings)
    result = call(deps, 'read_sheet', {'path': 'inputs/wide.xlsx', 'sheet': 'Data', 'end_row': 1, 'end_column': 40})
    assert 'truncated' not in result and result['next_row'] == 1
    recovered = []
    for column in range(1, 41, 4):
        result = call(
            deps,
            'read_sheet',
            {
                'path': 'inputs/wide.xlsx',
                'sheet': 'Data',
                'end_row': 1,
                'start_column': column,
                'end_column': column + 3,
            },
        )
        assert 'truncated' not in result and result['next_row'] is None
        recovered.extend(cell['value'] for cell in result['rows'][0]['cells'])
    assert recovered == values
