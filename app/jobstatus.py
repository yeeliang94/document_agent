"""Job status groups shared by the API, the runner and (through /api/config) the browser.

Lifecycle: ready -> queued -> running -> completed | partial | waiting | paused | failed | cancelled.
`pausing` is a running job asked to stop at its next saved tool boundary.
"""

ACTIVE = frozenset({'running', 'queued', 'pausing'})
# A finished job; `partial` can still continue with a new turn.
FINISHED = frozenset({'completed', 'partial'})
STARTABLE = frozenset({'ready', 'paused', 'failed', 'cancelled', 'partial'})
PAUSABLE = frozenset({'running', 'queued'})
# Instructions are queued and delivered on the next model request.
ACCEPTS_MESSAGES = ACTIVE | {'paused', 'partial'}
# Nothing will change until the user acts, so live streams can close.
IDLE = FINISHED | {'failed', 'cancelled', 'paused', 'waiting'}

GROUPS = {
    'active': ACTIVE,
    'finished': FINISHED,
    'startable': STARTABLE,
    'pausable': PAUSABLE,
    'accepts_messages': ACCEPTS_MESSAGES,
    'idle': IDLE,
}


def as_json():
    return {name: sorted(statuses) for name, statuses in GROUPS.items()}
