from __future__ import annotations

from types import SimpleNamespace

import httpx
from markvector import (
    AgentAnswer,
    Markvector,
    Thinking,
    ToolCall,
    ToolResult,
)

# The agent is driven by an OpenAI-compatible client. Here that client is a
# scripted fake: it plays back streamed chunks in the exact shape the openai SDK
# yields (choices[0].delta.content, and .tool_calls[i] with .index/.id/.function),
# so the whole loop runs with no network and no real model.


def _chunk(content=None, tool=None):
    delta = SimpleNamespace(content=content, tool_calls=None)
    if tool is not None:
        i, cid, name, args = tool
        delta.tool_calls = [
            SimpleNamespace(
                index=i,
                id=cid,
                function=SimpleNamespace(name=name, arguments=args),
            )
        ]
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


class _FakeCompletions:
    def __init__(self, script):
        self._script = script
        self.turns = 0

    def create(self, **kwargs):
        chunks = self._script[self.turns]
        self.turns += 1
        return iter(chunks)


class _FakeLLM:
    """Turn 1: narrate, then call search. Turn 2: give the final answer."""

    def __init__(self):
        self.chat = SimpleNamespace(
            completions=_FakeCompletions(
                [
                    # turn 1 — a thought, then a tool call (arguments streamed in two fragments)
                    [
                        _chunk(content="Let me search the store. "),
                        _chunk(tool=(0, "call_1", "search", '{"query":')),
                        _chunk(tool=(0, None, None, ' "voice input"}')),
                    ],
                    # turn 2 — the grounded answer
                    [
                        _chunk(content="Brocaly dictates wherever your cursor is "),
                        _chunk(content="[item-1]."),
                    ],
                ]
            )
        )


def _search_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/search":
        return httpx.Response(
            200,
            json={
                "query": request.url.params.get("q"),
                "trace_id": "t",
                "took_ms": 3,
                "results": [
                    {
                        "item_id": "item-1",
                        "title": "Brocaly",
                        "excerpt": "dictate anywhere your [[cursor]] is",
                        "source": {"source": "upload", "locator": "brocaly.pdf"},
                        "score": 0.8,
                        "semantic": 0.6,
                        "keyword": 0.2,
                    }
                ],
            },
        )
    return httpx.Response(404, json={"detail": "unhandled"})


def _agent():
    mv = Markvector(api_key="test", transport=httpx.MockTransport(_search_handler))
    return mv.collection("default").agent(client=_FakeLLM(), model="fake")


def test_stream_emits_thoughts_tools_then_answer():
    events = list(_agent().stream("How does Brocaly handle voice input?"))
    kinds = [type(e).__name__ for e in events]

    # A thought, then the search tool call + its result, then exactly one answer.
    assert "Thinking" in kinds
    assert kinds.count("ToolCall") == 1
    assert kinds.count("ToolResult") == 1
    assert kinds[-1] == "AgentAnswer"

    call = next(e for e in events if isinstance(e, ToolCall))
    assert call.name == "search"
    assert call.arguments == {"query": "voice input"}  # reassembled from two fragments

    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.name == "search" and "1 result" in result.summary

    answer = events[-1]
    assert isinstance(answer, AgentAnswer)
    assert "cursor" in answer.text


def test_answer_returns_final_plus_transcript():
    result = _agent().answer("How does Brocaly handle voice input?")
    assert result.answer.endswith("[item-1].")
    assert result.tool_calls == 1
    # The transcript holds every step except the final answer.
    assert any(isinstance(s, Thinking) for s in result.steps)
    assert not any(isinstance(s, AgentAnswer) for s in result.steps)


def test_tool_error_is_data_not_an_exception():
    # A structure() call against a missing document 404s; the agent must turn
    # that into a tool result the model can read, not raise out of the loop.
    mv = Markvector(api_key="test", transport=httpx.MockTransport(_search_handler))
    agent = mv.collection("default").agent(client=_FakeLLM(), model="fake")
    out = agent._run_tool("structure", {"item_id": "missing"})
    assert "error" in out


def test_config_without_openai_is_a_clear_error(monkeypatch):
    # Building an agent from api_key (no client) needs openai; if it is not
    # importable the error tells you to install the extra.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "openai":
            raise ImportError("no openai")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    mv = Markvector(api_key="test", transport=httpx.MockTransport(_search_handler))
    try:
        mv.collection("default").agent(api_key="sk-x")
        raised = False
    except Exception as e:  # noqa: BLE001 - asserting the message
        raised = "markvector[agent]" in str(e)
    assert raised
