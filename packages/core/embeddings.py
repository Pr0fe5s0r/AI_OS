from __future__ import annotations

# Back-compat shim: the embedding function now lives in the single LLM client
# (packages/core/llm.py). Import from there so there is exactly one SDK client.
from packages.core.llm import EMBED_DIM, embed

__all__ = ["embed", "EMBED_DIM"]
