"""The report a run ends with: a headline, what was done, the decisions made and what to check before sending.

The agent writes it as its final answer (JobSummary in agent.py). The app keeps it on the job as "report", uses the
headline as the job's one-line summary. (Runs before 2026-11-26 also have it as processing_summary.txt.) A run from before
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
    if not any(report.values()):
        return None
    # What a client's first run found worth keeping, offered to the person as the client's notes (plan Step 50).
    keep = _lines(answer.get('for_next_time'))[:5]
    return report | ({'for_next_time': keep} if keep else {})
