export { Agent, AgentAnswer, AgentResult, Thinking, ToolCall, ToolResult } from "./agent.js";
export type { AgentClient, AgentEvent, AgentOptions, AgentQueryOptions } from "./agent.js";
export { Collection, Markvector, SCOPES } from "./client.js";
export type {
  Filterable,
  FileInput,
  MarkvectorOptions,
  Scope,
  StreamEvent,
  Where,
} from "./client.js";
export {
  AuthError,
  IndexingTimeout,
  InvalidRequest,
  MarkvectorError,
  NotFound,
  RateLimited,
  Unavailable,
} from "./errors.js";
export type {
  Answer,
  ApiKey,
  Category,
  Chunk,
  Citation,
  CollectionInfo,
  Deletion,
  Document,
  IndexSummary,
  Match,
  MintedKey,
  Neighbor,
  Original,
  Region,
  Results,
  Section,
  Source,
  Structure,
  WriteResult,
} from "./models.js";
export { walkSections } from "./models.js";

export { VERSION } from "./version.js";
