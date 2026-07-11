from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderConfig:
    """Everything the OpenAI-compatible client needs to talk to a provider.

    Every adapter (openai / nebius / openrouter) resolves env vars into one of
    these. ``core/embeddings.py`` is the only consumer.
    """

    name: str
    base_url: str
    api_key: str
    embedding_model: str
    # Only real OpenAI accepts the `dimensions` request param; gateways reject it.
    supports_dimensions: bool
