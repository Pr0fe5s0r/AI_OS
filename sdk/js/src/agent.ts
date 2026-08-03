import { MarkvectorError } from "./errors.js";
import { type Collection } from "./client.js";
import { walkSections } from "./models.js";

const DEFAULT_MODEL = "gpt-4o-mini";
const READ_BUDGET = 8000;

const DEFAULT_SYSTEM = `You are a research assistant answering questions from a private knowledge base, using ONLY the tools provided to look things up.

Work like this: search for what you need, list or open the relevant files, read the sections that matter, then answer. Ground every claim in what the tools returned — do not use outside knowledge. For a long, structured document, prefer structure to see its sections and read_document to read it; for a specific fact, prefer search. Cite the file (item_id / filename) a claim came from. If the knowledge base does not contain the answer, say so plainly rather than guessing.`;

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
  system?: string;
  maxSteps?: number;
  temperature?: number;
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
    this.system = options.system ?? DEFAULT_SYSTEM;
    this.maxSteps = options.maxSteps ?? 8;
    this.temperature = options.temperature ?? 0;
    this.client = options.client
      ? Promise.resolve(options.client as AgentClient)
      : buildClient(options.apiKey, options.baseUrl);
  }

  /** Reason, call tools, and answer, yielding each step as it happens. */
  async *stream(query: string): AsyncGenerator<AgentEvent> {
    const messages: Message[] = [
      { role: "system", content: this.system },
      { role: "user", content: query },
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
        const result = await this.runTool(call.name, args);
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
  async answer(query: string): Promise<AgentResult> {
    const steps: Exclude<AgentEvent, AgentAnswer>[] = [];
    let answer = "";
    for await (const event of this.stream(query)) {
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
  async runTool(name: string, args: JsonObject): Promise<unknown> {
    try {
      if (name === "search") {
        const limit = finiteNumber(args.limit, 8);
        const files = Array.isArray(args.files) ? args.files.map(String) : undefined;
        const results = await this.collection.search(String(args.query ?? ""), { limit, files });
        return results.matches.map((hit) => ({
          item_id: hit.id,
          title: hit.title,
          score: Math.round(hit.score * 10000) / 10000,
          excerpt: hit.cleanExcerpt.slice(0, 400),
        }));
      }
      if (name === "list_files") {
        return (await this.collection.list({ limit: 200 })).map((doc) => ({
          item_id: doc.id,
          filename: doc.original?.filename ?? doc.source.locator,
          title: doc.title,
          source: doc.source.source,
        }));
      }
      if (name === "structure") {
        const structure = await this.collection.structure(String(args.item_id ?? ""));
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
        const doc = await this.collection.get(String(args.item_id ?? ""));
        const result: JsonObject = {
          item_id: doc.id,
          title: doc.title,
          text: doc.body.slice(0, READ_BUDGET),
        };
        if (doc.body.length > READ_BUDGET) result.truncated = true;
        return result;
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
