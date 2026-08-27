# OpenAI streaming events — what the lib emits, and how to arrange them for a full chat

This is about the **raw events the OpenAI SDK gives you when `stream=True`** — nothing
framework-specific. The goal is the thing your friend is hitting: **stop "thinking"
and "tool calls" from leaking into the visible reply.**

The one-sentence cause: *all the stream fragments are being appended to a single
string.* They must instead be **routed by type into three separate channels**:

| Channel | What it is | Where it goes |
|---|---|---|
| **answer** | the assistant's visible reply text | the chat bubble |
| **thinking** | reasoning / reasoning-summary text | a separate collapsible panel, or discarded |
| **tool calls** | function name + a JSON-arguments string built up char-by-char | an accumulator, **never** the bubble; you execute it and feed the result back |

If you only ever put the **answer** channel into the message, nothing leaks. Everything
below is just "how do I know which channel a given event belongs to."

---

## First: which API are you on? The events are completely different.

There are two OpenAI endpoints, and the shape of the stream depends on which one you call.

- **Chat Completions** — `client.chat.completions.create(..., stream=True)`
  Yields `ChatCompletionChunk` objects. Everything lives under `choices[0].delta`.
- **Responses API** — `client.responses.create(..., stream=True)` (or `client.responses.stream()`)
  Yields **typed semantic events**, each with a `.type` string like
  `response.output_text.delta`. This is the newer API and is *much* easier to route
  because the type tells you the channel directly.

> If the "leak" is showing up as raw JSON like `{"city":"Paris"}` inside the message,
> that's a **tool-call arguments** fragment.
> If it's showing up as the model narrating its plan ("First I'll... then I'll..."),
> that's either **reasoning** text or the model literally answering that way.

---

## Chat Completions: the chunk shape

Every chunk looks like this (fields appear only when relevant):

```jsonc
{
  "id": "chatcmpl-...",
  "object": "chat.completion.chunk",
  "choices": [
    {
      "index": 0,
      "delta": {
        "role": "assistant",          // ← first chunk of the turn only
        "content": "Hello",            // ← ANSWER channel: visible token text
        "reasoning_content": "...",    // ← THINKING channel (see note)
        "tool_calls": [                // ← TOOL channel: arrives in fragments
          {
            "index": 0,                //    which tool call this fragment belongs to
            "id": "call_abc123",       //    usually only in the first fragment
            "type": "function",
            "function": {
              "name": "get_weather",   //    usually whole, in the first fragment
              "arguments": "{\"ci"     //    STREAMED A FEW CHARS AT A TIME
            }
          }
        ]
      },
      "finish_reason": null            // then "stop" | "tool_calls" | "length"
    }
  ]
}
```

Key facts that cause bugs:

1. **`delta.content` is the only thing that belongs in the message.** Append *only* this.
2. **`delta.tool_calls[].function.arguments` is streamed a few characters at a time**,
   and split across many chunks. You must **concatenate the fragments per `index`** to
   rebuild the JSON string, then `json.loads` it *after* the stream ends for that call.
   Appending these fragments to your message text is the classic "tool call leaking" bug.
3. **`reasoning_content`** is **not** standard OpenAI on Chat Completions — OpenAI's
   o-series hides reasoning here. But **OpenAI-compatible providers** (DeepSeek-R1,
   some local servers, OpenRouter) put chain-of-thought in `delta.reasoning_content`.
   If your friend is on one of those and appends the *whole delta*, thinking leaks.
   Route it to the thinking channel or drop it.
4. A chunk can have **empty `content`** (e.g. it only carries a tool-call fragment, or
   only `finish_reason`). Guard with `if delta.content:` before appending.
5. `finish_reason == "tool_calls"` means "I'm done deciding, go run the tools." That's
   your signal the tool-call accumulators are complete.

### Python — Chat Completions, routed correctly

