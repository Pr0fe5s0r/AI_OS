from __future__ import annotations

import os

from packages.adapter.base import ProviderConfig

DEFAULT_BASE_URL = "https://api.studio.nebius.com/v1"


def build() -> ProviderConfig:
    """Nebius Token Factory / AI Studio (OpenAI-compatible)."""
    return ProviderConfig(
        name="nebius",
        base_url=os.getenv("NEBIUS_BASE_URL", DEFAULT_BASE_URL),
        api_key=os.getenv("NEBIUS_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
        # Qwen3-Embedding is Matryoshka-capable; Nebius honors the `dimensions`
        # request param, so we can pin it to 1536 to match the schema.
        embedding_model=os.getenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B"),
        supports_dimensions=True,
    )
