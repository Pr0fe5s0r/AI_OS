"""07 · Agent — reason + tools, streamed, with your own LLM.

The agent runs entirely client-side: it reasons, calls read-only markvector
tools (overview / search / list_files / structure / read_document / neighbors)
to look things up, and keeps going until it can answer. Bring any
OpenAI-compatible endpoint.

A `neighbors` hop returns `relation` and `typed`. Where `typed` is true the
relation is an authored judgement — elaborates, defines, supports, contradicts,
precedes — and the built-in prompt acts on it: it follows `elaborates` to fill a
thin answer, and on `contradicts` it reports that the collection disagrees with
itself and cites both sides instead of quietly picking a winner. Where `typed`
is false the relation is only "near", a cosine resemblance nobody vouched for.
This recipe prints the relation on every hop so you can watch that happen.

    pip install 'markvector[agent]'
    export MARKVECTOR_API_KEY=kb_live_…      # the collection
    export OPENAI_API_KEY=sk-…               # the model
    python python/07_agent.py "how are refunds handled?"
"""
from __future__ import annotations

import os
import sys

from markvector import (
    AgentAnswer,
    Markvector,
    MarkvectorError,
    Thinking,
    ToolCall,
    ToolResult,
)


def main(question: str) -> None:
    # Reads MARKVECTOR_API_KEY and MARKVECTOR_URL from the environment when the
    # constructor arguments are omitted. Keys do not belong in a file that gets
    # committed — a recipe is the easiest place in a repository to leak one.
    with Markvector() as mv:
        docs = mv.collection(os.environ.get("MARKVECTOR_COLLECTION", "cookbook"))

        agent = docs.agent(
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            base_url=os.environ.get("OPENAI_BASE_URL"),  # any compatible endpoint
            instructions="Answer in three sentences or fewer. Cite the filename.",
        )

        # Stream the chain of thought and every tool call as it happens.
        print(f"Q: {question}\n")
        for event in agent.stream(question):
            if isinstance(event, Thinking):
                print(event.text, end="", flush=True)
            elif isinstance(event, ToolCall):
                print(f"\n  → {event.name}({event.arguments})")
            elif isinstance(event, ToolResult):
                # For a `neighbors` hop the summary names the authored relations
                # that came back — "6 results (4× elaborates, 1× contradicts)" —
                # so a disagreement is visible in the transcript as it happens,
                # rather than only implied by the wording of the final answer.
                print(f"  ← {event.summary}")
            elif isinstance(event, AgentAnswer):
                print(f"\n\nANSWER:\n{event.text}")

        # Non-streaming variant, scoped to selected files:
        picked = docs.files(limit=3)
        if picked:
            result = agent.answer("What do these say about refunds?", files=picked)
            print(f"\n[{result.tool_calls} tool calls] {result.answer}")


if __name__ == "__main__":
    try:
        main(" ".join(sys.argv[1:]) or "")
    except KeyError:
        raise SystemExit("Set OPENAI_API_KEY to run the agent recipe.")
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
