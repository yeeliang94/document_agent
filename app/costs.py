"""What runs cost in money: their token use times the prices per million tokens on the Settings page.

Only tokens are kept. The money figure is worked out each time it is asked for, with the prices as they are now, so
a changed price changes every figure, past runs included. With no input or output price set there is no figure.

Input tokens are all the tokens sent to the model; cached tokens are the part of them the service had cached and
charges less for. Output tokens include the model's reasoning.
"""

from __future__ import annotations

PRICE_SCALE = 10_000  # prices are kept in ten-thousandths of a US dollar per million tokens
KEYS = ('input_tokens', 'cached_tokens', 'output_tokens')


def tokens(parts) -> dict:
    """The sum of usage entries: an engine record's lead, scout and review entries, or a job's runs. The engine
    names the cached part cached_input_tokens; older entries without it count as having none cached."""
    total = dict.fromkeys(KEYS, 0)
    for part in parts:
        total['input_tokens'] += part.get('input_tokens', 0) or 0
        total['cached_tokens'] += part.get('cached_input_tokens', part.get('cached_tokens', 0)) or 0
        total['output_tokens'] += part.get('output_tokens', 0) or 0
    return total


def job_tokens(job: dict) -> dict:
    """Every engine run of a job: the first, and each one an answer, a comment or a resume started. A job from
    before runs were listed one by one had a single run, kept as its usage."""
    runs = job.get('runs')
    return tokens(runs if runs else [job.get('usage') or {}])


def cost(used: dict, settings: dict) -> dict | None:
    """The money figure for these tokens with today's prices, or None when no input or output price is set. A
    cached price of 0 means cached tokens are charged as other input."""
    price_in, price_out = settings['price_input_per_million'], settings['price_output_per_million']
    if not price_in and not price_out:
        return None
    price_cached = settings['price_cached_per_million'] or price_in
    cached = min(used['cached_tokens'], used['input_tokens'])
    owed = (used['input_tokens'] - cached) * price_in + cached * price_cached + used['output_tokens'] * price_out
    return {'usd': round(owed / PRICE_SCALE / 1_000_000, 4), **used}


def job_cost(job: dict, settings: dict) -> dict | None:
    """A run's figure; None also for a run that has not asked the AI anything yet, rather than $0."""
    used = job_tokens(job)
    return cost(used, settings) if used['input_tokens'] or used['output_tokens'] else None


def playbook_cost(store, playbook_id: str) -> dict | None:
    """What a playbook's work has cost: its runs that are still kept, and its replayed tests apart (their jobs are
    removed after each check, so their tokens are tallied as each one ends). None when no price is set."""
    settings = store.settings()
    runs = store.playbook_jobs(playbook_id)
    replays = store.replay_spend(playbook_id)
    count = replays.pop('runs')
    run_cost, replay_cost = cost(tokens(map(job_tokens, runs)), settings), cost(replays, settings)
    if run_cost is None:
        return None
    return {'runs': {'count': len(runs), **run_cost}, 'replays': {'count': count, **replay_cost}}
