from __future__ import annotations

import os

from packages.adapter.base import ProviderConfig

DEFAULT_BASE_URL = "https://api.openai.com/v1"


def build() -> ProviderConfig:
    """Plain OpenAI, or ANY OpenAI-compatible endpoint via OPENAI_BASE_URL.

    Setting OPENAI_BASE_URL to a Nebius / OpenRouter / self-hosted gateway URL
    makes this adapter drive that provider directly.
    """
    base_url = os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    return ProviderConfig(
        name="openai",
        base_url=base_url,
        api_key=os.getenv("OPENAI_API_KEY", ""),
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        supports_dimensions="api.openai.com" in base_url,
    )
