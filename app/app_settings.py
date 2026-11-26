"""Settings people change on the Settings page, stored in the database. .env keeps only secrets and the endpoint.

Each setting has a default, a type and allowed values, so a bad value is refused with a clear message.

"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


class SettingError(ValueError):
    """A value the Settings page cannot keep. field names the setting, so the page can show the sentence beside it."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class Option:
    default: object
    label: str
    choices: tuple = ()
    low: int | None = None
    high: int | None = None
    pattern: str | None = None

    def check(self, value):
        """The value, or a SettingError whose sentence reads well beside the field (it does not repeat the label)."""
        if isinstance(self.default, bool):
            if not isinstance(value, bool):
                raise SettingError('Choose on or off.')
        elif isinstance(self.default, int):
            if isinstance(value, bool) or not isinstance(value, int) or not (self.low <= value <= self.high):
                raise SettingError(f'Choose a whole number from {self.low} to {self.high}.')
        elif self.choices and value not in self.choices:
            raise SettingError(f'Choose one of {", ".join(map(str, self.choices))}.')
        elif not isinstance(value, str):
            raise SettingError('Enter text.')
        elif self.pattern and not re.fullmatch(self.pattern, value):
            raise SettingError('Use only letters, digits, dots, dashes and underscores.')
        return value


OPTIONS = {
    'max_scouts': Option(0, 'Parallel scouts', low=0, high=8),  # the experiment found one agent as accurate and faster
    'reviewer': Option(False, 'Independent reviewer'),
    'proof': Option('row', 'Proof level', ('row', 'none')),
    'effort': Option('medium', 'Reasoning effort', ('low', 'medium', 'high')),
    'sandbox_memory': Option('4g', 'Sandbox memory', ('1g', '4g', '16g')),
    'max_turns': Option(60, 'Turn limit per run', low=10, high=200),
    'more_steps': Option(20, 'Steps added by "More steps"', low=5, high=100),
    'time_limit_minutes': Option(30, 'Time limit per run (minutes)', low=5, high=240),
    'token_limit_thousands': Option(3000, 'Token limit per run (thousands)', low=100, high=20000),
    # Every run, replayed test included, counts towards the day. 0 means no limit.
    'daily_token_limit_thousands': Option(30000, 'Token limit per day, all runs (thousands)', low=0, high=500000),
    # OpenAI condenses the older history once a request reaches this size; tried live at 20 (see PLAN-IMPROVEMENTS).
    'condense_at_thousands': Option(200, 'Condense history at (thousands of tokens, 0 for never)', low=0, high=1000),
    # The first save that passes also shows the agent pictures of its deliverables (about 1,000-2,000 tokens each).
    'look_before_finish': Option(True, 'Show the agent its files before it finishes'),
    # Runs going side by side, replayed tests included; the rest wait in line. Read as each run ends (app/runtime.py).
    'runs_at_once': Option(2, 'Runs at once', low=1, high=6),
    'versions_kept': Option(20, 'Versions kept per run', low=1, high=100),  # a copy of the outputs at each save
    # Questions the agent may ask over the whole job; each one waits for your answer.
    'max_questions': Option(3, 'Questions it may ask per job', low=0, high=10),
    'question_wait_minutes': Option(10, 'Minutes to hold the sandbox while a question waits', low=1, high=18),
    'replay_tests': Option('large', 'Check playbook changes against past corrections', ('never', 'large', 'every')),
    'litellm_provider': Option('', 'LiteLLM provider header', pattern=r'[A-Za-z0-9_.-]{0,64}'),
    'model_on_create': Option(False, 'Name the model when creating a sandbox (LiteLLM)'),
    # Speak instead of type: live words while speaking, or the words after the person stops (see app/voice.py).
    'voice_mode': Option('live', 'Voice words appear', ('live', 'after')),
    'voice_live_model': Option('gpt-live-transcribe', 'Live voice model', pattern=r'[A-Za-z0-9_.-]{1,128}'),
    'voice_file_model': Option('gpt-transcribe', 'Recording voice model', pattern=r'[A-Za-z0-9_.-]{1,128}'),
    'voice_max_minutes': Option(2, 'Longest recording (minutes)', low=1, high=10),
    # Prices per million tokens, in ten-thousandths of a US dollar (the page shows dollars), for the money figure
    # of each run and playbook (app/costs.py). 0 for input and output means no figure; 0 for cached means cached
    # tokens cost what other input does.
    'price_input_per_million': Option(0, 'Price per million input tokens', low=0, high=10_000_000),
    'price_cached_per_million': Option(0, 'Price per million cached input tokens', low=0, high=10_000_000),
    'price_output_per_million': Option(0, 'Price per million output tokens', low=0, high=10_000_000),
    # A notification and a mark on the browser tab when a run asks, finishes or stops while you look elsewhere.
    'notify': Option(True, 'Tell me when a run asks, finishes or stops'),
}


def defaults() -> dict:
    return {name: option.default for name, option in OPTIONS.items()}


def validate(changes: dict) -> dict:
    unknown = sorted(set(changes) - set(OPTIONS))
    if unknown:
        raise SettingError(f'Unknown setting(s): {", ".join(unknown)}.')
    checked = {}
    for name, value in changes.items():
        try:
            checked[name] = OPTIONS[name].check(value)
        except SettingError as error:
            raise SettingError(str(error), name) from None
    return checked


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
