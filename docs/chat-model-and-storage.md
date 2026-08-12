# Chat: how a conversation is created, stored, and how thinking / tools / answer attach

Companion to [`openai-streaming-events.md`](./openai-streaming-events.md). That doc is
about the *wire* (the events the OpenAI lib emits). This one is about the *data model*:
what a chat is made of, how to persist it, and where reasoning, tool input/output, and
the final reply hang off a turn.

The single idea to hold onto: **there are two views of the same conversation, and they
are not the same shape.**

| View | Purpose | Contains |
|---|---|---|
| **Transcript (display)** | what the human sees / what you save | answer text **+** thinking **+** each tool call and its result, ordered in time |
| **Context (wire)** | what you send back to OpenAI next turn | roles the API understands, tool calls with matching ids, tool outputs — **usually no reasoning** |

Store enough to rebuild **both**. Mixing them is what makes chats either leak internal
steps into the bubble, or lose the tool results the model needs to continue.

---

## 1. The shape of a chat

```
Conversation
 └── Turn (one user prompt and everything the assistant did to answer it)
      ├── user message            (the prompt)
      └── assistant turn
           ├── thinking           0..1   reasoning text     (display-only)
           ├── tool call + result 0..n   name, args, output (both views)
           └── final answer       1      the visible reply  (both views)
```

A "message" in the OpenAI sense is finer-grained than a "turn." One assistant *turn*
(one user prompt) can expand into several OpenAI *messages*: an assistant message that
requests tools, one `tool` message per result, then a second assistant message with the
answer — possibly looping. Model your storage around the **turn** for display, but keep
the individual **messages** so you can replay them to the API.

---

## 2. Creating a chat

Creating a conversation is just inserting a row and seeding the system prompt. No API
call is needed until the first user message.

```sql
INSERT INTO conversations (id, title, system_prompt, model, created_at)
VALUES (:id, :title, :system_prompt, :model, now());
```

- **`system_prompt`** is stored on the conversation, not repeated per message. It becomes
  the first `{"role": "system", ...}` message every time you build the context.
- **`title`** can start null and be filled later (a common trick: after the first
  exchange, ask the model for a 3–5 word title, or slice the first user line).
- **`model`** pinned per conversation keeps a chat reproducible if you later change the
  app default.

Then each user send appends a message and kicks off a turn (section 5).

---

## 3. The four roles, and what goes back to the API

OpenAI Chat Completions only understands these roles in the `messages` array:

| role | who | carries |
|---|---|---|
| `system` | you | instructions / persona |
| `user` | human | the prompt |
| `assistant` | model | answer text **and/or** `tool_calls` (a request to run tools) |
| `tool` | you | the **output** of one tool, tied back by `tool_call_id` |

The non-obvious ones:

- An **assistant message can have `content: null` and only `tool_calls`** — that's the
  model saying "run these," not talking to the user. You still store it and send it back;
  it's the anchor the `tool` messages point at.
- Every **`tool` message must carry the `tool_call_id`** of the call it answers. Drop it
  and the next turn breaks or re-requests the call.
- **Reasoning is not a role.** On Chat Completions you do **not** send reasoning back —
  you store it for display only. (Responses API differs, see §7.)

---

## 4. Storage schema

A pragmatic relational layout. Adjust types to your DB; the structure is the point.

```sql
CREATE TABLE conversations (
  id            uuid PRIMARY KEY,
  title         text,
  system_prompt text NOT NULL,
  model         text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE messages (
  id              uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES conversations(id),
  seq             int  NOT NULL,          -- ordering within the conversation
  role            text NOT NULL,          -- system | user | assistant | tool
  content         text,                   -- answer/prompt text; NULL for a pure tool-call msg
  reasoning       text,                   -- THINKING, display-only; NULL if none
  tool_call_id    text,                   -- set on role='tool': which call this answers
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conversation_id, seq)
);

-- One row per tool the assistant asked for (children of an assistant message).
CREATE TABLE tool_calls (
  id            text PRIMARY KEY,         -- OpenAI's call id ("call_abc123")
  message_id    uuid NOT NULL REFERENCES messages(id),  -- the assistant msg that asked
  name          text NOT NULL,           -- function name
  arguments     jsonb NOT NULL,          -- parsed args the model produced (INPUT)
  output        text,                    -- the tool's result (OUTPUT); NULL until it runs
  status        text NOT NULL,           -- pending | done | error
  created_at    timestamptz NOT NULL DEFAULT now()
);
```

Where each channel lands:

- **thinking** → `messages.reasoning` on the assistant message. Never in `content`.
- **tool input** → `tool_calls.arguments` (the reassembled, parsed JSON from the stream).
- **tool output** → `tool_calls.output` (and you also mirror it into a `role='tool'`
  message so the context view can replay it).
- **final answer** → `messages.content` on the assistant message with `status`/finish
  `stop`.

