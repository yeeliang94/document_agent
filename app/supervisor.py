"""Format-independent capability results, repetition detection and durable stall recovery.

Adapters supply opaque evidence/artifact versions. No worksheet or business identities belong here.
"""

from dataclasses import dataclass, field
from typing import Literal

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
# Looking at evidence. Useful early, but it cannot stand in for work once the discovery budget is spent.
DISCOVERY_EVENTS = {'evidence_inspected', 'cursor_extended'}
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


@dataclass
class CapabilityResult:
    artifacts: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    cursor: dict | None = None
    error_class: str | None = None
    validation: dict | None = None
    progress: list[dict] = field(default_factory=list)


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


class Supervisor:
    def __init__(self, state, actor='main', stall_turns=4, recovery_turns=2, evidence_window=6, discovery_turns=10):
        self.state, self.actor = state, actor
        self.stall_turns, self.recovery_turns, self.evidence_window = stall_turns, recovery_turns, evidence_window
        self.discovery_turns = discovery_turns

    def accept(self, result: CapabilityResult):
        return self.accept_many(result.progress)

    def event(self, kind: str, key: str, version: str, linked=False):
        return bool(self.accept_many([{'kind': kind, 'key': key, 'version': version, 'linked': linked}]))

    def accept_many(self, events):
        """Credit events not seen before, with one lookup and one write however many records a tool touched."""
        if any(event['kind'] not in PROGRESS_EVENTS for event in events):
            raise ValueError('Unknown application progress event')
        tokens = {}
        for event in events:
            tokens.setdefault(identity(self.actor, event['kind'], event['key'], event['version']), event)
        if not tokens:
            return []
        known = self.state.existing('progress_event', tokens)
        turn = self.state.get('supervision', self.actor, {'turn': 0})['turn']
        credited = [(token, event) for token, event in tokens.items() if token not in known]
        self.state.put_many(
            'progress_event',
            [
                (
                    token,
                    {
                        'actor': self.actor,
                        'kind': event['kind'],
                        'key': event['key'],
                        'version': event['version'],
                        'turn': turn,
                        'linked': event.get('linked', False) or event['kind'] != 'evidence_inspected',
                    },
                )
                for token, event in credited
            ],
        )
        return [event for _, event in credited]

    def link(self, evidence):
        for key, event in self.state.all('progress_event'):
            if event['actor'] == self.actor and event['key'] in evidence:
                self.state.put('progress_event', key, event | {'linked': True})

    def instructions(self, version):
        """A new external task revision gets a bounded attempt; resume alone never resets history."""
        clock = self.state.get('supervision', self.actor, {'turn': 0, 'last_useful': 0, 'recovery': None})
        if version > clock.get('instruction_epoch', 0):
            clock.update(instruction_epoch=version, instruction_baseline=clock['turn'], recovery=None)
            self.state.put('supervision', self.actor, clock)
            return True
        return False

    def restore_instruction(self, key, start, end, quantum=1, final=False):
        """Suspend one stall-clock tick for new instruction coverage, without resetting work/recovery.

        Coverage survives restart and application of an instruction, but a context rebuild needs a fresh
        restoration pass. Character quanta prevent shifting an overlapping range by one character from
        buying a turn each time. Credit is consumed at the next boundary, never banked for idle turns.
        """
        if end <= start:
            return
        generation = self.state.get('context', self.actor, {}).get('archive')
        key = identity(self.actor, generation, key)
        saved = self.state.get('instruction_read', key, {})
        previous = saved.get('ranges', [])
        merged = []
        for left, right in sorted([*previous, [start, end]]):
            if merged and left <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], right)
            else:
                merged.append([left, right])
        if merged == previous:
            return
        before = sum(right - left for left, right in previous)
        after = sum(right - left for left, right in merged)
        self.state.put('instruction_read', key, {'ranges': merged, 'final': final or saved.get('final', False)})
        # A legitimate short last page must also fit, even when recovery was already nearly exhausted.
        if (after + quantum - 1) // quantum > (before + quantum - 1) // quantum or (final and not saved.get('final')):
            clock = self.state.get('supervision', self.actor, {'turn': 0, 'last_useful': 0, 'recovery': None})
            clock['restoring_instructions'] = True
            self.state.put('supervision', self.actor, clock)

    def turn(self) -> tuple[Literal['continue', 'recover', 'pause'], dict]:
        clock = self.state.get('supervision', self.actor, {'turn': 0, 'last_useful': 0, 'recovery': None})
        if clock.pop('restoring_instructions', False):
            clock['restoration_turns'] = clock.get('restoration_turns', 0) + 1
            clock['notice'] = False
            self.state.put('supervision', self.actor, clock)
            return 'continue', clock
        clock['turn'] += 1
        events = [event for _, event in self.state.all('progress_event') if event['actor'] == self.actor]
        baseline = clock.get('instruction_baseline', 0)
        work = [event['turn'] + 1 for event in events if event['kind'] not in DISCOVERY_EVENTS]
        clock['last_work'] = max([*work, baseline])
        # The main agent gets a bounded allowance of reading between pieces of work. Once it is spent, or once
        # recovery has begun, only work clears the stall: rereading or citing evidence in a plan cannot.
        reading_counts = self.actor != 'main' or (
            clock['turn'] - clock['last_work'] < self.discovery_turns and clock.get('recovery') is None
        )
        clock['discovery_left'] = (
            max(0, self.discovery_turns - (clock['turn'] - clock['last_work'])) if self.actor == 'main' else None
        )
        if reading_counts:
            permanent = [event['turn'] + 1 for event in events if event['linked']]
            unlinked = [
                event['turn']
                for event in events
                if not event['linked'] and event['turn'] + 1 > max(permanent, default=0)
            ]
            useful = permanent[:]
            # A stream of unrelated new reads cannot renew the oldest unresolved evidence allowance.
            if unlinked and clock['turn'] - min(unlinked) <= self.evidence_window:
                useful.extend(turn + 1 for turn in unlinked)
            window_left = self.evidence_window - (clock['turn'] - min(unlinked)) if unlinked else None
        else:
            useful, window_left = work, 0
        clock['last_useful'] = max([*useful, baseline])
        idle = clock['turn'] - clock['last_useful']
        # Both limits end reading credit abruptly, so warn once per stretch without work, just before either does.
        left = [value for value in (clock['discovery_left'], window_left) if value is not None]
        clock['notice'] = bool(
            self.actor == 'main'
            and left
            and min(left) <= 1
            and clock.get('recovery') is None
            and clock.get('noticed_after') != clock['last_work']
        )
        if clock['notice']:
            clock['noticed_after'] = clock['last_work']
        status = 'continue'
        if idle >= self.stall_turns:
            if clock.get('recovery') is None:
                clock['recovery'] = clock['turn']
                status = 'recover'
            elif clock['turn'] - clock['recovery'] >= self.recovery_turns:
                status = 'pause'
        else:
            clock['recovery'] = None
        self.state.put('supervision', self.actor, clock)
        return status, clock
