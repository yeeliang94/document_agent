"""Settings people change on the Settings page, stored in the database. .env keeps only secrets and the endpoint.

Each setting has a default, a type and allowed values, so a bad value is refused with a clear message.
A job records the engine it was created with, so changing the engine never switches a job half way.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Option:
    default: object
    label: str
    choices: tuple = ()
    low: int | None = None
    high: int | None = None

    def check(self, name: str, value):
        if isinstance(self.default, bool):
            if not isinstance(value, bool):
                raise ValueError(f'{self.label} must be on or off.')
        elif isinstance(self.default, int):
            if isinstance(value, bool) or not isinstance(value, int) or not (self.low <= value <= self.high):
                raise ValueError(f'{self.label} must be a whole number from {self.low} to {self.high}.')
        elif self.choices and value not in self.choices:
            raise ValueError(f'{self.label} must be one of {", ".join(map(str, self.choices))}.')
        elif not isinstance(value, str):
            raise ValueError(f'{self.label} must be text.')
        return value


OPTIONS = {
    'engine': Option('classic', 'Engine', ('classic', 'sandbox')),
    'max_scouts': Option(4, 'Parallel scouts', low=0, high=8),
    'reviewer': Option(False, 'Independent reviewer'),
    'proof': Option('row', 'Proof level', ('row', 'none')),
    'effort': Option('medium', 'Reasoning effort', ('low', 'medium', 'high')),
    'sandbox_memory': Option('4g', 'Sandbox memory', ('1g', '4g', '16g')),
    'max_turns': Option(60, 'Turn limit per run', low=10, high=200),
    'question_wait_minutes': Option(10, 'Minutes to hold the sandbox while a question waits', low=1, high=18),
    'litellm_provider': Option('', 'LiteLLM provider header'),
    'model_on_create': Option(False, 'Name the model when creating a sandbox (LiteLLM)'),
}


def defaults() -> dict:
    return {name: option.default for name, option in OPTIONS.items()}


def validate(changes: dict) -> dict:
    unknown = sorted(set(changes) - set(OPTIONS))
    if unknown:
        raise ValueError(f'Unknown setting(s): {", ".join(unknown)}.')
    return {name: OPTIONS[name].check(name, value) for name, value in changes.items()}


def describe() -> dict:
    """What the Settings page needs to draw each control."""
    return {
        name: {'label': o.label, 'default': o.default, 'choices': list(o.choices), 'low': o.low, 'high': o.high}
        for name, o in OPTIONS.items()
    }
