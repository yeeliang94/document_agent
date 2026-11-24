"""Timestamps, and the clean-up every stored log entry passes through: no keys, no image data, no huge strings."""

import re
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    if isinstance(value, dict):
        if value.get('kind') in ('binary', 'binary-content') or value.get('media_type', '').startswith('image/'):
            return {'kind': 'image', 'media_type': value.get('media_type'), 'data': '[image payload omitted]'}
        return {
            k: ('[redacted]' if any(x in k.lower() for x in ('api_key', 'authorization', 'access_token')) else clean(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', '[image payload omitted]', value)
        value = re.sub(r'sk-[A-Za-z0-9_-]{12,}', '[credential omitted]', value)
        return value[:24000] + (' [truncated]' if len(value) > 24000 else '')
    return value
