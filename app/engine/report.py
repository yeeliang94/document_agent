"""The report a run ends with: a headline, what was done, the decisions made and what to check before sending.

The agent writes it as its final answer (JobSummary in agent.py). The app keeps it on the job as "report", uses the
headline as the job's one-line summary, and writes the same text to processing_summary.txt. A run from before
reports ended with one paragraph, "summary": it is read as a headline with nothing else.
"""

from __future__ import annotations

PARTS = (('done', 'What was done'), ('decisions', 'Decisions made'), ('to_check', 'Check before sending'))


def _lines(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [' '.join(str(v).split()) for v in value or () if str(v).strip()]


def from_answer(answer: dict | None) -> dict | None:
    """The report in the agent's final answer, or None when the run ended without one."""
    if not answer:
        return None
    headline = ' '.join(str(answer.get('headline') or answer.get('summary') or '').split())
    report = {'headline': headline, **{key: _lines(answer.get(key)) for key, _ in PARTS}}
    return report if any(report.values()) else None


def as_text(report: dict | None, open_questions=(), extra: str = '') -> str:
    """The report as plain text, for processing_summary.txt. extra: what the app adds (why a run stopped)."""
    report = report or {}
    blocks = [part for part in (report.get('headline', ''), extra.strip()) if part]
    for key, title in (*PARTS, ('open_questions', 'Still open')):
        lines = _lines(open_questions) if key == 'open_questions' else report.get(key) or []
        if lines:
            blocks.append(title + ':\n' + '\n'.join(f'- {line}' for line in lines))
    return '\n\n'.join(blocks)
