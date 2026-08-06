import { MarkvectorError } from "./errors.js";
import { type Collection } from "./client.js";
import { type Document, walkSections } from "./models.js";

const DEFAULT_MODEL = "gpt-4o-mini";
const READ_BUDGET = 8000;
// The overview is a MAP, so it must stay scannable: the document cards, bounded
// and trimmed, not every section summary. Past this many documents the tail is
// left to search — a card the model cannot read is no better than one it never
// got, the same reason the navigator caps its catalogue.
const MAX_OVERVIEW = 60;

const DEFAULT_SYSTEM = `You are a research assistant answering questions from a private knowledge base, using ONLY the tools provided to look things up.

Work like this:
1. FIRST call overview — a card for each document describing what it is about. Use it to find the document that holds the answer.
2. When the overview points to a document, open it directly with read_document (or structure first for a long one) and read it. Do NOT search when the map already tells you which document to open — reading the right source is faster and more reliable than searching for it.
3. Search ONLY when the overview does not make the source obvious: a specific figure, name or identifier no card would mention, or when you genuinely cannot tell which document to open.
4. When a passage a search or read returned is close but not complete, call neighbors on its chunk_id to reach the passages nearest it in the store's graph — often the rest of the answer sits one hop away.

Ground every claim in what the tools returned — do not use outside knowledge. The overview is a MAP, not evidence: never cite it; cite the real document (item_id / filename) a claim came from. If the knowledge base does not contain the answer, say so plainly rather than guessing.`;

type JsonObject = Record<string, unknown>;
type Message = Record<string, unknown>;

interface StreamDelta {
  content?: string | null;
  tool_calls?: Array<{
    index: number;
    id?: string | null;
    function?: { name?: string | null; arguments?: string | null };
  }> | null;
}

interface StreamChunk {
  choices?: Array<{ delta?: StreamDelta | null }>;
}

/** The small surface required from an OpenAI-compatible client. */
export interface AgentClient {
  chat: {
    completions: {
      create(options: Record<string, unknown>):
        | AsyncIterable<StreamChunk>
        | Promise<AsyncIterable<StreamChunk>>;
    };
  };
}

export interface AgentOptions {
  /** An existing OpenAI-compatible client. Avoids loading the optional peer dependency. */
  client?: AgentClient | object;
  /** LLM API key. Defaults to the OpenAI client's environment configuration. */
  apiKey?: string;
  /** Base URL for an OpenAI-compatible endpoint. */
  baseUrl?: string;
  model?: string;
  /** Additional system instructions appended without replacing grounding safeguards. */
  instructions?: string;
  /** Full system-prompt replacement. Prefer `instructions` for additive guidance. */
  system?: string;
  maxSteps?: number;
  temperature?: number;
}

/** Scope one question to selected documents. Omit `files` to use the entire collection. */
export interface AgentQueryOptions {
  files?: (string | Document)[];
}

export class Thinking {
  readonly kind = "thinking" as const;
  constructor(readonly text: string) {}
}

export class ToolCall {
  readonly kind = "tool_call" as const;
  readonly ["arguments"]: JsonObject;
  constructor(
    readonly name: string,
    args: JsonObject,
  ) {
    this["arguments"] = args;
  }
}

export class ToolResult {
  readonly kind = "tool_result" as const;
  constructor(
    readonly name: string,
    readonly summary: string,
  ) {}
}

export class AgentAnswer {
  readonly kind = "answer" as const;
  constructor(readonly text: string) {}
}

export type AgentEvent = Thinking | ToolCall | ToolResult | AgentAnswer;

export class AgentResult {
  constructor(
    readonly answer: string,
    readonly steps: Exclude<AgentEvent, AgentAnswer>[] = [],
  ) {}

  get toolCalls(): number {
    return this.steps.filter((step) => step instanceof ToolCall).length;
  }
}

