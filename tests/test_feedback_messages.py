"""Plan Step 48: the messages the agent and the person read say where the problem is and what to do about it."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.engine import agent, checks, service
from app.engine.jobspec import JobSpec
from app.engine.runstore import RunStore
from app.engine.sandbox import Sandbox
from app.store import Store


def events(store: RunStore) -> list[dict]:
    return [json.loads(line) for line in (store.dir / 'events.jsonl').read_text().splitlines()]


def test_a_tool_failure_names_the_tool_and_its_input(tmp_path):
    store = RunStore(tmp_path)
    ctx = SimpleNamespace(context=SimpleNamespace(store=store), tool_name='look_at_output',
                          tool_arguments='{"file": "payroll.xlsx", "sheet": "Summary"}')  # fmt: skip
    told = agent._tool_error(ctx, FileNotFoundError('payroll.xlsx'))
    assert told.startswith('look_at_output failed on the input {"file": "payroll.xlsx", "sheet": "Summary"}')
    assert 'FileNotFoundError: payroll.xlsx' in told and 'Fix the cause' in told
    [failed] = [e for e in events(store) if e['event'] == 'tool.failed']
    assert failed['tool'] == 'look_at_output' and 'Summary' in failed['arguments']
    # A long input is cut short, and says so; no input at all is not shown as an empty one.
    long = agent._tool_error(
        SimpleNamespace(context=ctx.context, tool_name='t', tool_arguments='x' * 400), OSError('e')
    )
    assert ' ...: OSError: e' in long
    empty = agent._tool_error(
        SimpleNamespace(context=ctx.context, tool_name='save_outputs', tool_arguments='{}'), OSError('e')
    )
    assert empty.startswith('save_outputs failed: OSError: e')


def test_a_missing_file_points_to_the_outputs_folder():
    class Box(Sandbox):
        async def list_files(self):
            return [{'id': 'f1', 'path': '/mnt/data/other.xlsx', 'bytes': 1, 'source': 'user'}]

    with pytest.raises(FileNotFoundError) as missing:
        asyncio.run(Box(client=None, id='c', name='n').download('/mnt/data/outputs/payroll.xlsx'))
    assert 'Outputs belong under /mnt/data/outputs/' in str(missing.value)


def test_checks_that_crash_say_the_fault_is_theirs_and_what_to_do(tmp_path, monkeypatch):
    def broken(*_):
        raise KeyError('row')

    monkeypatch.setattr(agent, 'run_checks', broken)
    store = RunStore(tmp_path)
    job = SimpleNamespace(store=store, spec=None, files=[], proof='row')
    [problem] = agent._check(job)['issues']
    assert 'fault in the checks, not in your work' in problem
    assert 'save_outputs once more' in problem and 'notes.md' in problem
    assert [e['event'] for e in events(store)] == ['check.crashed']


def test_a_duplicate_row_says_what_to_do():
    alignment = SimpleNamespace(written={5, 9}, after={5: {1: 'EMP001', 2: 100}, 9: {1: 'EMP001', 2: 100}})
    [line] = checks._duplicate_rows('Payroll', alignment)
    assert line.startswith('Payroll row 9 duplicates row 5.')
    assert 'Remove one' in line and 'say why in notes.md' in line


def _workbook(path, cells):
    book = Workbook()
    for where, value in cells.items():
        book.active[where] = value
    book.save(path)


def test_formulas_the_app_cannot_work_out_are_named_by_cell(tmp_path):
    outputs = tmp_path / 'out'
    outputs.mkdir()
    _workbook(outputs / 'odd.xlsx', {f'A{r}': '=SOMEFUTUREFUNCTION(1)' for r in range(1, 13)})
    spec = JobSpec(id='t', module='ap', goal='g', zip=tmp_path / 'p.zip', deliverables=())
    said = checks.run_checks(outputs, spec, [], proof='none')['totals_not_worked_out']
    assert said.startswith('12 formula(s) use something this machine cannot work out: odd.xlsx: Sheet!A1, ')
    assert 'Sheet!A10 ... and 2 more. Check those cells in the sandbox.' in said


def test_lists_cut_short_say_how_many_were_left_out(tmp_path):
    files = [{'path': f'f{n:02}.csv'} for n in range(12)]
    [missing] = checks.check_map({}, files)[0]
    assert 'for 12 file(s): f00.csv, ' in missing and missing.endswith('f09.csv ... and 2 more')
    # used_not_cited: twelve files marked used, none cited by a row
    entries = {f['path']: {'path': f['path'], 'decision': 'used', 'reason': 'r'} for f in files}
    (tmp_path / checks.PROVENANCE).write_text('[]', encoding='utf-8')
    _, counts = checks.check_provenance(tmp_path, {}, files, entries)
    assert len(counts['used_not_cited']) == 11 and counts['used_not_cited'][-1] == '... and 2 more'


def test_the_resume_card_says_how_many_issues_it_left_out(tmp_path):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'x', 'y')
    run = store.path(job['id']) / 'engine' / 'run-01'
    run.mkdir(parents=True)
    issues = [f'problem {n}' for n in range(40)] + ['... and 15 more.']  # as the checks cut their own list
    (run / 'run.json').write_text(
        json.dumps({'status': 'partial', 'final_check': {'issues': issues}}), encoding='utf-8'
    )
    card = service.resume_card(store, store.get(job['id']))
    assert '- problem 19\n- ... and 35 more.' in card and 'problem 20' not in card
    assert service.some_issues(['a', 'b']) == ['a', 'b']


@pytest.mark.parametrize(
    ('record', 'said'),
    [
        ({'status': 'stopped', 'cut_short': 'tokens', 'error': 'Reached the token limit of 3,000,000.'},
         'because it reached a limit (Reached the token limit of 3,000,000.)'),
        ({'status': 'stopped', 'cut_short': 'service', 'error': 'The AI service stopped answering (APIError).'},
         'because the AI service stopped answering'),
        ({'status': 'paused'}, 'because the user paused it'),
        ({'status': 'cancelled'}, 'because the user stopped it'),
        ({'status': 'failed', 'error': 'RuntimeError: disk full'}, 'because of a fault (RuntimeError: disk full)'),
    ],
)  # fmt: skip
def test_the_resume_card_says_why_the_last_run_stopped(tmp_path, record, said):
    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'x', 'y')
    run = store.path(job['id']) / 'engine' / 'run-01'
    run.mkdir(parents=True)
    (run / 'run.json').write_text(json.dumps(record), encoding='utf-8')
    card = service.resume_card(store, store.get(job['id']))
    assert f'The previous run ended as "{record["status"]}", {said}.' in card


def test_the_forced_fix_pass_carries_the_whole_check():
    check = {'passed': False, 'issues': ['Payroll row 9 duplicates row 5.'], 'template_changes': ['moved'],
             'empty_rows': ['Payroll row 7 is empty'], 'totals': [], 'looks': ['B4 shows ####'], 'summary': {}}  # fmt: skip
    told = agent.fix_pass_follow_up(check)
    head, body = told.split('\n', 1)
    assert 'Fix them, call save_outputs until it passes' in head
    assert json.loads(body) == check


def test_the_reviewer_names_a_place_and_the_agent_hears_of_the_low_doubts_left_out():
    assert 'name the file and the cell or row' in agent.REVIEWER
    assert 'Sheet!B4' in agent.Doubt.model_fields['evidence'].description
    serious = [{'issue': 'Wrong rate', 'evidence': 'payroll.xlsx Summary!C4; fx.csv says 1.08', 'severity': 'high'}]
    told = agent.review_follow_up(serious, 3)
    assert '3 low-severity doubts were left out.' in told and json.loads(told.split('\n', 1)[1]) == serious
    assert '1 low-severity doubt was left out.' in agent.review_follow_up(serious, 1)
    assert 'left out' not in agent.review_follow_up(serious, 0)


class CondensingRun:
    """A streamed run whose first model reply condenses the history, then a tool step."""

    def __init__(self, condense, final=None):
        self.condense, self.final_output, self.current_turn = condense, final, 1
        usage = SimpleNamespace(requests=1, input_tokens=1, output_tokens=1, input_tokens_details=None,
                                output_tokens_details=None)  # fmt: skip
        self.context_wrapper = SimpleNamespace(usage=usage)
        self.cancelled = None

    async def stream_events(self):
        output = [SimpleNamespace(type='compaction')] if self.condense else []
        response = SimpleNamespace(usage=SimpleNamespace(input_tokens=9, output_tokens=1), output=output)
        yield SimpleNamespace(
            type='raw_response_event', data=SimpleNamespace(type='response.completed', response=response)
        )
        yield SimpleNamespace(type='run_item_stream_event', name='other', item=None)
        if self.cancelled:
            return
        self.final_output = self.final_output or 'done'

    def cancel(self, mode):
        self.cancelled = mode

    def to_input_list(self):
        return [{'role': 'user', 'content': 'so far'}]


def test_the_agent_is_told_once_when_its_history_was_condensed(tmp_path, monkeypatch):
    runs, prompts = [CondensingRun(True), CondensingRun(False)], []

    def run_streamed(agent_, prompt, **_):
        prompts.append(prompt)
        return runs.pop(0)

    monkeypatch.setattr(agent.Runner, 'run_streamed', run_streamed)
    store = RunStore(tmp_path)
    job = agent.Job(settings=None, store=store, spec=None, main=None, zip_path=None, files=[], scout_model='',
                    effort='low', max_scouts=0, answerer=None)  # fmt: skip
    control = SimpleNamespace(stop_requested=lambda: None, new_messages=lambda: [], more_turns=lambda: 0)
    result = asyncio.run(agent._drive(None, 'go', job, store, lambda *_: None, control))
    assert result.final_output == 'done' and len(prompts) == 2
    assert prompts[1][-1]['content'] == agent.CONDENSED_NOTE and 'notes.md' in agent.CONDENSED_NOTE
    assert not job.condensed_unsaid
    said = [e['event'] for e in events(store) if e['event'].startswith('history.')]
    assert said == ['history.condensed', 'history.condensed_told']


def test_the_briefing_asks_for_notes_from_the_start_and_a_reread_after_condensing():
    assert 'Keep notes.md from the start' in agent.COORDINATOR and 'before your first save' in agent.COORDINATOR
    assert 'where each output column comes from' in agent.COORDINATOR
    assert 'When you are told the conversation was condensed, re-read notes.md' in agent.COORDINATOR
