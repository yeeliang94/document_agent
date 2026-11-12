"""A thin wrapper around one OpenAI Code Interpreter container.

A container is a short-lived computer OpenAI runs. It is deleted after 20 idle minutes and cannot be
revived, so this wrapper never treats it as the master copy of anything: inputs are uploaded from
disk, and outputs are downloaded and checked on this machine.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

from .config import Settings

DATA_DIR = '/mnt/data'


@dataclass
class Sandbox:
    client: AsyncOpenAI
    id: str
    name: str

    @classmethod
    @contextlib.asynccontextmanager
    async def open(cls, settings: Settings, name: str, memory: str = '4g'):
        client = settings.client()
        container = await client.containers.create(
            name=name,
            memory_limit=memory,
            expires_after={'anchor': 'last_active_at', 'minutes': 20},
            extra_body=settings.sandbox_create_extra,
        )
        box = cls(client=client, id=container.id, name=name)
        try:
            yield box
        finally:
            with contextlib.suppress(Exception):
                await client.containers.delete(container.id)

    async def upload_bytes(self, filename: str, data: bytes) -> str:
        created = await self.client.containers.files.create(self.id, file=(filename, data))
        return created.path

    async def upload_file(self, local: Path, filename: str | None = None) -> str:
        return await self.upload_bytes(filename or local.name, local.read_bytes())

    async def list_files(self) -> list[dict]:
        found = []
        async for item in self.client.containers.files.list(self.id, limit=100):
            found.append({'id': item.id, 'path': item.path, 'bytes': item.bytes, 'source': item.source})
        return found

    async def download_id(self, file_id: str) -> bytes:
        response = await self.client.containers.files.content.retrieve(file_id, container_id=self.id)
        return response.read()

    async def download(self, path: str) -> bytes:
        """Download a file by its container path. Lists the container rather than trusting file citations,
        which users report are sometimes missing."""
        wanted = path if path.startswith('/') else f'{DATA_DIR}/{path}'
        for item in await self.list_files():
            if item['path'] == wanted:
                return await self.download_id(item['id'])
        raise FileNotFoundError(f'{wanted} is not in the container. Save it under {DATA_DIR}/ first.')

    async def status(self) -> str:
        return (await self.client.containers.retrieve(self.id)).status
