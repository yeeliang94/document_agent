"""Provision verified o200k assets from an approved offline file, or explicitly download at setup."""

import argparse
import hashlib
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path

URL = 'https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken'
SHA256 = '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d'


def provision(source=None, download=False, cache=None):
    cache = Path(cache or os.getenv('TIKTOKEN_CACHE_DIR', str(Path(tempfile.gettempdir()) / 'data-gym-cache')))
    target = cache / hashlib.sha1(URL.encode()).hexdigest()
    if source:
        payload = Path(source).read_bytes()
    elif download:
        import truststore

        truststore.inject_into_ssl()
        with urllib.request.urlopen(URL, timeout=30) as response:
            payload = response.read(10_000_000)
    elif target.is_file():
        payload = target.read_bytes()
    else:
        raise ValueError(
            'No tokenizer asset. Pass --from-file with an approved offline file, or --download during setup.'
        )
    if hashlib.sha256(payload).hexdigest() != SHA256:
        raise ValueError('Tokenizer asset checksum mismatch')
    cache.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.pending')
    temporary.write_bytes(payload)
    shutil.move(temporary, target)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-file')
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--cache-dir')
    options = parser.parse_args()
    try:
        print(provision(options.from_file, options.download, options.cache_dir))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
