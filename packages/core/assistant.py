from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

from packages.core.llm import chat_with_tools, stream_chat_with_tools

# Generic tool-use loop (checkpoint 4). The engine holds NO opinion about what
# any tool does: the tool specs and the dispatcher both arrive as arguments,
# exactly like detect() takes its rules and act() takes its registry. This is
# only the mechanics — send the conversation + tools to the model, run whatever
# tool calls come back, feed the results in, repeat until the model answers in
# plain text (or a safety cap is hit).
#
# A Dispatch is an async callable the caller supplies: given a tool name and
# its parsed arguments, it runs the real core function and returns a
# JSON-serializable result. Every write path a dispatcher exposes is expected
# to go through act()'s approval gate — the loop itself never touches an
# external system.

Dispatch = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

MAX_STEPS = 5  # hard cap on tool round-trips per user turn — never loop forever


async def run_tool_loop(
    system_prompt: str,
    user_message: str,
    tools: list[dict[str, Any]],
    dispatch: Dispatch,
    *,
    history: list[dict[str, Any]] | None = None,
    max_steps: int = MAX_STEPS,
    ground_first: bool = True,
) -> dict[str, Any]:
    """Drive one user turn to completion.

    Returns ``{"reply": str, "steps": [{"tool", "arguments", "result"}, ...]}``
    — the final natural-language answer plus a record of every tool the model
    actually ran (so the caller can render evidence/action artifacts and
    persist an honest trace).

    ``ground_first`` forces a tool call before the model is allowed to answer.
    Without it the model confabulates: asked what it had learned, it invented
    a baseline ("2.3 days from 120 issues"), three colleagues who do not exist
    and a fabricated SLA statistic — having called nothing. A prompt rule did
    not stop it; removing the option to answer blind does. The cost is one
    cheap lookup on a pure pleasantry, which is a price worth paying in a
    product whose entire value is being grounded in real data.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})

    steps: list[dict[str, Any]] = []

    for attempt in range(max_steps):
        # only the FIRST turn is forced; after a tool has run the model must be
        # free to stop, or it would loop calling tools forever
        choice = "required" if (ground_first and attempt == 0 and tools) else "auto"
        try:
            turn = chat_with_tools(messages, tools, tool_choice=choice)
        except Exception as exc:  # a provider/parse failure must not 500 the chat
            return {"reply": f"I hit an error reaching the model: {exc}", "steps": steps}

        tool_calls = turn["tool_calls"]
        if not tool_calls:
            return {"reply": turn["content"] or "(no answer)", "steps": steps}

        # Echo the assistant's tool-call message back, then answer each call.
        messages.append(
            {
                "role": "assistant",
                "content": turn["content"] or None,
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"]},
                    }
                    for c in tool_calls
                ],
            }
        )

        for call in tool_calls:
            try:
                args = json.loads(call["arguments"]) if call["arguments"] else {}
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            try:
                result = await dispatch(call["name"], args)
            except Exception as exc:  # a broken tool must not kill the turn
                result = {"error": f"{call['name']} failed: {exc}"}
            steps.append({"tool": call["name"], "arguments": args, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, default=str)[:6000],
                }
            )

    # Hit the step cap — ask for a plain-language wrap-up with no more tools.
    messages.append({"role": "user", "content": _WRAP_UP})
    try:
        final = chat_with_tools(messages, tools)
        return {"reply": final["content"] or "(no answer)", "steps": steps}
    except Exception as exc:
        return {"reply": f"I ran several steps but couldn't wrap up: {exc}", "steps": steps}


_WRAP_UP = (
    "Summarize what you found or did so far in plain language. Do not call any more tools."
)


# ------------------------------- streaming -------------------------------


async def _drain(factory: Callable[[], Iterator[dict[str, Any]]]) -> AsyncIterator[dict[str, Any]]:
    """Turn a BLOCKING generator into an async one without stalling the event
    loop. The provider client is synchronous, so it runs on a worker thread and
    hands items back through a queue; awaiting the queue keeps the server able
    to serve other requests while a long answer streams."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    done = object()

    def produce() -> None:
        try:
            for item in factory():
                loop.call_soon_threadsafe(queue.put_nowait, item)
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "error": str(exc)})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, done)

    task = loop.run_in_executor(None, produce)
    try:
        while True:
            item = await queue.get()
            if item is done:
                return
            yield item
    finally:
        await task


