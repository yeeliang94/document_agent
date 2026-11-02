"""Controlled context estimates for tests that deliberately exhaust the budget."""

import pytest


@pytest.fixture
def byte_context_estimator(monkeypatch):
    # Exercise the supported fallback without depending on a machine's tokenizer cache.
    monkeypatch.setattr(
        'app.context.estimate_text', lambda text: (len(text.encode('utf-8')) + 32, 'conservative_byte_estimate')
    )
