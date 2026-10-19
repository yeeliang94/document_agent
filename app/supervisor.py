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
    elif isinstance(error, (ValueError, KeyError, TypeError)):
        category, instruction = 'invalid_arguments', 'Correct arguments or change the failing approach before retrying.'
    else:
        category, instruction = 'unclassified', 'Investigate the failure; continue independent work.'
    return {'error': detail, 'error_type': name, 'error_class': category, 'instruction': instruction}


class Supervisor:
    def __init__(self, state, actor='main', stall_turns=4, recovery_turns=2, evidence_window=6):
        self.state, self.actor = state, actor
        self.stall_turns, self.recovery_turns, self.evidence_window = stall_turns, recovery_turns, evidence_window

    def accept(self, result: CapabilityResult):
        credited = []
        for event in result.progress:
            if self.event(event['kind'], event['key'], event['version'], event.get('linked', False)):
                credited.append(event)
        return credited

    def event(self, kind: str, key: str, version: str, linked=False):
        if kind not in PROGRESS_EVENTS:
            raise ValueError('Unknown application progress event')
        token = identity(self.actor, kind, key, version)
        if self.state.get('progress_event', token):
            return False
        clock = self.state.get('supervision', self.actor, {'turn': 0})
        self.state.put(
            'progress_event',
            token,
            {
                'actor': self.actor,
                'kind': kind,
                'key': key,
                'version': version,
                'turn': clock['turn'],
                'linked': linked or kind != 'evidence_inspected',
            },
        )
        return True

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

    def turn(self) -> tuple[Literal['continue', 'recover', 'pause'], dict]:
        clock = self.state.get('supervision', self.actor, {'turn': 0, 'last_useful': 0, 'recovery': None})
        clock['turn'] += 1
        events = [event for _, event in self.state.all('progress_event') if event['actor'] == self.actor]
        permanent = [event['turn'] + 1 for event in events if event['linked']]
        unlinked = [
            event['turn'] for event in events if not event['linked'] and event['turn'] + 1 > max(permanent, default=0)
        ]
        useful = permanent[:]
        # A stream of unrelated new reads cannot renew the oldest unresolved evidence allowance.
        if unlinked and clock['turn'] - min(unlinked) <= self.evidence_window:
            useful.extend(turn + 1 for turn in unlinked)
        clock['last_useful'] = max([*useful, clock.get('instruction_baseline', 0)])
        idle = clock['turn'] - clock['last_useful']
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
