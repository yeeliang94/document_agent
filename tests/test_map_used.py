"""A file the document map marks "used" should feed some written row: people trust the word when they read the map.
The save lists such files for the agent to look at; it does not refuse the save, because a used file may feed only a
memo or another output without rows."""

import json

from app.engine.checks import PROVENANCE, check_provenance

FILES = [{'path': 'payroll.xlsx'}, {'path': 'draft_superseded.xlsx'}, {'path': 'fx_rates.xlsx'}]
WRITTEN = {('out.xlsx', 'Employees'): {2, 3}}
REAL = {'file': 'out.xlsx', 'sheet': 'Employees', 'rows': '2-3', 'sources': ['payroll.xlsx']}


def check(tmp_path, decisions, records=(REAL,)):
    (tmp_path / PROVENANCE).write_text(json.dumps(list(records)))
    entries = {path: {'path': path, 'decision': d, 'reason': 'x'} for path, d in decisions.items()}
    return check_provenance(tmp_path, WRITTEN, FILES, entries)


USED_DRAFT = {'payroll.xlsx': 'used', 'draft_superseded.xlsx': 'used', 'fx_rates.xlsx': 'reference'}


def test_a_used_file_no_row_cites_is_listed_without_failing_the_save(tmp_path):
    issues, counts = check(tmp_path, USED_DRAFT)
    assert issues == [] and counts['sourced'] == 2
    assert counts['used_not_cited'] == ['draft_superseded.xlsx']


def test_used_files_that_rows_cite_are_not_listed(tmp_path):
    _, counts = check(tmp_path, {**USED_DRAFT, 'draft_superseded.xlsx': 'not_relevant'})
    assert counts['used_not_cited'] == []


def test_a_record_for_rows_that_were_not_written_does_not_count_as_a_citation(tmp_path):
    ghost = [REAL, {'file': 'nowhere.xlsx', 'sheet': 'Ghost', 'rows': '2', 'sources': ['draft_superseded.xlsx']},
             {'file': 'out.xlsx', 'sheet': 'Employees', 'rows': '9999', 'sources': ['draft_superseded.xlsx']}]  # fmt: skip
    _, counts = check(tmp_path, USED_DRAFT, ghost)
    assert counts['used_not_cited'] == ['draft_superseded.xlsx']
