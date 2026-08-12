"""Run the markvector agent against a real LLM and a real store.

    pip install 'markvector[agent]'
    export MARKVECTOR_API_KEY=kb_live_…          # your workspace key
    export MARKVECTOR_URL=http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me
    export OPENAI_API_KEY=sk-…                    # your LLM key
    python examples/agent.py "how does Brocaly handle voice input?"

Point base_url/model elsewhere for a local model, OpenRouter, Nebius, etc.
"""
from __future__ import annotations

import os
import sys

from markvector import AgentAnswer, Markvector, Thinking, ToolCall, ToolResult


def main() -> None:
    question = " ".join(sys.argv[1:]) or "what is the bank details?"

    # Reads MARKVECTOR_API_KEY / MARKVECTOR_URL from the environment. Keys do not
    # belong in a file that gets committed.
    mv = Markvector()
    agent = mv.collection(os.environ.get("MARKVECTOR_COLLECTION", "cookbook")).agent(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ.get("OPENAI_BASE_URL"),  # any compatible endpoint
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
    )

    for event in agent.stream(question):
        if isinstance(event, Thinking):
            print(event.text, end="", flush=True)
        elif isinstance(event, ToolCall):
            print(f"\n  \033[36m→ {event.name}({event.arguments})\033[0m")
        elif isinstance(event, ToolResult):
            print(f"  \033[2m← {event.summary}\033[0m")
        elif isinstance(event, AgentAnswer):
            print("\n\n\033[1mANSWER\033[0m\n" + event.text)


if __name__ == "__main__":
    main()
