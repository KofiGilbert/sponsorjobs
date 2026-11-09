// The Anthropic call, ported from backend/provider_anthropic.py, over plain fetch (the SDK is
// not needed for one endpoint and would only grow the bundle).

import type { TextBlock, ToolDef } from "./tools";

const API_URL = "https://api.anthropic.com/v1/messages";
const DEFAULT_MAX_TOKENS = 1024;

// `output_config.effort` is not universal: Haiku 4.5 and Sonnet 4.5 (exactly what a free plan
// routes to) reject it with a 400. Listed as the families that DO accept it, so an unknown
// future model degrades to "send no effort" (a working request) rather than to a 400. Keep this
// list identical to _EFFORT_CAPABLE_PREFIXES in provider_anthropic.py.
const EFFORT_CAPABLE_PREFIXES = [
  "claude-fable-", "claude-mythos-",
  "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
  "claude-opus-4-6", "claude-opus-4-5",
  "claude-sonnet-5", "claude-sonnet-4-6",
];

export function supportsEffort(model: string): boolean {
  return EFFORT_CAPABLE_PREFIXES.some((p) => (model || "").startsWith(p));
}

export interface CompleteArgs {
  model: string;
  prompt: string;
  /** Plain text, or text blocks that may carry cache_control (src/tools.ts checked them). */
  system?: string | TextBlock[];
  messages?: unknown[] | null;
  maxTokens?: number | null;
  effort?: unknown;
  tools?: ToolDef[];
  toolChoice?: Record<string, unknown>;
}

export interface Completion {
  text: string;
  /** Every input token billed: uncached, cache writes and cache reads (metered alike). */
  input_tokens: number;
  output_tokens: number;
  /** The model's content blocks (text, tool_use) and why it stopped, for tool-use callers. */
  content: unknown[];
  stop_reason: string | null;
}

export class UpstreamError extends Error {
  constructor(readonly status: number, readonly body: string, readonly retryAfter: number | null = null) {
    super(`anthropic ${status}: ${body.slice(0, 500)}`);
  }
}

// ---- what an upstream failure MEANS (src/outage.ts turns it into the answer and the alert) ---- //

/** Why the model is unavailable: "company" is OUR problem (the company key has no credit, is
 * disabled, or is invalid: the owner must act and the person can do nothing); "transient" is a
 * passing one (rate limit, overload, a 5xx, a dropped connection: trying again later works). */
export type OutageReason = "company" | "transient";

export interface Outage {
  reason: OutageReason;
  /** Seconds the app should wait before trying again (transient only, from Retry-After). */
  retryAfter: number | null;
  /** A short cause for the log, the owner's alert and /health. Never shown to a person. */
  note: string;
}

// The words Anthropic uses when the ORGANIZATION, not the request, is the problem (seen 2026-11-01:
// "Your credit balance is too low to access the Anthropic API"; also "organization has been
// disabled", "usage limit", "billing"). Checked only on the statuses a billing problem comes with;
// a 429 is a passing rate limit unless its body says the organization's limit is spent.
const COMPANY_WORDS = /credit|billing|disabled|usage limit|spend(ing)? limit/i;
const COMPANY_STATUSES = new Set([400, 402, 403, 429]);

/** The upstream message, if the body is Anthropic's {error: {message}} JSON; else its start. */
export function upstreamMessage(body: string): string {
  try {
    const parsed = JSON.parse(body) as { error?: { message?: unknown } };
    const m = parsed?.error?.message;
    if (typeof m === "string" && m.trim()) return m.trim().slice(0, 200);
  } catch { /* not JSON */ }
  return body.replace(/\s+/g, " ").trim().slice(0, 200);
}

/** Classify a failed model call. null means it is not an outage at all (a malformed request,
 * an unknown model name, ...): the caller keeps its plain "unavailable" answer for those. */
