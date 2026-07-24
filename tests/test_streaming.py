from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import packages.core.assistant as assistant_mod
from packages.core.assistant import run_tool_loop_streaming

# Part B: streaming. The mechanics are what's tested here — a fake model stands
# in for the provider, because what matters is that the loop emits honest events
# in the right order and ends with EXACTLY the payload the non-streaming loop
# would have returned. If those two diverge, a refresh shows something different
# from what the user just watched, which is the whole failure mode to avoid.


def _text(*chunks: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [{"type": "text", "delta": c} for c in chunks]
    return events + [{"type": "done", "content": "".join(chunks), "tool_calls": []}]


def _tool(name: str, arguments: str = "{}", preamble: str = "") -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if preamble:
        events.append({"type": "text", "delta": preamble})
    events.append({
        "type": "done",
        "content": preamble,
        "tool_calls": [{"id": "call-1", "name": name, "arguments": arguments}],
    })
    return events


@contextmanager
def _model(turns: list[list[dict[str, Any]]]):
    """Script the provider: one list of stream events per turn."""
    remaining = list(turns)

    def fake(messages, tools, **kwargs) -> Iterator[dict[str, Any]]:
        yield from (remaining.pop(0) if remaining else _text("(done)"))

    original = assistant_mod.stream_chat_with_tools
    assistant_mod.stream_chat_with_tools = fake  # type: ignore[assignment]
    try:
        yield
    finally:
        assistant_mod.stream_chat_with_tools = original  # type: ignore[assignment]


async def _collect(**kwargs: Any) -> list[dict[str, Any]]:
    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": name}

    defaults: dict[str, Any] = {
        "system_prompt": "sys", "user_message": "hi", "tools": [], "dispatch": dispatch,
        "ground_first": False,
    }
    defaults.update(kwargs)
    return [
        e
        async for e in run_tool_loop_streaming(
            defaults.pop("system_prompt"), defaults.pop("user_message"),
            defaults.pop("tools"), defaults.pop("dispatch"), **defaults
        )
    ]


async def test_prose_streams_then_finals_once() -> None:
    with _model([_text("Hello", " there")]):
        events = await _collect()

    assert [e["delta"] for e in events if e["type"] == "text"] == ["Hello", " there"]
    finals = [e for e in events if e["type"] == "final"]
    assert len(finals) == 1, "exactly one final, always last"
    assert events[-1]["type"] == "final"
    assert finals[0]["reply"] == "Hello there"
    assert finals[0]["steps"] == []


async def test_tool_events_bracket_the_call_and_reach_final_steps() -> None:
    with _model([_tool("list_situations"), _text("Four need you.")]):
        events = await _collect(tools=[{"function": {"name": "list_situations"}}])

    kinds = [e["type"] for e in events]
    assert kinds.index("tool_start") < kinds.index("tool_done")
    assert [e["tool"] for e in events if e["type"] == "tool_start"] == ["list_situations"]
    final = events[-1]
    assert final["reply"] == "Four need you."
    assert [s["tool"] for s in final["steps"]] == ["list_situations"]
    assert final["steps"][0]["result"] == {"ok": "list_situations"}


async def test_preamble_before_a_tool_call_is_retracted() -> None:
    """A model may narrate and THEN call a tool. We forward prose immediately —
    that's the point of streaming — so the client must be told to drop it, or
    half a sentence is stranded above the real answer."""
    with _model([_tool("get_norms", preamble="Let me check"), _text("All normal.")]):
        events = await _collect(tools=[{"function": {"name": "get_norms"}}])

    kinds = [e["type"] for e in events]
    assert "text_reset" in kinds
    # the retraction lands after the preamble and before the real answer
    assert kinds.index("text_reset") > kinds.index("text")
    assert kinds.index("text_reset") < kinds.index("tool_start")
    assert events[-1]["reply"] == "All normal."


async def test_no_reset_when_nothing_was_streamed_first() -> None:
    with _model([_tool("get_norms"), _text("All normal.")]):
        events = await _collect(tools=[{"function": {"name": "get_norms"}}])
    assert "text_reset" not in [e["type"] for e in events]


async def test_bad_tool_arguments_do_not_kill_the_turn() -> None:
    with _model([_tool("get_norms", arguments="not json"), _text("Recovered.")]):
        events = await _collect(tools=[{"function": {"name": "get_norms"}}])

    final = events[-1]
    assert final["type"] == "final"
    assert final["steps"][0]["arguments"] == {}, "unparseable args degrade to empty, not a crash"
    assert final["reply"] == "Recovered."


async def test_a_failing_tool_is_recorded_not_fatal() -> None:
    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    with _model([_tool("get_norms"), _text("Handled.")]):
        events = await _collect(tools=[{"function": {"name": "get_norms"}}], dispatch=dispatch)

    final = events[-1]
    assert final["type"] == "final"
    assert "boom" in final["steps"][0]["result"]["error"]


async def test_provider_failure_still_ends_with_a_final() -> None:
    """A stream that dies must not leave the caller waiting forever."""
    def exploding(messages, tools, **kwargs) -> Iterator[dict[str, Any]]:
        raise RuntimeError("provider down")
        yield  # pragma: no cover

    original = assistant_mod.stream_chat_with_tools
    assistant_mod.stream_chat_with_tools = exploding  # type: ignore[assignment]
    try:
        events = await _collect()
    finally:
        assistant_mod.stream_chat_with_tools = original  # type: ignore[assignment]

    assert events[-1]["type"] == "final"
    assert "provider down" in events[-1]["reply"]


async def test_step_cap_forces_a_wrap_up_instead_of_looping() -> None:
    # two tool turns exhaust the cap; the third scripted turn is the wrap-up,
    # which the loop asks for with NO tools available
    with _model([_tool("get_norms"), _tool("get_norms"), _text("Summary.")]):
        events = await _collect(tools=[{"function": {"name": "get_norms"}}], max_steps=2)

    final = events[-1]
    assert final["type"] == "final"
    assert len(final["steps"]) == 2, "never more tool rounds than the cap allows"
    assert final["reply"] == "Summary."
