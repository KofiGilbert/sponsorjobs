// Tool use through the broker (the resume chat agent, agent/resume_chat.py): the app may send
// Anthropic tool definitions, a tool_choice and a system prompt as text blocks with
// cache_control. Everything is checked here before any metering or model call, so a malformed
// or oversized tool list is a plain 400 and never reaches the company key. Mirrors
// backend/broker.py _tool_args; keep the limits identical.
//
// Only the app's own (custom) tools are allowed: a definition with a `type` other than
// "custom" is an Anthropic server tool (web search, code execution, computer use) that would
// run, and bill, on the company key, so it is refused.

export const MAX_TOOLS = 16;
export const MAX_TOOLS_CHARS = 32_000;     // the tools' JSON, after the checks below
export const MAX_SYSTEM_BLOCKS = 4;
const TOOL_NAME = /^[a-zA-Z0-9_-]{1,64}$/;
const CHOICE_TYPES = new Set(["auto", "any", "tool", "none"]);

export class BadRequest extends Error {}

export interface CacheControl { type: "ephemeral"; ttl?: "5m" | "1h" }
export interface TextBlock { type: "text"; text: string; cache_control?: CacheControl }
export interface ToolDef { name: string; description?: string; input_schema: Record<string, unknown>; cache_control?: CacheControl }

export interface ToolArgs {
  system: string | TextBlock[];
  tools?: ToolDef[];
  toolChoice?: Record<string, unknown>;
  /** Characters the system blocks and the tools add to the request, for the up-front estimate. */
  chars: number;
}

const isObj = (v: unknown): v is Record<string, unknown> =>
  Boolean(v) && typeof v === "object" && !Array.isArray(v);

function cacheControl(v: unknown): CacheControl | undefined {
  if (v === undefined || v === null) return undefined;
  if (!isObj(v) || v.type !== "ephemeral") throw new BadRequest("cache_control must be {type: \"ephemeral\"}");
  if (v.ttl === undefined) return { type: "ephemeral" };
  if (v.ttl === "5m" || v.ttl === "1h") return { type: "ephemeral", ttl: v.ttl };
  throw new BadRequest("cache_control ttl must be 5m or 1h");
}

function system(v: unknown): { value: string | TextBlock[]; chars: number } {
  if (v === undefined || v === null) return { value: "", chars: 0 };
  if (!Array.isArray(v)) { const s = String(v); return { value: s, chars: s.length }; }
  if (v.length > MAX_SYSTEM_BLOCKS) throw new BadRequest(`at most ${MAX_SYSTEM_BLOCKS} system blocks`);
  const out: TextBlock[] = [];
  let chars = 0;
  for (const b of v) {
    if (!isObj(b) || b.type !== "text" || typeof b.text !== "string") {
      throw new BadRequest("system blocks must be {type: \"text\", text}");
    }
    const cc = cacheControl(b.cache_control);
    out.push(cc ? { type: "text", text: b.text, cache_control: cc } : { type: "text", text: b.text });
    chars += b.text.length;
  }
  return { value: out, chars };
}

function tools(v: unknown): ToolDef[] | undefined {
  if (v === undefined || v === null) return undefined;
  if (!Array.isArray(v)) throw new BadRequest("tools must be a list");
  if (!v.length) return undefined;
  if (v.length > MAX_TOOLS) throw new BadRequest(`at most ${MAX_TOOLS} tools`);
  const seen = new Set<string>();
  const out: ToolDef[] = [];
  for (const t of v) {
    if (!isObj(t)) throw new BadRequest("each tool must be an object");
    if (t.type !== undefined && t.type !== "custom") throw new BadRequest("only the app's own tools are allowed");
    if (typeof t.name !== "string" || !TOOL_NAME.test(t.name)) throw new BadRequest("a tool name is missing or invalid");
    if (seen.has(t.name)) throw new BadRequest("tool names must be unique");
    seen.add(t.name);
    if (!isObj(t.input_schema)) throw new BadRequest("each tool needs an input_schema object");
    if (t.description !== undefined && typeof t.description !== "string") throw new BadRequest("a tool description must be text");
    const def: ToolDef = { name: t.name, input_schema: t.input_schema };
    if (typeof t.description === "string") def.description = t.description;
    const cc = cacheControl(t.cache_control);
    if (cc) def.cache_control = cc;
    out.push(def);
  }
  return out;
}

function toolChoice(v: unknown, defs: ToolDef[] | undefined): Record<string, unknown> | undefined {
  if (v === undefined || v === null) return undefined;
  if (!defs) throw new BadRequest("tool_choice needs tools");
  if (!isObj(v) || typeof v.type !== "string" || !CHOICE_TYPES.has(v.type)) {
    throw new BadRequest("tool_choice type must be auto, any, tool or none");
  }
  const out: Record<string, unknown> = { type: v.type };
  if (v.type === "tool") {
    if (typeof v.name !== "string" || !defs.some((d) => d.name === v.name)) {
      throw new BadRequest("tool_choice names a tool that was not sent");
    }
    out.name = v.name;
  }
  if (typeof v.disable_parallel_tool_use === "boolean" && v.type !== "none") {
    out.disable_parallel_tool_use = v.disable_parallel_tool_use;
  }
  return out;
}

/** Check and clean the tool-use fields of a /llm/complete body. Throws BadRequest. */
export function toolArgs(b: Record<string, unknown>): ToolArgs {
  const sys = system(b.system);
  const defs = tools(b.tools);
  const choice = toolChoice(b.tool_choice, defs);
  const toolChars = defs ? JSON.stringify(defs).length : 0;
  if (toolChars > MAX_TOOLS_CHARS) throw new BadRequest("the tool definitions are too large");
  return { system: sys.value, tools: defs, toolChoice: choice, chars: sys.chars + toolChars };
}
