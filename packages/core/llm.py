from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from openai import OpenAI

from packages.adapter import ProviderConfig, resolve_provider

# THE single OpenAI-compatible client. Every embedding and chat call in the whole
# system goes through here — base_url / api_key come from env via the adapter, so
# it works against OpenAI, Nebius, a local server, or any compatible endpoint.

EMBED_DIM = 1536


@lru_cache(maxsize=1)
def _client() -> tuple[OpenAI, ProviderConfig]:
    cfg = resolve_provider()
    return OpenAI(base_url=cfg.base_url, api_key=cfg.api_key), cfg


def embed(text: str) -> list[float]:
    """Return a 1536-dim embedding for ``text``."""
    client, cfg = _client()
    kwargs: dict[str, Any] = {"model": cfg.embedding_model, "input": text}
    if cfg.supports_dimensions:
        kwargs["dimensions"] = EMBED_DIM
    vector = client.embeddings.create(**kwargs).data[0].embedding
    if len(vector) != EMBED_DIM:
        raise ValueError(
            f"Embedding model {cfg.embedding_model!r} returned {len(vector)} dims; "
            f"expected {EMBED_DIM}. Set EMBEDDING_MODEL to a {EMBED_DIM}-dim model."
        )
    return vector


def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.0,
) -> str:
    """Chat completion. ``response_format`` enables structured/JSON output.

    Returns the assistant message content (string). Used by the detection
    runtime in later checkpoints — kept generic here.
    """
    client, _ = _client()
    kwargs: dict[str, Any] = {
        "model": model or os.getenv("CHAT_MODEL", "gpt-4o-mini"),
        "messages": messages,
        "temperature": temperature,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""
