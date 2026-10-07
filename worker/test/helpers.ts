// Shared test plumbing: call the Worker's fetch handler directly (same isolate as the test, so a
// stubbed global fetch is what the Worker sees), mint accounts, and stub Anthropic.
import { env } from "cloudflare:workers";
import { vi } from "vitest";
import worker from "../src/index";
import type { Env } from "../src/env";
import { setPass } from "../src/store";
import { DAY_SECONDS, PLANS } from "../src/metering";

export { env };

let ipSeq = 0;
/** A fresh client IP per call site, so the per-network registration limit never couples tests. */
export const freshIp = () => `203.0.113.${++ipSeq}`;

export interface CallOpts {
  method?: string;
  body?: unknown;
  rawBody?: string;
  token?: string;
  headers?: Record<string, string>;
  env?: Partial<Env>;
}

export async function call(path: string, o: CallOpts = {}) {
  const headers: Record<string, string> = { ...(o.headers ?? {}) };
  if (o.token) headers.Authorization = `Bearer ${o.token}`;
  let reqBody: string | undefined = o.rawBody;
  if (o.body !== undefined) {
    reqBody = JSON.stringify(o.body);
    headers["content-type"] = "application/json";
  }
  const method = o.method ?? (reqBody !== undefined ? "POST" : "GET");
  const res = await worker.fetch(
    new Request(`https://broker.test${path}`, { method, headers, body: reqBody }),
    { ...env, ...(o.env ?? {}) } as Env,
  );
  const text = await res.text();
  let data: any = null;
  try { data = JSON.parse(text); } catch { /* not JSON */ }
  return { status: res.status, data, text };
}

/** Register an anonymous account the way the app does; returns its id and token. */
export async function newAccount(): Promise<{ id: string; token: string }> {
  const r = await call("/account/register", { method: "POST", headers: { "CF-Connecting-IP": freshIp() } });
  if (r.status !== 200) throw new Error(`register failed: ${r.status} ${r.text}`);
  return { id: r.data.account_id, token: r.data.token };
}

/** Put an account on an active pass, as a completed purchase would (phase 1 cannot sell one). */
export async function givePass(user: string, name: "pass30" | "pass90" = "pass30", packages?: number) {
  const plan = PLANS[name]!;
  const now = Math.floor(Date.now() / 1000);
  await setPass(env.DB, user, name, now + plan.days * DAY_SECONDS, plan.avatarSecondsIncluded,
                packages ?? plan.packages);
}

export interface Sent { url: string; headers: Record<string, string>; body: any }

/** Stub global fetch with a queue of Anthropic replies; records every request the Worker sent. */
export function stubAnthropic(...replies: Array<{ status?: number; body: unknown } | Error>) {
  const sent: Sent[] = [];
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    sent.push({ url: req.url, headers: Object.fromEntries(req.headers), body: await req.json() });
    const next = replies.length > 1 ? replies.shift()! : replies[0];
    if (!next) throw new Error("no stubbed reply left");
    if (next instanceof Error) throw next;
    return new Response(JSON.stringify(next.body), {
      status: next.status ?? 200, headers: { "content-type": "application/json" },
    });
  });
  return { sent, spy };
}

export const okReply = (text = "tailored text", input_tokens = 30, output_tokens = 12) => ({
  body: { content: [{ type: "text", text: ` ${text} ` }], usage: { input_tokens, output_tokens } },
});