async def run_tool_loop_streaming(
    system_prompt: str,
    user_message: str,
    tools: list[dict[str, Any]],
    dispatch: Dispatch,
    *,
    history: list[dict[str, Any]] | None = None,
    max_steps: int = MAX_STEPS,
    ground_first: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """The streaming twin of :func:`run_tool_loop`, with identical semantics.

    Yields, in order:
      ``{"type": "tool_start", "tool": str}``     — a tool is about to run
      ``{"type": "tool_done",  "tool": str}``     — and has finished
      ``{"type": "text", "delta": str}``          — answer prose, as it arrives
      ``{"type": "text_reset"}``                  — discard the prose so far
      ``{"type": "final", "reply": str, "steps": [...]}``  — exactly once, last

    ``text_reset`` exists because a model may emit a preamble and THEN decide to
    call a tool. We forward prose the moment it arrives (that is the point of
    streaming) and, if the turn turns out to have been a tool call after all,
    tell the caller to drop it — rather than leaving half a sentence stranded
    above the real answer.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})

    steps: list[dict[str, Any]] = []

    for attempt in range(max_steps):
        choice = "required" if (ground_first and attempt == 0 and tools) else "auto"

        def factory(_choice: str = choice) -> Iterator[dict[str, Any]]:
            return stream_chat_with_tools(messages, tools, tool_choice=_choice)

        turn: dict[str, Any] | None = None
        streamed_text = False
        async for event in _drain(factory):
            if event["type"] == "text":
                streamed_text = True
                yield event
            elif event["type"] == "error":
                yield {"type": "final", "reply": f"I hit an error reaching the model: {event['error']}", "steps": steps}
                return
            elif event["type"] == "done":
                turn = event

        if turn is None:  # stream ended without a terminal frame
            yield {"type": "final", "reply": "(no answer)", "steps": steps}
            return

        tool_calls = turn["tool_calls"]
        if not tool_calls:
            yield {"type": "final", "reply": turn["content"] or "(no answer)", "steps": steps}
            return

        if streamed_text:
            yield {"type": "text_reset"}  # that prose was a preamble, not the answer

        messages.append(
            {
                "role": "assistant",
                "content": turn["content"] or None,
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"]},
                    }
                    for c in tool_calls
                ],
            }
        )

        for call in tool_calls:
            yield {"type": "tool_start", "tool": call["name"]}
            try:
                args = json.loads(call["arguments"]) if call["arguments"] else {}
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            try:
                result = await dispatch(call["name"], args)
            except Exception as exc:
                result = {"error": f"{call['name']} failed: {exc}"}
            steps.append({"tool": call["name"], "arguments": args, "result": result})
            yield {"type": "tool_done", "tool": call["name"]}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, default=str)[:6000],
                }
            )

    messages.append({"role": "user", "content": _WRAP_UP})

    def wrap_up() -> Iterator[dict[str, Any]]:
        return stream_chat_with_tools(messages, [], tool_choice="auto")

    reply_parts: list[str] = []
    async for event in _drain(wrap_up):
        if event["type"] == "text":
            reply_parts.append(event["delta"])
            yield event
        elif event["type"] == "error":
            yield {"type": "final", "reply": f"I ran several steps but couldn't wrap up: {event['error']}", "steps": steps}
            return
    yield {"type": "final", "reply": "".join(reply_parts) or "(no answer)", "steps": steps}
