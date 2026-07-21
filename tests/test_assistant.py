from __future__ import annotations

import json

import packages.core.assistant as assistant_mod
from packages.core.assistant import run_tool_loop

# Checkpoint 4: the generic tool-use loop. These tests drive it with a scripted
# fake model (no real LLM) so the MECHANICS are pinned down: it runs the tool
# calls the model asks for, feeds results back, and stops on a plain-text turn.


def _script(turns: list[dict]):
    """A fake chat_with_tools that returns each queued turn in order."""
    calls = iter(turns)

    def fake(messages, tools, **kwargs):
        return next(calls)

    return fake


async def test_loop_runs_a_tool_then_answers(monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_mod,
        "chat_with_tools",
        _script(
            [
                # step 1: the model asks to search
                {
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "name": "search_events", "arguments": json.dumps({"query": "checkout"})}
                    ],
                },
                # step 2: with the result in hand, it answers in plain text
                {"content": "I found 1 event about checkout.", "tool_calls": []},
            ]
        ),
    )

    seen: list[tuple[str, dict]] = []

    async def dispatch(name, args):
        seen.append((name, args))
        return {"count": 1, "results": [{"id": "gh-1"}]}

    out = await run_tool_loop("sys", "any checkout issues?", [], dispatch)

    assert out["reply"] == "I found 1 event about checkout."
    assert seen == [("search_events", {"query": "checkout"})]
    assert out["steps"][0]["tool"] == "search_events"
    assert out["steps"][0]["result"]["count"] == 1


async def test_loop_answers_immediately_when_no_tool_needed(monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_mod,
        "chat_with_tools",
        _script([{"content": "Hello! How can I help?", "tool_calls": []}]),
    )

    async def dispatch(name, args):
        raise AssertionError("dispatch must not be called when the model asks for no tools")

    # no tools supplied -> nothing to force, so it may answer straight away
    out = await run_tool_loop("sys", "hi", [], dispatch)
    assert out["reply"] == "Hello! How can I help?"
    assert out["steps"] == []


async def test_the_model_cannot_answer_before_looking(monkeypatch) -> None:
    """The anti-confabulation guard. Asked what it had learned, the model
    invented a baseline, three colleagues who do not exist and an SLA
    statistic — having called no tools at all. The first turn is therefore
    forced to call something."""
    seen: list[str] = []

    def fake(messages, tools, **kwargs):
        seen.append(kwargs.get("tool_choice", "auto"))
        if len(seen) == 1:
            return {
                "content": "",
                "tool_calls": [{"id": "c1", "name": "get_norms", "arguments": "{}"}],
            }
        return {"content": "Typically 1.76 hours, from 5 examples.", "tool_calls": []}

    monkeypatch.setattr(assistant_mod, "chat_with_tools", fake)

    async def dispatch(name, args):
        return {"measurements": [{"what": "issue resolution", "typically": "1.76 hours"}]}

    tools = [{"type": "function", "function": {"name": "get_norms", "parameters": {}}}]
    out = await run_tool_loop("sys", "what have you learned?", tools, dispatch)

    assert seen[0] == "required", "the first turn must force a lookup"
    assert seen[1] == "auto", "after grounding it must be free to stop"
    assert out["steps"][0]["tool"] == "get_norms"
    assert "1.76" in out["reply"]


async def test_grounding_can_be_switched_off(monkeypatch) -> None:
    calls: list[str] = []

    def fake(messages, tools, **kwargs):
        calls.append(kwargs.get("tool_choice", "auto"))
        return {"content": "no lookup needed", "tool_calls": []}

    monkeypatch.setattr(assistant_mod, "chat_with_tools", fake)

    async def dispatch(name, args):
        return {}

    tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    out = await run_tool_loop("sys", "hi", tools, dispatch, ground_first=False)
    assert calls == ["auto"]
    assert out["reply"] == "no lookup needed"


async def test_a_failing_tool_does_not_kill_the_turn(monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_mod,
        "chat_with_tools",
        _script(
            [
                {
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "name": "run_action", "arguments": json.dumps({"action": "boom"})}
                    ],
                },
                {"content": "That action wasn't available, so I did nothing.", "tool_calls": []},
            ]
        ),
    )

    async def dispatch(name, args):
        raise RuntimeError("kaboom")

    out = await run_tool_loop("sys", "do the thing", [], dispatch)
    # the tool blew up but the turn still completed with a real answer
    assert "did nothing" in out["reply"]
    assert out["steps"][0]["result"]["error"].startswith("run_action failed")


async def test_loop_stops_at_the_step_cap(monkeypatch) -> None:
    """A model that keeps asking for tools forever must be capped, then made to
    answer in words rather than looping without end."""
    always_tool = {
        "content": "",
        "tool_calls": [{"id": "c", "name": "search_events", "arguments": "{}"}],
    }
    wrapup = {"content": "Here is what I found across those steps.", "tool_calls": []}
    # max_steps tool-turns, then the forced wrap-up turn
    monkeypatch.setattr(
        assistant_mod, "chat_with_tools", _script([always_tool, always_tool, wrapup])
    )

    async def dispatch(name, args):
        return {"ok": True}

    out = await run_tool_loop("sys", "loop", [], dispatch, max_steps=2)
    assert out["reply"] == "Here is what I found across those steps."
    assert len(out["steps"]) == 2  # exactly the cap, no more
