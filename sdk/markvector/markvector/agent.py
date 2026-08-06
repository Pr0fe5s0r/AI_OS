from __future__ import annotations

import json
from collections.abc import Generator, Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import MarkvectorError

if TYPE_CHECKING:
    from .client import Collection
    from .models import Document

# ---------------------------------------------------------------------------
# THE AGENT — reasoning + tool execution, entirely on the client.
#
# You configure an LLM; this drives it in a loop: the model reasons, calls a
# markvector tool to look something up, reads the result, reasons again, and
# keeps going until it can answer — the same shape a person (or an assistant)
# uses to work through a question. Nothing here runs on the server: the tools
# are the ordinary markvector read calls, and the model is yours.
#
# The loop mirrors packages/core/llm.py `stream_chat_with_tools`: stream the
# content as it arrives, and reassemble tool calls from the index-keyed
# fragments the API dribbles out.
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "gpt-4o-mini"
_READ_BUDGET = 8000  # chars of a document a single read_document returns
# The overview is a MAP, so it must stay scannable: the document cards, bounded
# and trimmed, not every section summary. Past this many documents the tail is
# left to search — a card the model cannot read is no better than one it never
# got, the same reason the navigator caps its catalogue.
_MAX_OVERVIEW = 60

SYSTEM = """You are a research assistant answering questions from a private \
knowledge base, using ONLY the tools provided to look things up.

Work like this:
1. FIRST call `overview` — a card for each document describing what it is \
about. Use it to find the document that holds the answer.
2. When the overview points to a document, open it directly with \
`read_document` (or `structure` first for a long one) and read it. Do NOT \
search when the map already tells you which document to open — reading the \
right source is faster and more reliable than searching for it.
3. Search ONLY when the overview does not make the source obvious: a specific \
figure, name or identifier no card would mention, or when you genuinely cannot \
tell which document to open.
4. When a passage a search or read returned is close but not complete, call \
`neighbors` on its chunk_id to reach the passages nearest it in the store's \
graph — often the rest of the answer sits one hop away.

Ground every claim in what the tools returned — do not use outside knowledge. \
The overview is a MAP, not evidence: never cite it; cite the real document \
(item_id / filename) a claim came from. If the knowledge base does not contain \
the answer, say so plainly rather than guessing."""


# --------------------------------- events ---------------------------------


@dataclass(slots=True)
class Thinking:
    """A delta of the model's visible reasoning as it streams."""

    text: str


@dataclass(slots=True)
class ToolCall:
    """The agent is about to run a tool."""

    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    """A tool has returned — a compact summary of what it produced."""

    name: str
    summary: str


@dataclass(slots=True)
class AgentAnswer:
    """The final answer. Emitted once, at the end of a run."""

    text: str


# Thinking | ToolCall | ToolResult | AgentAnswer
Event = Thinking | ToolCall | ToolResult | AgentAnswer


@dataclass(slots=True)
class AgentResult:
    """What a non-streaming `answer()` returns: the answer plus the full
    transcript of thoughts, tool calls and results that produced it."""

    answer: str
    steps: list[Event] = field(default_factory=list)

    @property
    def tool_calls(self) -> int:
        return sum(1 for s in self.steps if isinstance(s, ToolCall))


# ------------------------------- tool schema -------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "overview",
            "description": "The collection's MAP, read FIRST: a card for each document "
            "describing what it is about, with its document id and how many passages it "
            "covers. Use it to find the document that holds the answer and open it directly "
            "with read_document — it lets you SKIP searching when the right source is "
            "obvious. It is the map, not the evidence, so never cite it.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the knowledge base by meaning and wording. Returns the "
            "best-matching passages with their document id, filename and score.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: restrict to these document ids.",
                    },
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the documents in the collection: id, filename, title, source.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "structure",
            "description": "The heading tree (table of contents) of one document: section "
            "titles, sizes and a one-line preview each. Use it to decide what to read.",
            "parameters": {
                "type": "object",
                "properties": {"item_id": {"type": "string"}},
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "The full text of one document (truncated if very long). Use after "
            "search or structure has pointed you at the right file.",
            "parameters": {
                "type": "object",
                "properties": {"item_id": {"type": "string"}},
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "neighbors",
            "description": "Given a passage's chunk_id (from a search result, or from an "
            "earlier neighbors hop), list the passages nearest it in meaning — the store's "
            "own similarity graph. Use it to explore around a promising hit: related "
            "material often sits one hop away, where a fresh search would miss it. Returns "
            "each neighbour's chunk_id (hop again from it), the document it belongs to, and "
            "how close it is.",
            "parameters": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["chunk_id"],
            },
        },
    },
]


