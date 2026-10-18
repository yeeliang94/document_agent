import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')

LOOPBACK = ('127.0.0.1', 'localhost', '::1')


def _list(name):
    return tuple(x.strip() for x in os.getenv(name, '').split(',') if x.strip())


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv('OPENAI_API_KEY', '')
    model: str = os.getenv('OPENAI_MODEL', 'gpt-5.4-mini')
    base_url: str = os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')
    host: str = os.getenv('DI_HOST', '127.0.0.1')
    port: int = int(os.getenv('DI_PORT', '8003'))
    data_dir: Path = Path(os.getenv('DI_DATA_DIR', str(ROOT / 'data'))).expanduser().resolve()
    model_timeout: int = int(os.getenv('DI_MODEL_TIMEOUT_SECONDS', '120'))
    job_timeout: int = int(os.getenv('DI_JOB_TIMEOUT_SECONDS', '1800'))
    max_requests: int = int(os.getenv('DI_MAX_MODEL_REQUESTS', '60'))
    max_upload_mb: int = int(os.getenv('DI_MAX_UPLOAD_MB', '200'))
    max_files: int = int(os.getenv('DI_MAX_FILES', '2000'))
    context_chars: int = int(os.getenv('DI_CONTEXT_CHAR_LIMIT', '160000'))
    access_token: str = os.getenv('DI_ACCESS_TOKEN', '')
    # Host names the browser may use to reach the app. Blank: loopback names only,
    # or any name when an access token protects the API.
    allowed_hosts: tuple[str, ...] = _list('DI_ALLOWED_HOSTS')
    # Extra browser origins (e.g. https://payroll.example.com behind a TLS proxy).
    allowed_origins: tuple[str, ...] = _list('DI_ALLOWED_ORIGINS')

    def validate(self):
        if self.host not in LOOPBACK and not self.access_token:
            raise ValueError('Set DI_ACCESS_TOKEN before exposing the app beyond loopback.')
        url = urlparse(self.base_url)
        if not url.hostname or not (url.scheme == 'https' or (url.scheme == 'http' and url.hostname in LOOPBACK)):
            raise ValueError('OPENAI_BASE_URL must use HTTPS or loopback HTTP.')

    def trusted_hosts(self):
        """Host header values accepted; blocks DNS-rebinding pages from reaching a token-less API."""
        if self.allowed_hosts:
            return [*self.allowed_hosts, *LOOPBACK]
        if self.access_token:
            return ['*']
        return [*LOOPBACK]


settings = Settings()
