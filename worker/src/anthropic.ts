// The Anthropic call, ported from backend/provider_anthropic.py, over plain fetch (the SDK is
// not needed for one endpoint and would only grow the bundle).

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
  system?: string;
  messages?: unknown[] | null;
  maxTokens?: number | null;
  effort?: unknown;
}

export interface Completion { text: string; input_tokens: number; output_tokens: number }

export class UpstreamError extends Error {
  constructor(readonly status: number, readonly body: string) {
    super(`anthropic ${status}: ${body.slice(0, 500)}`);
  }
}

// The Python broker uses the anthropic SDK, which retries twice on 408/409/429/5xx and on
// network errors with a short backoff. Mirror that so an overloaded moment (529) does not reach
// the person as a failure the Python broker would have absorbed.
const MAX_RETRIES = 2;
const RETRYABLE = (s: number) => s === 408 || s === 409 || s === 429 || s >= 500;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function backoffMs(attempt: number, res?: Response): number {
  const after = Number(res?.headers.get("retry-after"));
  if (Number.isFinite(after) && after >= 0 && after <= 60) return after * 1000;
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
    throw new UpstreamError(res.status, text);
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
  if (a.system) body.system = a.system;
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
  return {
    text,
    input_tokens: Number(msg.usage?.input_tokens) || 0,
    output_tokens: Number(msg.usage?.output_tokens) || 0,
  };
}
