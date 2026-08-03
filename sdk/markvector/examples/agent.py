"""Run the markvector agent against a real LLM and a real store.

    pip install 'markvector[agent]'
    export MARKVECTOR_API_KEY=kb_live_…          # your workspace key
    export MARKVECTOR_URL=http://localhost:8000
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

    mv = Markvector(api_key="kb_live_4yVotNdXLTGxADrMsNVqgq8zjByGSbAs0oMn4ZIHPTk")  # MARKVECTOR_API_KEY / MARKVECTOR_URL
    agent = mv.collection("checking-collection").agent(
        api_key="v1.CmMKHHN0YXRpY2tleS1lMDBreGJhdnBxNTJwOTd6enQSIXNlcnZpY2VhY2NvdW50LWUwMHljeWt5bjhyendhNDRlcTILCP6wrswGEMDPzzM6DAj9s8aXBxCA_OuOAkACWgNlMDA.AAAAAAAAAAFX3TPuGB5p10KSS8cwpiVYwqtWfUPdUXSFnnTy4z17Vqzn8Hr2V_C-7B4BJkBtTwDviyGwibudnPbztpworoYE",
        base_url="https://api.studio.nebius.com/v1",
        model="zai-org/GLM-5.2",
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
