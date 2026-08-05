from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
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


def embed_many(texts: list[str], batch: int = 64) -> list[list[float]]:
    """Embeddings for many texts, in input order.

    Passage-level embedding turns one document into tens of vectors, so a call
    per passage would make indexing a long file tens of round trips. Batched
    here rather than at the caller so every ingest path gets it.

    Order is the contract: the caller zips the result against its passages, so
    a provider returning results out of order would attach the wrong vector to
    the wrong text. The API guarantees index order, and it is re-sorted here
    rather than trusted.
    """
    if not texts:
        return []
    client, cfg = _client()
    out: list[list[float]] = []
    for start in range(0, len(texts), batch):
        window = texts[start : start + batch]
        kwargs: dict[str, Any] = {"model": cfg.embedding_model, "input": window}
        if cfg.supports_dimensions:
            kwargs["dimensions"] = EMBED_DIM
        data = sorted(client.embeddings.create(**kwargs).data, key=lambda d: d.index)
        for entry in data:
            if len(entry.embedding) != EMBED_DIM:
                raise ValueError(
                    f"Embedding model {cfg.embedding_model!r} returned "
                    f"{len(entry.embedding)} dims; expected {EMBED_DIM}."
                )
            out.append(entry.embedding)
    return out


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


def chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    tool_choice: str = "auto",
) -> dict[str, Any]:
    """One tool-enabled turn. Returns the assistant message decomposed into
    ``{"content": str, "tool_calls": [{"id", "name", "arguments"}, ...]}``.

    ``arguments`` is the raw JSON string the model emitted (parsed by the
    caller, which owns validation). The agentic loop that drives repeated
    calls lives in ``core.assistant`` — this stays a single stateless request,
    like ``chat()``, so llm.py remains THE one place the provider is called.
    """
    client, _ = _client()
    kwargs: dict[str, Any] = {
        "model": model or os.getenv("CHAT_MODEL", "gpt-4o-mini"),
        "messages": messages,
        "tools": tools,
        # "required" forces the model to call SOMETHING before it may answer —
        # the only reliable way to stop it inventing facts it never looked up.
        "tool_choice": tool_choice,
        "temperature": temperature,
    }
    resp = client.chat.completions.create(**kwargs)
    message = resp.choices[0].message
    calls = []
    for call in message.tool_calls or []:
        calls.append(
            {"id": call.id, "name": call.function.name, "arguments": call.function.arguments or "{}"}
        )
    return {"content": message.content or "", "tool_calls": calls}


def look(image_png: bytes, instruction: str, *, model: str, temperature: float = 0.0) -> str:
    """Read a picture and answer in text. One shot, no tools, no history.

    Deliberately NOT a turn inside the navigator's conversation. Putting an
    image into that loop would drag every following turn onto a vision model,
    and a model chosen for eyes is not the one chosen for reliable tool calls —
    the loop would get worse at the thing it exists to do in order to gain
    something it needs twice a session.

    So looking is a sub-call: the picture goes out, TEXT comes back, and the
    navigator carries on reasoning in text about a page it never had to see.
    What comes back is a transcription, which is checkable — the reader is
    shown the same picture next to it.
    """
    import base64

    client, _ = _client()
    encoded = base64.b64encode(image_png).decode("ascii")
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                ],
            }
        ],
    )
    return resp.choices[0].message.content or ""


def stream_chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    tool_choice: str = "auto",
) -> Iterator[dict[str, Any]]:
    """The streaming twin of ``chat_with_tools``. Yields
    ``{"type": "text", "delta": str}`` as prose arrives, then exactly one
    ``{"type": "done", "content": str, "tool_calls": [...]}`` with the same
    shape the non-streaming call returns — so a caller can swap between them
    without changing how it reads the result.

    Tool calls arrive as index-keyed fragments across chunks (the name usually
    whole in the first, the arguments a character at a time), so they are
    reassembled here rather than in every caller. This is a blocking generator
    like the rest of the module; driving it off the event loop is the caller's
    job (see core.assistant).
    """
    client, _ = _client()
    kwargs: dict[str, Any] = {
        "model": model or os.getenv("CHAT_MODEL", "gpt-4o-mini"),
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    for chunk in client.chat.completions.create(**kwargs):
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            parts.append(delta.content)
            yield {"type": "text", "delta": delta.content}
        for call in delta.tool_calls or []:
            slot = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
            if call.id:
                slot["id"] = call.id
            if call.function is not None:
                if call.function.name:
                    slot["name"] += call.function.name
                if call.function.arguments:
                    slot["arguments"] += call.function.arguments

    yield {
        "type": "done",
        "content": "".join(parts),
        "tool_calls": [
            {**calls[i], "arguments": calls[i]["arguments"] or "{}"} for i in sorted(calls)
        ],
    }


async def astream_chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    tool_choice: str = "auto",
) -> AsyncIterator[dict[str, Any]]:
    """Async view of ``stream_chat_with_tools``.

    The provider client is synchronous, so the blocking generator is pumped on
    a worker thread and its chunks are handed back to the running loop through a
    queue. Callers that live on the event loop — the streaming API endpoint,
    the navigator when it is reporting progress — can then ``async for`` over
    the same ``{"type": "text"|"done"}`` events without a thread of their own,
    and without turning llm.py into anything other than THE provider caller.

    A failure inside the thread arrives as ``{"type": "error", "error": str}``
    rather than vanishing with the thread, so the caller can surface it.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def pump() -> None:
        try:
            for event in stream_chat_with_tools(
                messages,
                tools,
                model=model,
                temperature=temperature,
                tool_choice=tool_choice,
            ):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # surfaced to the consumer, not swallowed
            loop.call_soon_threadsafe(
                queue.put_nowait, {"type": "error", "error": f"{type(exc).__name__}: {exc}"}
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    task = loop.run_in_executor(None, pump)
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
    finally:
        await task
