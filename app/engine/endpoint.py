"""Where the engine sends model and sandbox calls: one endpoint, one key, one shared client.

The app builds this from its own settings (.env and the Settings page); the prototype's command line builds it from
the same .env plus sandbox_settings.json and flags.
"""

from dataclasses import dataclass, field
from functools import cached_property

from agents.models.openai_responses import OpenAIResponsesModel
from openai import AsyncOpenAI

DEFAULT_MODEL = 'gpt-6-luna'
DEFAULT_BASE_URL = 'https://api.openai.com/v1'


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    provider: str | None = None  # LiteLLM proxies: sent as the custom-llm-provider header
    model_on_create: bool = False  # LiteLLM proxies with model_list credentials: name the model when creating a sandbox
    origin: str = field(default='', compare=False)  # where base_url came from, for display
    model_origin: str = field(default='', compare=False)

    @cached_property
    def _client(self) -> AsyncOpenAI:
        headers = {'custom-llm-provider': self.provider} if self.provider else None
        return AsyncOpenAI(
            api_key=self.api_key, base_url=self.base_url, default_headers=headers, max_retries=3, timeout=600
        )

    def client(self) -> AsyncOpenAI:
        """One shared client for model calls and sandbox calls, so both always reach the same endpoint."""
        return self._client

    def model_for(self, name: str | None = None) -> OpenAIResponsesModel:
        return OpenAIResponsesModel(model=name or self.model, openai_client=self._client)

    @property
    def sandbox_create_extra(self) -> dict | None:
        return {'model': self.model} if self.model_on_create else None
