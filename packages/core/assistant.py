from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from packages.core.llm import chat_with_tools

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
    messages.append(
        {
            "role": "user",
            "content": "Summarize what you found or did so far in plain language. Do not call any more tools.",
        }
    )
    try:
        final = chat_with_tools(messages, tools)
        return {"reply": final["content"] or "(no answer)", "steps": steps}
    except Exception as exc:
        return {"reply": f"I ran several steps but couldn't wrap up: {exc}", "steps": steps}
