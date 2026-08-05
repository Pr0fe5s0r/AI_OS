"""07 · Agent — reason + tools, streamed, with your own LLM.

The agent runs entirely client-side: it reasons, calls read-only markvector
tools (search / list / structure / read_document) to look things up, and keeps
going until it can answer. Bring any OpenAI-compatible endpoint.

    pip install 'markvector[agent]'
    export OPENAI_API_KEY=sk-…
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
    with Markvector(api_key="kb_live_SzftqASz9j89vL7eNQp__kWjtvuOyXwSMRB0iONmYEs", base_url="http://localhost:8000") as mv:
        
        docs = mv.collection("tn-organization-brain")

        agent = docs.agent(
            api_key="v1.CmMKHHN0YXRpY2tleS1lMDBreGJhdnBxNTJwOTd6enQSIXNlcnZpY2VhY2NvdW50LWUwMHljeWt5bjhyendhNDRlcTILCP6wrswGEMDPzzM6DAj9s8aXBxCA_OuOAkACWgNlMDA.AAAAAAAAAAFX3TPuGB5p10KSS8cwpiVYwqtWfUPdUXSFnnTy4z17Vqzn8Hr2V_C-7B4BJkBtTwDviyGwibudnPbztpworoYE",   # your LLM key
            model="moonshotai/Kimi-K2.6",
            base_url="https://api.studio.nebius.com/v1",  # any compatible endpoint
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