export function classifyUpstream(err: unknown): Outage | null {
  if (!(err instanceof UpstreamError)) {
    // fetch itself failed (DNS, a reset, a timeout): nothing wrong with the key.
    return { reason: "transient", retryAfter: 15,
             note: err instanceof Error ? `network: ${err.name}` : "network error" };
  }
  const message = upstreamMessage(err.body);
  if (err.status === 401) return { reason: "company", retryAfter: null, note: `invalid API key (401): ${message}` };
  if (COMPANY_STATUSES.has(err.status) && COMPANY_WORDS.test(err.body)) {
    return { reason: "company", retryAfter: null, note: `${err.status}: ${message}` };
  }
  if (err.status === 429) return { reason: "transient", retryAfter: err.retryAfter ?? 30, note: `rate limited (429): ${message}` };
  if (err.status >= 500) return { reason: "transient", retryAfter: err.retryAfter ?? 15, note: `${err.status}: ${message}` };
  return null;
}

// The Python broker uses the anthropic SDK, which retries twice on 408/409/429/5xx and on
// network errors with a short backoff. Mirror that so an overloaded moment (529) does not reach
// the person as a failure the Python broker would have absorbed.
const MAX_RETRIES = 2;
const RETRYABLE = (s: number) => s === 408 || s === 409 || s === 429 || s >= 500;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** The Retry-After header in seconds, when it is a sane number of them. */
function retryAfterOf(res?: Response): number | null {
  const after = Number(res?.headers.get("retry-after"));
  return Number.isFinite(after) && after >= 0 && after <= 60 ? after : null;
}

function backoffMs(attempt: number, res?: Response): number {
  const after = retryAfterOf(res);
  if (after !== null) return after * 1000;
  return Math.min(8000, 500 * 2 ** attempt);
}

async function post(apiKey: string, body: Record<string, unknown>): Promise<Record<string, any>> {
  const init: RequestInit = {
    method: "POST",
    headers: { "x-api-key": apiKey, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify(body),
  };
  for (let attempt = 0; ; attempt++) {
    let res: Response;
    try {
      res = await fetch(API_URL, init);
    } catch (err) {
      if (attempt < MAX_RETRIES) { await sleep(backoffMs(attempt)); continue; }
      throw err;
    }
    if (res.ok) return (await res.json()) as Record<string, any>;
    const text = await res.text();
    if (RETRYABLE(res.status) && attempt < MAX_RETRIES) { await sleep(backoffMs(attempt, res)); continue; }
    throw new UpstreamError(res.status, text, retryAfterOf(res));
  }
}

export async function complete(apiKey: string, a: CompleteArgs): Promise<Completion> {
  // The app's calls carry a system prompt and, for the conversational flows, a full message
  // history; a plain {prompt} caller falls back to one user turn. No temperature and no
  // thinking params: the Python provider sends neither, and newer models reject them.
  const messages = a.messages && a.messages.length ? a.messages : [{ role: "user", content: a.prompt }];
  const body: Record<string, unknown> = {
    model: a.model,
    max_tokens: a.maxTokens || DEFAULT_MAX_TOKENS,
    messages,
  };
  if (Array.isArray(a.system) ? a.system.length : a.system) body.system = a.system;
  if (a.tools && a.tools.length) {
    body.tools = a.tools;
    if (a.toolChoice) body.tool_choice = a.toolChoice;
  }
  if (a.effort && supportsEffort(a.model)) body.output_config = { effort: a.effort };

  let msg: Record<string, any>;
  try {
    msg = await post(apiKey, body);
  } catch (err) {
    // A model that rejects `effort` must not take the whole request down: effort tunes
    // reasoning depth and is never load-bearing for correctness, so drop it and retry once.
    if ("output_config" in body && err instanceof UpstreamError && err.status === 400 &&
        err.body.toLowerCase().includes("effort")) {
      delete body.output_config;
      msg = await post(apiKey, body);
    } else {
      throw err;
    }
  }
  const content: any[] = Array.isArray(msg.content) ? msg.content : [];
  const text = content.filter((b) => b?.type === "text").map((b) => String(b.text ?? "")).join("").trim();
  const u = msg.usage ?? {};
  return {
    text,
    // Cached input is still input on the company key: a cache write costs more than plain input
    // and a read less, so counting both as input keeps the plan caps on the safe side.
    input_tokens: (Number(u.input_tokens) || 0) + (Number(u.cache_creation_input_tokens) || 0) +
                  (Number(u.cache_read_input_tokens) || 0),
    output_tokens: Number(u.output_tokens) || 0,
    content,
    stop_reason: typeof msg.stop_reason === "string" ? msg.stop_reason : null,
  };
}
