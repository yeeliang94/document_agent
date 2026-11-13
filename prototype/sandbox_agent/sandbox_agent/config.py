"""Settings, read the same way as the main app.

- OPENAI_API_KEY, OPENAI_BASE_URL and OPENAI_MODEL come from the project's .env (or the real environment, which wins,
  as in the main app). So the Windows enterprise machine's proxy is configured once, for both.
- sandbox_settings.json is optional. It holds the LiteLLM extras the main app has no setting for (provider,
  model_on_create) and can override the endpoint or model for this prototype only.
- Command-line flags override both for one run.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import truststore
from dotenv import load_dotenv

from app.engine.endpoint import DEFAULT_BASE_URL, DEFAULT_MODEL, Settings

PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PROTOTYPE_DIR.parents[1]
SETTINGS_FILE = PROTOTYPE_DIR / 'sandbox_settings.json'
PLACEHOLDER_HOST = 'your-litellm-proxy'
LOOPBACK = ('127.0.0.1', 'localhost', '::1')  # as in app/config.py
PUBLIC_HOST = 'api.openai.com'


def add_endpoint_args(parser: argparse.ArgumentParser):
    """Flags that override sandbox_settings.json for one run."""
    parser.add_argument('--base-url', help='endpoint, e.g. your LiteLLM proxy; must support Code Interpreter sandboxes')
    parser.add_argument('--model', help='model name or proxy alias')
    parser.add_argument('--provider', help='LiteLLM custom-llm-provider header, e.g. openai or azure')
    parser.add_argument(
        '--model-on-create',
        action=argparse.BooleanOptionalAction,
        default=None,
        help='LiteLLM: name the model when creating sandboxes',
    )


def read_settings_file(path: Path = SETTINGS_FILE) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))  # -sig: Notepad may add a byte-order mark
    except json.JSONDecodeError as error:
        raise SystemExit(f'{path.name} is not valid JSON ({error}). Fix it or delete it.') from error
    unknown = set(data) - {'base_url', 'model', 'provider', 'model_on_create', '_comment'}
    if unknown:
        raise SystemExit(f'{path.name} has unknown keys {sorted(unknown)}.')
    for name in ('base_url', 'model', 'provider'):
        if data.get(name) is not None and not isinstance(data[name], str):
            raise SystemExit(f'{path.name}: "{name}" must be text in quotes, or null.')
    if 'model_on_create' in data and not isinstance(data['model_on_create'], bool):
        raise SystemExit(f'{path.name}: "model_on_create" must be true or false, without quotes.')
    return data


def prepare_console_and_network():
    """Windows enterprise machines: trust the Windows certificate store (as the main app does) and never crash on
    characters the console cannot show. Proxy settings such as HTTPS_PROXY are honoured by the HTTP client."""
    truststore.inject_into_ssl()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')


def check_base_url(url: str):
    """The main app's rule: HTTPS, or plain HTTP only to this machine."""
    parsed = urlparse(url)
    if not parsed.hostname or not (
        parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in LOOPBACK)
    ):
        raise SystemExit(f'The endpoint {url!r} must use HTTPS, or plain HTTP only to this machine.')


def _filled(value) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def load_settings(args: argparse.Namespace | None = None, settings_file: Path = SETTINGS_FILE) -> Settings:
    already_set = {name for name in ('OPENAI_BASE_URL', 'OPENAI_MODEL') if _filled(os.environ.get(name))}
    load_dotenv(PROJECT_ROOT / '.env')  # like the main app: a real environment variable wins over .env
    key = os.environ.get('PROXY_API_KEY') or os.environ.get('OPENAI_API_KEY', '')
    if not key:
        raise SystemExit('Add OPENAI_API_KEY to the project .env, or set PROXY_API_KEY.')
    file_values = read_settings_file(settings_file)
    chosen, origin = {}, {}
    for name, env_name in (('base_url', 'OPENAI_BASE_URL'), ('model', 'OPENAI_MODEL')):
        if _filled(os.environ.get(env_name)):
            where = 'Windows environment variable' if env_name in already_set else '.env'
            chosen[name], origin[name] = os.environ[env_name].strip(), f'{env_name} ({where})'
    for name in ('base_url', 'model', 'provider', 'model_on_create'):
        if _filled(file_values.get(name)):
            chosen[name], origin[name] = file_values[name], settings_file.name
        value = getattr(args, name, None) if args is not None else None
        if _filled(value):
            chosen[name], origin[name] = value, 'command line'
    base_url = chosen.get('base_url')
    if not base_url or PLACEHOLDER_HOST in base_url:
        # Never fall back to public OpenAI silently: on the enterprise machine that would bypass the approved proxy.
        raise SystemExit(
            'Set OPENAI_BASE_URL in the project .env to your proxy, as for the main app, '
            f'or pass --base-url {DEFAULT_BASE_URL} to use OpenAI directly.'
        )
    check_base_url(base_url)
    return Settings(
        api_key=key,
        base_url=base_url,
        model=chosen.get('model') or DEFAULT_MODEL,
        provider=chosen.get('provider') or None,
        model_on_create=chosen.get('model_on_create') is True,
        origin=origin.get('base_url', ''),
        model_origin=origin.get('model', f'default ({DEFAULT_MODEL})'),
    )


def describe(settings: Settings) -> str:
    """What a run will use and where each value came from, with a warning when it is not a company proxy."""
    lines = [
        f'Endpoint: {settings.base_url}  (from {settings.origin})',
        f'Model:    {settings.model}  (from {settings.model_origin})',
    ]
    if settings.provider or settings.model_on_create:
        lines.append(
            f'LiteLLM:  provider header {settings.provider or "none"}, model on sandbox create {settings.model_on_create}'
        )
    if urlparse(settings.base_url).hostname == PUBLIC_HOST:
        lines.append(
            'WARNING:  this is public OpenAI, not a company proxy. On the enterprise machine, set OPENAI_BASE_URL in '
            '.env to the proxy address, as for the main app.'
        )
    return '\n'.join(lines)