class Agent:
    """An agent bound to one collection, driven by an OpenAI-compatible LLM.

        agent = mv.collection("default").agent(api_key="sk-…", model="gpt-4o-mini")

        # stream the chain of thought and tool use:
        for event in agent.stream("How does Brocaly handle voice input?"):
            ...

        # or just get the answer:
        result = agent.answer("How does Brocaly handle voice input?")
        print(result.answer)

    Read-only: the tools search, list and read the collection; the agent never
    writes to it.
    """

    def __init__(
        self,
        collection: Collection,
        *,
        client: Any | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        system: str | None = None,
        instructions: str | None = None,
        max_steps: int = 8,
        temperature: float = 0.0,
    ) -> None:
        self._c = collection
        self._client = client or _build_client(api_key, base_url)
        self.model = model
        self.system = _with_instructions(system or SYSTEM, instructions)
        self.max_steps = max_steps
        self.temperature = temperature

    # ------------------------------ the loop ------------------------------

    def stream(
        self,
        query: str,
        *,
        files: Iterable[str | Document] | None = None,
    ) -> Iterator[Event]:
        """Reason, call tools, and answer — yielding each step as it happens.

        Yields `Thinking` as the model narrates, `ToolCall`/`ToolResult` around
        each lookup, and finally one `AgentAnswer`.
        """
        selected_files = (
            None
            if files is None
            else {f if isinstance(f, str) else f.id for f in files}
        )
        scoped_query = query
        if selected_files is not None:
            scoped_query += (
                "\n\nFile scope: use only these item_ids: "
                + json.dumps(sorted(selected_files))
                + "."
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": scoped_query},
        ]

        for step in range(self.max_steps):
            # The last step is answer-only: stop offering tools so the model has
            # to conclude rather than loop forever.
            use_tools = step < self.max_steps - 1
            content, calls = yield from self._turn(messages, use_tools)

            if not calls:
                yield AgentAnswer(content)
                return

            # Record the assistant's tool-call turn, then run each tool.
            messages.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]},
                        }
                        for c in calls
                    ],
                }
            )
            for c in calls:
                try:
                    args = json.loads(c["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield ToolCall(c["name"], args)
                result = self._run_tool(c["name"], args, selected_files=selected_files)
                yield ToolResult(c["name"], _summarise(result))
                messages.append(
                    {"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result)}
                )

        # Ran out of steps mid-investigation: take what we have and answer.
        content, _ = yield from self._turn(messages, use_tools=False)
        yield AgentAnswer(content)

    def answer(
        self,
        query: str,
        *,
        files: Iterable[str | Document] | None = None,
    ) -> AgentResult:
        """Run to completion and return the answer plus the full transcript."""
        steps: list[Event] = []
        final = ""
        for event in self.stream(query, files=files):
            if isinstance(event, AgentAnswer):
                final = event.text
            else:
                steps.append(event)
        return AgentResult(answer=final, steps=steps)

    # ------------------------------ internals ------------------------------

    def _turn(
        self, messages: list[dict[str, Any]], use_tools: bool
    ) -> Generator[Event, None, tuple[str, list[dict[str, str]]]]:
        """One streamed model turn. Yields `Thinking` deltas and returns
        ``(content, tool_calls)`` — the reassembled result of the turn."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": True,
        }
        if use_tools:
            kwargs["tools"] = TOOLS
            kwargs["tool_choice"] = "auto"

        parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        for chunk in self._client.chat.completions.create(**kwargs):
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if getattr(delta, "content", None):
                parts.append(delta.content)
                yield Thinking(delta.content)
            for call in getattr(delta, "tool_calls", None) or []:
                slot = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                if call.id:
                    slot["id"] = call.id
                fn = getattr(call, "function", None)
                if fn is not None:
                    if fn.name:
                        slot["name"] += fn.name
                    if fn.arguments:
                        slot["arguments"] += fn.arguments

        reassembled = [
            {**calls[i], "arguments": calls[i]["arguments"] or "{}"} for i in sorted(calls)
        ]
        return "".join(parts), reassembled

    def _run_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        selected_files: set[str] | None = None,
    ) -> Any:
        """Execute a tool against the collection. Errors become data the model
        can read and recover from, never exceptions that kill the run."""
        try:
            if name == "overview":
                # A lean map: the document CARDS ("what is this file about"), so
                # the model can pick the right source and read it directly rather
                # than searching. Bounded and trimmed so it stays scannable —
                # section-level detail comes from reading or searching the file a
                # card points to. Falls back to section summaries only when a
                # collection has no cards yet.
                everything = [
                    s
                    for s in self._c.summaries()
                    if selected_files is None or s.item_id in selected_files
                ]
                cards = [s for s in everything if s.node_type == "card"]
                pool = cards or everything
                pool = sorted(pool, key=lambda s: s.covers, reverse=True)
                out: list[Any] = [
                    {
                        "item_id": s.item_id,
                        "kind": s.node_type,
                        "heading": s.heading,
                        "about": (s.text or "")[:240],
                        "covers": s.covers,
                    }
                    for s in pool[:_MAX_OVERVIEW]
                ]
                if len(pool) > _MAX_OVERVIEW:
                    out.append(
                        {
                            "note": f"{len(pool) - _MAX_OVERVIEW} more documents not "
                            "shown here — use search to reach them."
                        }
                    )
                return out
            if name == "search":
                requested = args.get("files")
                requested_files = requested if isinstance(requested, list) else None
                files = (
                    requested_files
                    if selected_files is None
                    else [
                        str(item_id)
                        for item_id in (requested_files or sorted(selected_files))
                        if str(item_id) in selected_files
                    ]
                )
                # The API interprets no item_ids as an unscoped search. An
                # explicitly empty/intersected selection must therefore stop
                # locally rather than widening to the whole collection.
                if selected_files is not None and not files:
                    return []
                hits = self._c.search(
                    str(args.get("query", "")),
                    limit=int(args.get("limit", 8) or 8),
                    files=files or None,
                )
                return [
                    {
                        "item_id": h.id,
                        # The winning passage's id, so the model can hop from a
                        # hit to its neighbours instead of only reading its file.
                        "chunk_id": h.chunk_id,
                        "title": h.title,
                        "score": round(h.score, 4),
                        "excerpt": h.clean_excerpt[:400],
                    }
                    for h in hits
                ]
            if name == "list_files":
                if selected_files == set():
                    return []
                return [
                    {
                        "item_id": d.id,
                        "filename": d.original.filename if d.original else d.source.locator,
                        "title": d.title,
                        "source": d.source.source,
                    }
                    for d in self._c.list(limit=200)
                    if selected_files is None or d.id in selected_files
                ]
            if name == "structure":
                item_id = str(args["item_id"])
                if selected_files is not None and item_id not in selected_files:
                    return {"error": f"document {item_id!r} is outside the selected file scope"}
                s = self._c.structure(item_id)
                return {
                    "item_id": s.item_id,
                    "title": s.title,
                    "nodes": s.nodes,
                    "sections": [
                        {"title": n.title, "tokens": n.tokens, "opens": n.opens}
                        for n in s.walk()
                    ],
                }
            if name == "read_document":
                item_id = str(args["item_id"])
                if selected_files is not None and item_id not in selected_files:
                    return {"error": f"document {item_id!r} is outside the selected file scope"}
                doc = self._c.get(item_id)
                body = doc.body[:_READ_BUDGET]
                out: dict[str, Any] = {"item_id": doc.id, "title": doc.title, "text": body}
                if len(doc.body) > _READ_BUDGET:
                    out["truncated"] = True
                return out
            if name == "neighbors":
                chunk_id = str(args.get("chunk_id", "") or "")
                if not chunk_id:
                    return {"error": "neighbors needs a chunk_id from a search result or a hop"}
                found = self._c.neighbors(chunk_id, limit=int(args.get("limit", 10) or 10))
                # Honour the same file scope search does: a hop must not walk out
                # of the documents the caller confined the agent to.
                return [
                    {
                        "chunk_id": n.neighbor_id,
                        "item_id": n.item_id,
                        "heading": n.heading,
                        "title": n.title,
                        "similarity": round(n.similarity, 4),
                    }
                    for n in found
                    if selected_files is None or n.item_id in selected_files
                ]
            return {"error": f"unknown tool {name!r}"}
        except MarkvectorError as exc:
            return {"error": str(exc)}


def _summarise(result: Any) -> str:
    """A one-line, human-readable gist of a tool result for the transcript."""
    if isinstance(result, list):
        return f"{len(result)} result{'' if len(result) == 1 else 's'}"
    if isinstance(result, dict):
        if "error" in result:
            return f"error: {result['error']}"
        if "sections" in result:
            return f"{result.get('nodes', len(result['sections']))} sections"
        if "text" in result:
            n = len(result["text"])
            return f"{n} chars{' (truncated)' if result.get('truncated') else ''}"
    return "ok"


def _with_instructions(system: str, instructions: str | None) -> str:
    """Add caller guidance without silently replacing grounding safeguards."""
    custom = (instructions or "").strip()
    if not custom:
        return system
    return f"{system}\n\nAdditional instructions from the caller:\n{custom}"


def _build_client(api_key: str | None, base_url: str | None) -> Any:
    """Construct an OpenAI-compatible client from config. Only needed when no
    client was passed in — so `openai` stays an optional dependency."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise MarkvectorError(
            "The agent needs an OpenAI-compatible client. Either pass client=… "
            "or install the extra: pip install 'markvector[agent]'."
        ) from exc
    return OpenAI(api_key=api_key, base_url=base_url)


__all__ = [
    "Agent",
    "AgentAnswer",
    "AgentResult",
    "Event",
    "Thinking",
    "ToolCall",
    "ToolResult",
]