```python
from openai import OpenAI
client = OpenAI()

answer_parts: list[str] = []
tool_calls: dict[int, dict] = {}   # keyed by delta index

stream = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=messages,
    tools=tools,
    stream=True,
)

for chunk in stream:
    if not chunk.choices:
        continue
    delta = chunk.choices[0].delta

    # 1) ANSWER channel — the ONLY thing the user's bubble ever sees
    if delta.content:
        answer_parts.append(delta.content)
        yield {"channel": "answer", "text": delta.content}

    # 2) THINKING channel — compatible providers only; never the bubble
    reasoning = getattr(delta, "reasoning_content", None)
    if reasoning:
        yield {"channel": "thinking", "text": reasoning}

    # 3) TOOL channel — reassemble by index, do NOT show as text
    for tc in (delta.tool_calls or []):
        slot = tool_calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
        if tc.id:
            slot["id"] = tc.id
        if tc.function and tc.function.name:
            slot["name"] += tc.function.name
        if tc.function and tc.function.arguments:
            slot["arguments"] += tc.function.arguments   # concatenate fragments

# After the stream: parse each tool call's completed JSON string
import json
final_calls = [
    {**c, "arguments": json.loads(c["arguments"] or "{}")}
    for c in (tool_calls[i] for i in sorted(tool_calls))
]
final_answer = "".join(answer_parts)
```

### TypeScript — same routing

```ts
import OpenAI from "openai";
const client = new OpenAI();

const answerParts: string[] = [];
const toolCalls: Record<number, { id: string; name: string; arguments: string }> = {};

const stream = await client.chat.completions.create({
  model: "gpt-4o-mini",
  messages,
  tools,
  stream: true,
});

for await (const chunk of stream) {
  const delta = chunk.choices[0]?.delta;
  if (!delta) continue;

  // ANSWER
  if (delta.content) {
    answerParts.push(delta.content);
    onEvent({ channel: "answer", text: delta.content });
  }

  // THINKING (compatible providers expose delta.reasoning_content)
  const reasoning = (delta as any).reasoning_content;
  if (reasoning) onEvent({ channel: "thinking", text: reasoning });

  // TOOL — reassemble per index, never render as message text
  for (const tc of delta.tool_calls ?? []) {
    const slot = (toolCalls[tc.index] ??= { id: "", name: "", arguments: "" });
    if (tc.id) slot.id = tc.id;
    if (tc.function?.name) slot.name += tc.function.name;
    if (tc.function?.arguments) slot.arguments += tc.function.arguments;
  }
}

const finalAnswer = answerParts.join("");
const finalCalls = Object.keys(toolCalls)
  .map(Number).sort((a, b) => a - b)
  .map((i) => ({ ...toolCalls[i], arguments: JSON.parse(toolCalls[i].arguments || "{}") }));
```

---

## Responses API: typed events (easier to route)

Here the SDK emits a stream of **distinct event objects**, each with a `.type`. You just
switch on the type — the leak becomes almost impossible because reasoning, answer, and
tool args are *different event types*, not fields you might accidentally merge.

The events you care about, in the order they typically arrive:

| `event.type` | Channel | Notes |
|---|---|---|
| `response.created` | — | turn started |
| `response.output_item.added` | — | a new item begins; `item.type` is `message`, `reasoning`, or `function_call` |
| `response.output_text.delta` | **answer** | `event.delta` = visible token text |
| `response.output_text.done` | answer | the full answer text for that part |
| `response.reasoning_summary_text.delta` | **thinking** | `event.delta` = reasoning-summary token |
| `response.reasoning_summary_text.done` | thinking | — |
| `response.function_call_arguments.delta` | **tool** | `event.delta` = arg-string fragment |
| `response.function_call_arguments.done` | tool | `event.arguments` = the complete JSON string |
| `response.output_item.done` | — | item finished (carries the assembled `function_call` with its `call_id`) |
| `response.completed` | — | whole turn done; `event.response` has the final assembled output |
| `response.error` / `response.failed` | error | surface it |

### Python — Responses API, routed by type

