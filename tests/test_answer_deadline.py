"""A question must always end, one way or the other.

Found by testing: asked which elements are liquid, a streamed vectorless answer
held its connection open for over twelve minutes — steps on screen, spinner
turning, no error, no end. Nothing in the stack could stop it.

The cause was thread starvation, and it is worth writing down because it is
invisible from the outside. Every provider call runs inside asyncio.to_thread,
whose default executor has a fixed number of workers. Calls that never returned
kept their threads forever, so once they were all held, new work queued behind
them and simply never started. The event loop stayed healthy throughout —
/api/health answered in 41ms while a query had been stuck for ten minutes —
which is exactly why it took so long to see.

Two bounds now, at different levels:
  LLM_TIMEOUT_SECONDS     ends one provider call, so its thread comes back
  ANSWER_DEADLINE_SECONDS ends the whole question, whatever it is waiting on
"""

from __future__ import annotations

import asyncio

from apps.api import main


def test_a_question_is_always_bounded():
    assert main.ANSWER_DEADLINE_SECONDS > 0

    # A vision-escalated answer measured 27-49s honestly, and the paper's
    # slowest was 20s. The bound has to sit well clear of real work or it
    # starts failing questions that were merely slow.
    assert main.ANSWER_DEADLINE_SECONDS >= 120, "too tight; this would cut off real answers"
    # And it must still be short enough that a person waiting gets an ending.
    assert main.ANSWER_DEADLINE_SECONDS <= 600


def test_the_deadline_outlives_a_single_provider_call():
    """The two bounds have to nest. A whole question gets several provider
    calls, so the question's deadline must exceed one call's timeout — set the
    other way round, the deadline would fire first every time and the
    per-call timeout would never do anything."""
    from packages.core.llm import LLM_MAX_RETRIES, LLM_TIMEOUT_SECONDS

    one_call = LLM_TIMEOUT_SECONDS * (1 + LLM_MAX_RETRIES)
    assert main.ANSWER_DEADLINE_SECONDS > one_call


def test_the_reader_is_told_why_it_stopped():
    """A stream that just closes is indistinguishable from a network drop. The
    reader is owed the reason and a next step."""
    message = main._too_slow()
    assert str(int(main.ANSWER_DEADLINE_SECONDS)) in message
    assert "hybrid" in message.lower(), "say what to try instead"
    # What arrived before the cutoff is still on screen and still true.
    assert "above" in message.lower()


def test_events_are_framed_as_server_sent_events():
    """Two newlines end a frame. One, and the client waits for the rest of a
    message that already arrived."""
    framed = main._sse({"type": "step", "action": "read"})
    assert framed.startswith("data: ")
    assert framed.endswith("\n\n")


async def test_a_stalled_answer_ends_rather_than_hanging(monkeypatch):
    """The behaviour itself, on a queue that never delivers."""
    monkeypatch.setattr(main, "ANSWER_DEADLINE_SECONDS", 0.2)
    queue: asyncio.Queue = asyncio.Queue()

    async def never() -> None:
        await asyncio.sleep(3600)

    async def events():
        task = asyncio.create_task(never())
        import time as clock

        deadline = clock.monotonic() + main.ANSWER_DEADLINE_SECONDS
        try:
            while True:
                remaining = deadline - clock.monotonic()
                if remaining <= 0:
                    yield main._sse({"type": "error", "detail": main._too_slow()})
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    yield main._sse({"type": "error", "detail": main._too_slow()})
                    break
                if event is None:
                    break
                yield main._sse(event)
        finally:
            if not task.done():
                task.cancel()

    frames = [frame async for frame in events()]
    assert len(frames) == 1
    assert "error" in frames[0]
    assert frames[0].endswith("\n\n")
