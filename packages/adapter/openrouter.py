from __future__ import annotations

import os

from packages.adapter.base import ProviderConfig

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


def build() -> ProviderConfig:
    """OpenRouter (OpenAI-compatible).

    Note: OpenRouter is chat/completion focused. Point EMBEDDING_MODEL at an
    embeddings-capable model exposed on your account, or use the `openai` /
    `nebius` provider for the embedding step.
    """
    return ProviderConfig(
        name="openrouter",
        base_url=os.getenv("OPENROUTER_BASE_URL", DEFAULT_BASE_URL),
        api_key=os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        supports_dimensions=False,
    )