> Minimal alternative: if you don't want a `tool_calls` table, store the calls as JSON on
> the assistant message and the results as `role='tool'` messages. The dedicated table
> just makes "show the arguments and the result side by side" a trivial query.

---

## 5. Writing a turn: stream → accumulate → persist

You get fragments over the stream (see the streaming doc). **Buffer them in memory, then
write whole records** — don't do a DB write per token.

```python
# 1. append the user message
save_message(conv, role="user", content=prompt)

# 2. stream the assistant turn, routing channels into buffers
answer, reasoning = [], []
calls: dict[int, dict] = {}
for ev in stream_turn(context):           # yields {channel, ...} per the streaming doc
    if ev["channel"] == "answer":
        answer.append(ev["text"])
    elif ev["channel"] == "thinking":
        reasoning.append(ev["text"])
    elif ev["channel"] == "tool":
        acc = calls.setdefault(ev["index"], {"id": "", "name": "", "arguments": ""})
        acc["id"] = ev.get("id") or acc["id"]
        acc["name"] += ev.get("name", "")
        acc["arguments"] += ev.get("arguments", "")

# 3. persist the assistant message (answer text + reasoning together)
assistant_msg = save_message(
    conv, role="assistant",
    content="".join(answer) or None,
    reasoning="".join(reasoning) or None,
)

# 4. persist each requested tool call (INPUT), then run it (OUTPUT)
for c in (calls[i] for i in sorted(calls)):
    args = json.loads(c["arguments"] or "{}")
    save_tool_call(id=c["id"], message_id=assistant_msg.id,
                   name=c["name"], arguments=args, status="pending")
    result = run_tool(c["name"], args)
    update_tool_call(c["id"], output=result, status="done")
    # mirror the result as a tool message so the context view can replay it
    save_message(conv, role="tool", content=result, tool_call_id=c["id"])

# 5. if there were tool calls, LOOP: rebuild context (§6) and stream again;
#    the next assistant message carries the visible answer. Cap the loop (~5).
```

Persist reasoning and tool input **before** the tool runs — if the tool call crashes,
you still have a record of what the model asked for and why.

---

## 6. Rebuilding the context (wire) view from storage

Before every API call, turn stored rows back into the `messages` array. **This is where
you deliberately drop reasoning and keep tool ids.**

```python
def build_context(conv) -> list[dict]:
    msgs = [{"role": "system", "content": conv.system_prompt}]
    for m in load_messages(conv, order_by="seq"):
        if m.role == "assistant":
            entry = {"role": "assistant", "content": m.content}
            tcs = load_tool_calls(m.id)
            if tcs:
                entry["tool_calls"] = [
                    {"id": t.id, "type": "function",
                     "function": {"name": t.name, "arguments": json.dumps(t.arguments)}}
                    for t in tcs
                ]
            msgs.append(entry)                 # note: NO reasoning field sent
        elif m.role == "tool":
            msgs.append({"role": "tool", "tool_call_id": m.tool_call_id,
                         "content": m.content})
        else:  # system already added / user
            if m.role == "user":
                msgs.append({"role": "user", "content": m.content})
    return msgs
```

Rules encoded here:

- **Reasoning is omitted** — it's display-only for Chat Completions.
- **`tool_calls` stay attached to their assistant message**, and each `tool` message
  keeps its `tool_call_id`. Order matters: assistant-with-tool-calls must come *before*
  its `tool` results.
- Send only what the API needs. Titles, timestamps, status flags never go on the wire.

---

## 7. Responses API differences (if that's the endpoint)

If the chat is built on the **Responses API** rather than Chat Completions, two things
change in the storage/replay design:

- **You can skip re-sending history.** Store the `response.id` of each turn and pass
  `previous_response_id` on the next call; OpenAI keeps the server-side context. You still
  persist your own transcript for display and durability, but the wire view can be just
  the new input plus that id.
- **Reasoning may be replayable.** Reasoning items have ids and, within a linked chain,
  can be carried forward. Still keep them out of the *visible* answer; storing them under
  `messages.reasoning` is the same.

Everything else — routing channels, matching tool outputs to calls, the loop cap — is
identical.

---

## 8. Checklist

- [ ] Conversation row holds the **system prompt and model**; not repeated per message.
- [ ] **thinking → `reasoning` column**, never `content`, never sent back on the wire.
- [ ] **tool input → `arguments`** (parsed JSON), **tool output → `output`** + a mirrored
      `role='tool'` message carrying the matching `tool_call_id`.
- [ ] Final answer is the assistant message `content`; a tool-only assistant message has
      `content = NULL` and still gets stored.
- [ ] Context is **rebuilt from storage each turn** — reasoning dropped, tool ids kept,
      order preserved (assistant-with-calls before its tool results).
- [ ] Buffer stream fragments in memory; write whole records, not per-token.
- [ ] Tool-call loop is capped.
```
