"""One place that turns a failure into a plain sentence and a next step. The raw text is kept for the Technical log.

Used for run failures, tool failures shown in the feed, the connection test and form errors. A failure nobody has
described yet gets a general sentence; its raw text still reaches the log.
"""

from __future__ import annotations

from dataclasses import dataclass

DAY_LIMIT = "Today's token limit is used up"


@dataclass(frozen=True)
class Plain:
    message: str  # what happened, in a sentence
    next: str  # what to do about it
    raw: str  # the original text, for the Technical log

    @property
    def sentence(self) -> str:
        return f'{self.message} {self.next}'.strip()


# (words that must all appear, lower case; what happened; what to do). The first match wins, so narrow ones go first.
KNOWN = [
    (('add openai_api_key',), 'No API key is set.', 'Add OPENAI_API_KEY to .env and restart the app.'),
    (('incorrect api key',), 'The AI service did not accept the API key.',
     'Check OPENAI_API_KEY in .env, then restart the app.'),
    (('error code: 401',), 'The AI service did not accept the API key.',
     'Check OPENAI_API_KEY in .env, then restart the app.'),
    (('invalid_api_key',), 'The AI service did not accept the API key.',
     'Check OPENAI_API_KEY in .env, then restart the app.'),
    (('container', 'not found'), "The agent's workspace (its sandbox) could not be found.",
     'Continue the run to start a fresh one from the last saved version. If it happens at the start of every run, '
     'the model alias and the endpoint may point to different regions: use an alias from the same region as '
     'OPENAI_BASE_URL (for example an "eu" alias with an EMEA address).'),
    (('container', 'expired'), "The agent's workspace (its sandbox) expired.",
     'Continue the run to start a fresh one from the last saved version.'),
    (('error code: 403',), 'The AI service refused the request.',
     'Ask the proxy team to allow the /v1/containers routes, or set the LiteLLM options on the Settings page.'),
    (('permissiondeniederror',), 'The AI service refused the request.',
     'Ask the proxy team to allow the /v1/containers routes, or set the LiteLLM options on the Settings page.'),
    (("today's token limit",), DAY_LIMIT + '. Saved work is kept.',
     'Continue tomorrow, or raise the limit on the Settings page.'),
    (('insufficient_quota',), 'The AI account has run out of credit.',
     'Add credit to the account or raise its quota, then continue.'),
    (('error code: 429',), 'The AI service is busy and asked the app to slow down.',
     'Wait a few minutes, then continue the run.'),
    (('model', 'does not exist'), 'The model named in OPENAI_MODEL is not available on this endpoint.',
     'Check the model name in .env, then restart the app.'),
    (('apiconnectionerror',), 'The app could not reach the AI service.',
     'Check the internet connection, any proxy and OPENAI_BASE_URL, then try again.'),
    (('connection error',), 'The app could not reach the AI service.',
     'Check the internet connection, any proxy and OPENAI_BASE_URL, then try again.'),
    (('stopped answering',), 'The AI service stopped answering part way through.',
     'Saved work is kept; continue the run to carry on.'),
    (('apitimeouterror',), 'The AI service took too long to answer.', 'Continue the run to carry on.'),
    (('open in another program',), 'A file in the outputs is open in another program, usually Excel.',
     'Close it, then continue the run.'),
    (('-turn limit',), 'The run used all its steps.', 'Give it more steps, or continue the run.'),
    (('-minute time limit',), 'The run reached its time limit.', 'Continue the run, or raise the limit on the Settings page.'),
    (('minute limit set on the settings page',), 'The run reached its time limit.',
     'Continue the run, or raise the limit on the Settings page.'),
    (('reached the token limit',), 'The run used all the AI usage it was allowed.',
     'Continue the run, or raise the limit on the Settings page.'),
    (('final download failed',), "The agent's last files could not be fetched from its workspace.",
     'Continue the run; it starts from the last saved version.'),
]  # fmt: skip
# The app raises these with a sentence written for the person (a file too large to upload, a skill that changed).
WRITTEN_FOR_PEOPLE = ('ValueError: ', 'FileNotFoundError: ')
UNKNOWN = ('The run stopped because of an unexpected error.',
           'Continue the run. If it happens again, send the Technical log to whoever supports this app.')  # fmt: skip


def explain(error) -> Plain | None:
    """A failure (an exception or its text) as a plain sentence and a next step, or None for no failure."""
    if error is None or error == '':
        return None
    raw = f'{type(error).__name__}: {error}' if isinstance(error, BaseException) else str(error)
    lower = raw.lower()
    for words, message, step in KNOWN:
        if all(word in lower for word in words):
            return Plain(message, step, raw)
    if raw.startswith(WRITTEN_FOR_PEOPLE):  # the app's own refusals are already a sentence with what to do
        return Plain(raw.split(': ', 1)[1], '', raw)
    return Plain(*UNKNOWN, raw)


def sentence(error) -> str | None:
    found = explain(error)
    return found.sentence if found else None


def field_errors(errors: list[dict]) -> str:
    """A form's validation errors (FastAPI's list) as one sentence, so the page never shows "Request failed (422)"."""
    lines = []
    for error in errors[:3]:
        place = [str(p) for p in error.get('loc', ()) if p not in ('body', 'query', 'path', 'form')]
        name = (place[-1] if place else 'A value').replace('_', ' ')
        name = name[:1].upper() + name[1:]
        kind, context = error.get('type', ''), error.get('ctx') or {}
        if kind == 'missing':
            lines.append(f'{name} is missing.')
        elif kind == 'string_too_long':
            lines.append(f'{name} is too long: at most {context.get("max_length")} characters.')
        elif kind == 'string_too_short':
            lines.append(f'{name} is too short.' if context.get('min_length', 0) > 1 else f'Enter {name.lower()}.')
        elif kind in ('greater_than_equal', 'greater_than'):
            lines.append(f'{name} must be at least {context.get("ge", context.get("gt"))}.')
        elif kind in ('less_than_equal', 'less_than'):
            lines.append(f'{name} must be at most {context.get("le", context.get("lt"))}.')
        elif kind.startswith(('int_', 'float_')):
            lines.append(f'{name} must be a number.')
        elif kind == 'string_pattern_mismatch':
            lines.append(f'{name} is not in the expected form.')
        else:
            lines.append(f'{name}: {error.get("msg", "not accepted")}.')
    return ' '.join(lines) or 'The form could not be read. Check what you entered and try again.'