```python
stream = client.responses.create(
    model="gpt-5",           # a reasoning-capable model
    input=messages,
    tools=tools,
    stream=True,
)

answer_parts, reasoning_parts = [], []
tool_args: dict[str, str] = {}   # keyed by item_id

for event in stream:
    if event.type == "response.output_text.delta":
        answer_parts.append(event.delta)
        yield {"channel": "answer", "text": event.delta}

    elif event.type == "response.reasoning_summary_text.delta":
        reasoning_parts.append(event.delta)
        yield {"channel": "thinking", "text": event.delta}

    elif event.type == "response.function_call_arguments.delta":
        tool_args[event.item_id] = tool_args.get(event.item_id, "") + event.delta

    elif event.type == "response.error":
        raise RuntimeError(event.error)

final_answer = "".join(answer_parts)
```

Notes:

- **Only `output_text.delta` is the visible answer.** Reasoning is a *separate* event
  type, so if it's leaking on the Responses API, the code is treating "any event with a
  `.delta`" as answer text. Match on the exact type.
- Reasoning-summary events only appear when you ask for them (e.g.
  `reasoning={"summary": "auto"}`). The model's *raw* chain-of-thought is never streamed;
  you only get the summary. So there is nothing private to leak — but don't render the
  summary in the answer bubble either.
- Tool-call arguments still stream as fragments; the convenience is `...arguments.done`
  hands you the finished string so you don't have to trust your own concatenation.

---

## Assembling one full turn (and continuing the chat)

A "full chat" is a loop. One user message can trigger: *(optional reasoning) → optional
tool calls → tool results fed back → final answer.* The rule that keeps it clean:

1. **Stream** the turn, routing every event to answer / thinking / tool channels as above.
2. When the turn ends **with tool calls** (`finish_reason == "tool_calls"`, or a
   `function_call` item on the Responses API):
   - Append the **assistant message including its `tool_calls`** to history — *not* the
     arguments as text.
   - Run each tool, and append a **`role: "tool"`** message (Chat Completions) or a
     **`function_call_output`** item (Responses API) carrying the result, keyed by the
     tool call's `id` / `call_id`.
   - **Loop** — call the API again with the extended history. The model now writes the
     real answer.
3. When the turn ends **with `stop`**, the answer channel holds the complete reply. Save
   the assistant message to history and show it.

Chat Completions history after a tool round looks like:

```python
messages.append({
    "role": "assistant",
    "content": final_answer or None,
    "tool_calls": [                         # the reassembled calls, as objects
        {"id": c["id"], "type": "function",
         "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
        for c in final_calls
    ],
})
for c in final_calls:                        # one tool message per call, matched by id
    messages.append({
        "role": "tool",
        "tool_call_id": c["id"],
        "content": run_tool(c["name"], c["arguments"]),
    })
# then call create(...) again — the next turn produces the visible answer
```

**Cap the loop** (e.g. 5 round-trips) so a model that keeps calling tools can't spin
forever.

---

## Debug checklist for "thinking / tool calls leak into the reply"

Run down this list against your friend's code:

- [ ] **Is the message built from only one field?** The bubble must be fed *only* by
      `delta.content` (Chat Completions) or `response.output_text.delta` (Responses).
      If it's fed by "the whole delta" or "any `.delta`", that's the bug.
- [ ] **Are tool-call argument fragments being appended to the text?** They must go into
      a per-index / per-item accumulator, parsed as JSON *after* the stream, and never
      rendered.
- [ ] **Compatible provider?** If not on `api.openai.com`, check for
      `delta.reasoning_content` — many R1-style models put chain-of-thought there.
      Route or drop it.
- [ ] **Responses API matched loosely?** Switch on the *exact* `event.type`; don't treat
      every event with a `delta` attribute as answer text.
- [ ] **Guarding empty content?** `if delta.content:` before appending — some chunks
      carry only tool fragments or only `finish_reason`.
- [ ] **Tool result written back with the matching id?** A missing/mismatched
      `tool_call_id` makes the next turn behave oddly and re-emit the call.

If all six pass, nothing internal reaches the chat bubble.
