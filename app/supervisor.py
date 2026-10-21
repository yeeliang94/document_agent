"""Repetition detection, failure classification and a log of what each tool call changed.

Nothing here steers the agent turn by turn: hard request/time limits bound a run, and an exact
repeat loop pauses it. Which work comes next is the model's decision.
"""

from .state import identity

PROGRESS_EVENTS = {
    'evidence_inspected',
    'cursor_extended',
    'interpretation_settled',
    'question_resolved',
    'dataset_changed',
    'output_assigned',
    'validation_improved',
}
VOLATILE = {
    'revision',
    'scanned_cells',
    'duration_ms',
    'timestamp',
    'updated_at',
    'created_at',
    'request_number',
    'capability',
}


def normalized(value):
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items() if key not in VOLATILE}
    if isinstance(value, (list, tuple)):
        return [normalized(item) for item in value]
    return value


def repetition(recent, fingerprint):
    recent = [*recent, fingerprint][-30:]
    repeats = recent.count(fingerprint)
    cycle = 0
    for size in range(1, 6):
        if len(recent) >= size * 3 and recent[-size:] == recent[-2 * size : -size] == recent[-3 * size : -2 * size]:
            cycle = size
            break
    return (
        recent,
        repeats >= 3 or bool(cycle),
        repeats >= 5
        or bool(cycle > 1 and len(recent) >= cycle * 4 and recent[-cycle:] == recent[-4 * cycle : -3 * cycle]),
    )


def failure(error):
    name, detail = type(error).__name__, str(error)
    if isinstance(error, ArithmeticError):
        # decimal.InvalidOperation reads as "[<class 'decimal.ConversionSyntax'>]"; say what it means.
        detail = 'A value that must be a number is not numeric: ' + detail
    if isinstance(error, TimeoutError) or name in ('RateLimitError', 'APIConnectionError', 'APITimeoutError'):
        category, instruction = (
            'transient_service',
            'Retry at most twice within the shared allowance, or change approach.',
        )
    elif isinstance(error, FileNotFoundError):
        category, instruction = 'missing_evidence', 'Find another source or record the affected requirement as blocked.'
    elif isinstance(error, NotImplementedError):
        category, instruction = 'unsupported_format', 'Use an available capability or report the precise limitation.'
    elif 'journal' in detail.lower() or 'prepared operation' in detail.lower():
        category, instruction = 'uncertain_write', 'Reconcile the output journal before any further mutation.'
    elif isinstance(error, (ValueError, KeyError, TypeError, ArithmeticError)):
        category, instruction = 'invalid_arguments', 'Correct arguments or change the failing approach before retrying.'
    else:
        category, instruction = 'unclassified', 'Investigate the failure; continue independent work.'
    return {'error': detail, 'error_type': name, 'error_class': category, 'instruction': instruction}


def record_changes(state, actor, events):
    """Log events not seen before, with one lookup and one write however many records a tool touched.

    The log feeds Activity and the resume card; it never decides whether the run may continue.
    """
    if any(event['kind'] not in PROGRESS_EVENTS for event in events):
        raise ValueError('Unknown application progress event')
    tokens = {}
    for event in events:
        tokens.setdefault(identity(actor, event['kind'], event['key'], event['version']), event)
    if not tokens:
        return []
    known = state.existing('progress_event', tokens)
    credited = [(token, event) for token, event in tokens.items() if token not in known]
    state.put_many(
        'progress_event',
        [
            (token, {'actor': actor, 'kind': event['kind'], 'key': event['key'], 'version': event['version']})
            for token, event in credited
        ],
    )
    return [event for _, event in credited]
