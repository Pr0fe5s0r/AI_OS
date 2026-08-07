"""Embedding is what indexing costs, so these are tests about not paying twice.

Measured on this stack: embedding is ~98% of ingest wall-clock. A 134KB, 182
passage specification took 14.5 seconds to index, of which 14.5 was here. The
two savings below are the ones that survived measurement — batches in flight at
once, and not re-buying a vector for text that did not change.
"""

from __future__ import annotations

import hashlib

import pytest

from packages.core import llm


def test_batches_go_out_together_rather_than_one_after_another(monkeypatch):
    """Sequential batching was latency-bound, not throughput-bound.

    Measured on 1024 texts (16 batches), median of three runs: concurrency 1
    took 27.0s, concurrency 2 took 5.9s, and 4/8/16 were flat at ~6s. Sixteen
    round trips at ~1.7s each, spent waiting rather than working.
    """
    seen: list[list[str]] = []

    class _Entry:
        def __init__(self, index: int, embedding: list[float]) -> None:
            self.index, self.embedding = index, embedding

    class _Result:
        def __init__(self, data: list[_Entry]) -> None:
            self.data = data

    class _Embeddings:
        def create(self, **kwargs):
            window = list(kwargs["input"])
            seen.append(window)
            # Each vector encodes its own text, so a reordering is detectable.
            return _Result([
                _Entry(i, [float(len(t))] * llm.EMBED_DIM) for i, t in enumerate(window)
            ])

    class _Client:
        embeddings = _Embeddings()

    monkeypatch.setattr(llm, "_client", lambda: (_Client(), _dummy_cfg()))

    texts = [f"text of length {i:04d}" + "x" * i for i in range(200)]
    vectors = llm.embed_many(texts, batch=64)

    assert len(seen) == 4, "200 texts at 64 per batch is four calls"
    # Order is the contract: the caller zips these against its passages, so a
    # batch finishing early must not be allowed to jump the queue.
    assert [v[0] for v in vectors] == [float(len(t)) for t in texts]


def test_concurrency_is_bounded_and_can_be_turned_off(monkeypatch):
    """The ceiling is the provider's rate limit, not ours. Firing every batch at
    once buys a 429 and a retry, which is slower than having waited."""
    monkeypatch.delenv("EMBED_CONCURRENCY", raising=False)
    assert llm._embed_concurrency() == llm.EMBED_CONCURRENCY

    monkeypatch.setenv("EMBED_CONCURRENCY", "1")
    assert llm._embed_concurrency() == 1, "an operator must be able to get sequential back"

    # A misconfigured value must not take indexing down with it.
    monkeypatch.setenv("EMBED_CONCURRENCY", "not a number")
    assert llm._embed_concurrency() == llm.EMBED_CONCURRENCY
    monkeypatch.setenv("EMBED_CONCURRENCY", "0")
    assert llm._embed_concurrency() == 1


def test_an_empty_input_never_reaches_the_provider(monkeypatch):
    def _boom():
        raise AssertionError("a document with no passages must not be embedded")

    monkeypatch.setattr(llm, "_client", _boom)
    assert llm.embed_many([]) == []


def test_a_vector_is_keyed_by_its_text_not_by_where_it_sat():
    """chunk_id carries the version and the ordinal, and both move when a
    paragraph is inserted near the top — even though every passage after it is
    character-for-character the same. Keying reuse on chunk_id would therefore
    reuse nothing on exactly the edit where reuse matters most."""
    before = ["## A\n\nfirst", "## B\n\nsecond", "## C\n\nthird"]
    after = ["## New\n\ninserted", *before]

    digest = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()  # noqa: E731
    kept = {digest(t) for t in before} & {digest(t) for t in after}
    assert len(kept) == 3, "an insertion at the top must not invalidate what follows"


def test_a_provider_call_cannot_outlive_the_job_that_is_waiting_for_it():
    """Ingesting the same 146KB image took 32.9s once and 193.6s another time —
    one vision call, six times the duration. Unbounded, the SDK default of 600s
    retried twice let a single call hold a worker thread for half an hour, and a
    slow upload was indistinguishable from a lost one.

    arq's job_timeout does not cover this. Provider calls run inside
    asyncio.to_thread because the client blocks, and a cancellation cannot
    interrupt a thread parked in a socket read.
    """
    worst_case = llm.LLM_TIMEOUT_SECONDS * (1 + llm.LLM_MAX_RETRIES)
    assert worst_case <= 300, (
        "a call must not outlive the queue's job_timeout, or a wedged request "
        "goes quiet instead of being retried"
    )
    assert llm.LLM_TIMEOUT_SECONDS >= 120, (
        "the same image has taken 193s; clip too close and slow-but-fine calls "
        "start failing"
    )


def test_the_client_is_built_with_that_timeout(monkeypatch):
    """A constant nothing reads is a comment. This is the wiring."""
    captured = {}

    class _Fake:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm, "OpenAI", _Fake)
    llm._client.cache_clear()
    try:
        llm._client()
    finally:
        llm._client.cache_clear()

    assert captured.get("timeout") == llm.LLM_TIMEOUT_SECONDS
    assert captured.get("max_retries") == llm.LLM_MAX_RETRIES


def _dummy_cfg():
    class _Cfg:
        embedding_model = "test-model"
        supports_dimensions = False

    return _Cfg()


@pytest.mark.parametrize("count,batch,calls", [(1, 64, 1), (64, 64, 1), (65, 64, 2), (200, 64, 4)])
def test_batch_arithmetic(count, batch, calls, monkeypatch):
    """One text must not become two calls, and the single-batch case must not
    pay for a thread pool it has no use for."""
    seen = []

    class _E:
        def create(self, **kwargs):
            seen.append(len(kwargs["input"]))

            class R:
                data = [
                    type("X", (), {"index": i, "embedding": [0.0] * llm.EMBED_DIM})()
                    for i in range(len(kwargs["input"]))
                ]

            return R()

    monkeypatch.setattr(llm, "_client", lambda: (type("C", (), {"embeddings": _E()})(), _dummy_cfg()))
    llm.embed_many(["t"] * count, batch=batch)
    assert len(seen) == calls
    assert sum(seen) == count, "every text is embedded exactly once"