const TOOLS = [
  {
    type: "function",
    function: {
      name: "overview",
      description:
        "The collection's MAP, read FIRST: a card for each document describing what it is about, with its document id and how many passages it covers. Use it to find the document that holds the answer and open it directly with read_document — it lets you SKIP searching when the right source is obvious. It is the map, not the evidence, so never cite it.",
      parameters: { type: "object", properties: {} },
    },
  },
  {
    type: "function",
    function: {
      name: "search",
      description:
        "Search the knowledge base by meaning and wording. Returns the best-matching passages with their document id, filename and score.",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string" },
          files: {
            type: "array",
            items: { type: "string" },
            description: "Optional: restrict to these document ids.",
          },
          limit: { type: "integer", default: 8 },
        },
        required: ["query"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "list_files",
      description: "List the documents in the collection: id, filename, title, source.",
      parameters: { type: "object", properties: {} },
    },
  },
  {
    type: "function",
    function: {
      name: "structure",
      description:
        "The heading tree (table of contents) of one document: section titles, sizes and a one-line preview each. Use it to decide what to read.",
      parameters: {
        type: "object",
        properties: { item_id: { type: "string" } },
        required: ["item_id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "read_document",
      description:
        "The full text of one document (truncated if very long). Use after search or structure has pointed you at the right file.",
      parameters: {
        type: "object",
        properties: { item_id: { type: "string" } },
        required: ["item_id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "neighbors",
      description:
        "Given a passage's chunk_id (from a search result, or an earlier neighbors hop), list the passages nearest it in meaning — the store's own similarity graph. Related material often sits one hop from the first hit, where a fresh search would miss it. Returns each neighbour's chunk_id (hop again from it), the document it belongs to, and how close it is.",
      parameters: {
        type: "object",
        properties: {
          chunk_id: { type: "string" },
          limit: { type: "integer", default: 10 },
        },
        required: ["chunk_id"],
      },
    },
  },
] as const;

interface ReassembledCall {
  id: string;
  name: string;
  arguments: string;
}

/** A read-only, tool-using agent bound to one collection. */
export class Agent {
  readonly model: string;
  readonly system: string;
  readonly maxSteps: number;
  readonly temperature: number;
  private readonly client: Promise<AgentClient>;

  constructor(
    private readonly collection: Collection,
    options: AgentOptions = {},
  ) {
    this.model = options.model ?? DEFAULT_MODEL;
    this.system = withInstructions(options.system ?? DEFAULT_SYSTEM, options.instructions);
    this.maxSteps = options.maxSteps ?? 8;
    this.temperature = options.temperature ?? 0;
    this.client = options.client
      ? Promise.resolve(options.client as AgentClient)
      : buildClient(options.apiKey, options.baseUrl);
  }

  /** Reason, call tools, and answer, yielding each step as it happens. */
  async *stream(query: string, options: AgentQueryOptions = {}): AsyncGenerator<AgentEvent> {
    const selectedFiles = options.files === undefined
      ? undefined
      : new Set(options.files.map((file) => typeof file === "string" ? file : file.id));
    const messages: Message[] = [
      { role: "system", content: this.system },
      {
        role: "user",
        content: selectedFiles === undefined
          ? query
          : `${query}\n\nFile scope: use only these item_ids: ${JSON.stringify([...selectedFiles])}.`,
      },
    ];

    for (let step = 0; step < this.maxSteps; step++) {
      const turn = this.turn(messages, step < this.maxSteps - 1);
      let completed: IteratorResult<Thinking, { content: string; calls: ReassembledCall[] }>;
      while (!(completed = await turn.next()).done) yield completed.value;

      const { content, calls } = completed.value;
      if (calls.length === 0) {
        yield new AgentAnswer(content);
        return;
      }

      messages.push({
        role: "assistant",
        content: content || null,
        tool_calls: calls.map((call) => ({
          id: call.id,
          type: "function",
          function: { name: call.name, arguments: call.arguments },
        })),
      });

      for (const call of calls) {
        const args = parseArguments(call.arguments);
        yield new ToolCall(call.name, args);
        const result = await this.runTool(call.name, args, selectedFiles);
        yield new ToolResult(call.name, summarise(result));
        messages.push({
          role: "tool",
          tool_call_id: call.id,
          content: JSON.stringify(result),
        });
      }
    }

    // Defensive fallback for maxSteps=0. Ordinarily the last loop iteration is
    // already answer-only and returns above.
    const turn = this.turn(messages, false);
    let completed: IteratorResult<Thinking, { content: string; calls: ReassembledCall[] }>;
    while (!(completed = await turn.next()).done) yield completed.value;
    yield new AgentAnswer(completed.value.content);
  }

  /** Run to completion and return the answer plus its tool-use transcript. */
  async answer(query: string, options: AgentQueryOptions = {}): Promise<AgentResult> {
    const steps: Exclude<AgentEvent, AgentAnswer>[] = [];
    let answer = "";
    for await (const event of this.stream(query, options)) {
      if (event instanceof AgentAnswer) answer = event.text;
      else steps.push(event);
    }
    return new AgentResult(answer, steps);
  }

  private async *turn(
    messages: Message[],
    useTools: boolean,
  ): AsyncGenerator<Thinking, { content: string; calls: ReassembledCall[] }> {
    const options: Record<string, unknown> = {
      model: this.model,
      messages,
      temperature: this.temperature,
      stream: true,
    };
    if (useTools) {
      options.tools = TOOLS;
      options.tool_choice = "auto";
    }

    const stream = await (await this.client).chat.completions.create(options);
    const parts: string[] = [];
    const calls = new Map<number, ReassembledCall>();
    for await (const chunk of stream) {
      const delta = chunk.choices?.[0]?.delta;
      if (!delta) continue;
      if (delta.content) {
        parts.push(delta.content);
        yield new Thinking(delta.content);
      }
      for (const fragment of delta.tool_calls ?? []) {
        const call = calls.get(fragment.index) ?? { id: "", name: "", arguments: "" };
        if (fragment.id) call.id = fragment.id;
        if (fragment.function?.name) call.name += fragment.function.name;
        if (fragment.function?.arguments) call.arguments += fragment.function.arguments;
        calls.set(fragment.index, call);
      }
    }
    return {
      content: parts.join(""),
      calls: [...calls.entries()]
        .sort(([a], [b]) => a - b)
        .map(([, call]) => ({ ...call, arguments: call.arguments || "{}" })),
    };
  }

  /** @internal Execute one read-only agent tool. Errors become model-readable data. */
  async runTool(
    name: string,
    args: JsonObject,
    selectedFiles?: ReadonlySet<string>,
  ): Promise<unknown> {
    try {
      if (name === "overview") {
        // A lean map: the document CARDS ("what is this file about"), so the
        // model can pick the right source and read it directly rather than
        // searching. Bounded and trimmed so it stays scannable — section-level
        // detail comes from reading or searching the file a card points to.
        // Falls back to section summaries only when a collection has no cards.
        const everything = (await this.collection.summaries()).filter(
          (s) => selectedFiles === undefined || (s.itemId !== null && selectedFiles.has(s.itemId)),
        );
        const cards = everything.filter((s) => s.nodeType === "card");
        const pool = (cards.length ? cards : everything).sort((a, b) => b.covers - a.covers);
        const out: JsonObject[] = pool.slice(0, MAX_OVERVIEW).map((s) => ({
          item_id: s.itemId,
          kind: s.nodeType,
          heading: s.heading,
          about: s.text.slice(0, 240),
          covers: s.covers,
        }));
        if (pool.length > MAX_OVERVIEW) {
          out.push({ note: `${pool.length - MAX_OVERVIEW} more documents not shown here — use search to reach them.` });
        }
        return out;
      }
      if (name === "search") {
        const limit = finiteNumber(args.limit, 8);
        const requested = Array.isArray(args.files) ? args.files.map(String) : undefined;
        const files = selectedFiles === undefined
          ? requested
          : (requested ?? [...selectedFiles]).filter((id) => selectedFiles.has(id));
        // An empty item_ids query means "no filter" to the HTTP API, so stop
        // here rather than accidentally widening an empty selection to all.
        if (selectedFiles !== undefined && (files?.length ?? 0) === 0) return [];
        const results = await this.collection.search(String(args.query ?? ""), { limit, files });
        return results.matches.map((hit) => ({
          item_id: hit.id,
          // The winning passage's id, so the model can hop from a hit to its
          // neighbours instead of only reading its file.
          chunk_id: hit.chunkId,
          title: hit.title,
          score: Math.round(hit.score * 10000) / 10000,
          excerpt: hit.cleanExcerpt.slice(0, 400),
        }));
      }
      if (name === "list_files") {
        if (selectedFiles?.size === 0) return [];
        return (await this.collection.list({ limit: 200 }))
          .filter((doc) => selectedFiles === undefined || selectedFiles.has(doc.id))
          .map((doc) => ({
          item_id: doc.id,
          filename: doc.original?.filename ?? doc.source.locator,
          title: doc.title,
          source: doc.source.source,
          }));
      }
      if (name === "structure") {
        const itemId = String(args.item_id ?? "");
        if (selectedFiles !== undefined && !selectedFiles.has(itemId)) {
          return { error: `document ${JSON.stringify(itemId)} is outside the selected file scope` };
        }
        const structure = await this.collection.structure(itemId);
        return {
          item_id: structure.itemId,
          title: structure.title,
          nodes: structure.nodes,
          sections: walkSections(structure.sections).map((section) => ({
            title: section.title,
            tokens: section.tokens,
            opens: section.opens,
          })),
        };
      }
      if (name === "read_document") {
        const itemId = String(args.item_id ?? "");
        if (selectedFiles !== undefined && !selectedFiles.has(itemId)) {
          return { error: `document ${JSON.stringify(itemId)} is outside the selected file scope` };
        }
        const doc = await this.collection.get(itemId);
        const result: JsonObject = {
          item_id: doc.id,
          title: doc.title,
          text: doc.body.slice(0, READ_BUDGET),
        };
        if (doc.body.length > READ_BUDGET) result.truncated = true;
        return result;
      }
      if (name === "neighbors") {
        const chunkId = String(args.chunk_id ?? "");
        if (!chunkId) return { error: "neighbors needs a chunk_id from a search result or a hop" };
        const found = await this.collection.neighbors(chunkId, { limit: finiteNumber(args.limit, 10) });
        // Honour the same file scope search does: a hop must not walk out of the
        // documents the caller confined the agent to.
        return found
          .filter((n) => selectedFiles === undefined || selectedFiles.has(n.itemId))
          .map((n) => ({
            chunk_id: n.neighborId,
            item_id: n.itemId,
            heading: n.heading,
            title: n.title,
            similarity: Math.round(n.similarity * 10000) / 10000,
          }));
      }
      return { error: `unknown tool ${JSON.stringify(name)}` };
    } catch (error) {
      if (error instanceof MarkvectorError) return { error: error.message };
      throw error;
    }
  }
}

function parseArguments(raw: string): JsonObject {
  try {
    const parsed: unknown = JSON.parse(raw || "{}");
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as JsonObject)
      : {};
  } catch {
    return {};
  }
}

function finiteNumber(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function summarise(result: unknown): string {
  if (Array.isArray(result)) return `${result.length} result${result.length === 1 ? "" : "s"}`;
  if (result !== null && typeof result === "object") {
    const value = result as JsonObject;
    if ("error" in value) return `error: ${String(value.error)}`;
    if (Array.isArray(value.sections)) return `${String(value.nodes ?? value.sections.length)} sections`;
    if (typeof value.text === "string") {
      return `${value.text.length} chars${value.truncated ? " (truncated)" : ""}`;
    }
  }
  return "ok";
}

function withInstructions(system: string, instructions?: string): string {
  const custom = instructions?.trim();
  return custom ? `${system}\n\nAdditional instructions from the caller:\n${custom}` : system;
}

async function buildClient(apiKey?: string, baseUrl?: string): Promise<AgentClient> {
  try {
    const { default: OpenAI } = await import("openai");
    return new OpenAI({ apiKey, baseURL: baseUrl }) as unknown as AgentClient;
  } catch (error) {
    throw new MarkvectorError(
      "The agent needs an OpenAI-compatible client. Pass { client }, or install openai: npm install openai.",
      { cause: error },
    );
  }
}
