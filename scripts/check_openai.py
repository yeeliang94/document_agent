"""Make exactly one synthetic request directly to OpenAI (no documents sent)."""

import os
import sys
from pathlib import Path

import truststore

truststore.inject_into_ssl()
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')


def main() -> int:
    official_route = (
        os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1').strip().rstrip('/') == 'https://api.openai.com/v1'
    )
    direct_key = os.getenv('OPENAI_DIRECT_API_KEY', '').strip()
    direct_model = os.getenv('OPENAI_DIRECT_MODEL', '').strip()
    if not official_route and (not direct_key or not direct_model):
        print(
            'Set separate OPENAI_DIRECT_API_KEY and OPENAI_DIRECT_MODEL for the direct OpenAI check; gateway credentials and model aliases are not reused.'
        )
        return 2
    key = direct_key or os.getenv('OPENAI_API_KEY', '')
    model = direct_model or os.getenv('OPENAI_MODEL', 'gpt-5.4-mini')
    if not key.strip():
        print('Set OPENAI_DIRECT_API_KEY or OPENAI_API_KEY in .env first.')
        return 2
    try:
        with OpenAI(api_key=key, base_url='https://api.openai.com/v1', max_retries=0, timeout=30) as client:
            response = client.responses.create(
                model=model,
                input='Reply with the word Connected.',
                max_output_tokens=64,
                store=False,
            )
        print(f'Model: {response.model}')
        print(f'Request ID: {response._request_id}')
        print(f'Output: {response.output_text}')
        return 0
    except Exception as exc:
        # Provider exception messages may contain credentials or upstream details.
        print(
            f'OpenAI check failed ({type(exc).__name__}). Check key, model, network, and certificate trust.',
            file=sys.stderr,
        )
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
