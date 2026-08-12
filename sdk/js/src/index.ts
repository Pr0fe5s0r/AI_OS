export { Agent, AgentAnswer, AgentResult, Thinking, ToolCall, ToolResult } from "./agent.js";
export type { AgentClient, AgentEvent, AgentOptions, AgentQueryOptions } from "./agent.js";
export { Collection, Markvector } from "./client.js";
export type { FileInput, MarkvectorOptions } from "./client.js";
export {
  AuthError,
  IndexingTimeout,
  InvalidRequest,
  MarkvectorError,
  NotFound,
  Unavailable,
} from "./errors.js";
export type {
  Answer,
  ApiKey,
  Category,
  Chunk,
  Citation,
  CollectionInfo,
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

export const VERSION = "0.2.0";
