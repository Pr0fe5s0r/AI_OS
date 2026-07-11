from __future__ import annotations

import os

from packages.adapter import nebius, openai, openrouter
from packages.adapter.base import ProviderConfig

_BUILDERS = {
    "openai": openai.build,
    "nebius": nebius.build,
    "openrouter": openrouter.build,
}


def resolve_provider() -> ProviderConfig:
    """Pick the provider from LLM_PROVIDER (default: openai).

    Whatever key the user supplies — OpenAI, Nebius, or OpenRouter — the matching
    adapter turns it into a single OpenAI-compatible ``ProviderConfig`` that the
    embeddings module uses. The system always uses the provided credentials.
    """
    name = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    if name not in _BUILDERS:
        raise ValueError(
            f"Unknown LLM_PROVIDER={name!r}. Choose one of {sorted(_BUILDERS)}."
        )
    return _BUILDERS[name]()
