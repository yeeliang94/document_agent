"""Settings people change on the Settings page, stored in the database. .env keeps only secrets and the endpoint.

Each setting has a default, a type and allowed values, so a bad value is refused with a clear message.

"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Option:
    default: object
    label: str
    choices: tuple = ()
    low: int | None = None
    high: int | None = None
    pattern: str | None = None

    def check(self, value):
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
        elif self.pattern and not re.fullmatch(self.pattern, value):
            raise ValueError(f'{self.label} may hold only letters, digits, dots, dashes and underscores.')
        return value


OPTIONS = {
    'max_scouts': Option(0, 'Parallel scouts', low=0, high=8),  # the experiment found one agent as accurate and faster
    'reviewer': Option(False, 'Independent reviewer'),
    'proof': Option('row', 'Proof level', ('row', 'none')),
    'effort': Option('medium', 'Reasoning effort', ('low', 'medium', 'high')),
    'sandbox_memory': Option('4g', 'Sandbox memory', ('1g', '4g', '16g')),
    'max_turns': Option(60, 'Turn limit per run', low=10, high=200),
    'time_limit_minutes': Option(30, 'Time limit per run (minutes)', low=5, high=240),
    'token_limit_thousands': Option(3000, 'Token limit per run (thousands)', low=100, high=20000),
    'question_wait_minutes': Option(10, 'Minutes to hold the sandbox while a question waits', low=1, high=18),
    'litellm_provider': Option('', 'LiteLLM provider header', pattern=r'[A-Za-z0-9_.-]{0,64}'),
    'model_on_create': Option(False, 'Name the model when creating a sandbox (LiteLLM)'),
}


def defaults() -> dict:
    return {name: option.default for name, option in OPTIONS.items()}


def validate(changes: dict) -> dict:
    unknown = sorted(set(changes) - set(OPTIONS))
    if unknown:
        raise ValueError(f'Unknown setting(s): {", ".join(unknown)}.')
    return {name: OPTIONS[name].check(value) for name, value in changes.items()}


def describe() -> dict:
    """What the Settings page needs to draw each control."""
    return {
        name: {'label': o.label, 'default': o.default, 'choices': list(o.choices), 'low': o.low, 'high': o.high}
        for name, o in OPTIONS.items()
    }


REGION_WORDS = {'emea': 'eu', 'eu': 'eu', 'europe': 'eu', 'us': 'us', 'na': 'us', 'amer': 'us', 'apac': 'ap',
                'asia': 'ap', 'ap': 'ap', 'global': 'global'}  # fmt: skip


def _region(words) -> str | None:
    return next((REGION_WORDS[w] for w in words if w in REGION_WORDS), None)


def region_warning(base_url: str, model: str) -> str | None:
    """A sandbox lives in one region, so a model alias routed elsewhere cannot find it ("container not found").
    Warn when the endpoint's host and the model alias both name a region and the regions differ."""
    host = urlparse(base_url).hostname or ''
    endpoint = _region(re.split(r'[.\-]', host.lower()))
    alias = _region(model.lower().split('.')[:-1])  # e.g. openai.eu.gpt-6-luna -> eu
    if endpoint and alias and endpoint != alias:
        return f'The model alias points to "{alias}" but the endpoint is in "{endpoint}". Use an alias from the same region.'
    return None
