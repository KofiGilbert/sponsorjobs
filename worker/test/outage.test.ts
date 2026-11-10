// A COMPANY-side AI outage (src/outage.ts): the company key has no credit, is disabled or is
// invalid. The app gets a structured, honest 503 with nothing of Anthropic's text in it, /health
// says so, and the owner is told ONCE an hour on Telegram with the billing link. A passing
// failure (rate limit, overload) is "transient" and never alerts. No network: both Anthropic and
// the Telegram Bot API are stubbed fetches.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Env } from "../src/env";
import { classifyUpstream, UpstreamError } from "../src/anthropic";
import { ALERT_INTERVAL, BILLING_URL, resetOutageCache } from "../src/outage";
import { TELEGRAM_API } from "../src/telegram";
import { call, env, newAccount } from "./helpers";

const OWNER_CHAT = 424242;
const BOT: Partial<Env> = {
  TELEGRAM_BOT_TOKEN: "123456:TEST-not-real",
  TELEGRAM_BOT_USERNAME: "@SponsorJobsBot",
  TELEGRAM_WEBHOOK_SECRET: "whsec-test_not-real",
  OWNER_TELEGRAM_CHAT_ID: String(OWNER_CHAT),
};

const CREDIT_400 = { status: 400, body: { type: "error", error: { type: "invalid_request_error",
  message: "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits." } } };

interface Reply { status?: number; body: unknown; headers?: Record<string, string> }

/** Stub fetch for BOTH upstreams: a queue of Anthropic replies (the last one repeats) and a
 * recording Telegram Bot API that always says ok. */
function stubBoth(...anthropic: Array<Reply | Error>) {
  const telegram: Array<{ method: string; body: any }> = [];
  const model: Array<{ body: any }> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    if (req.url.startsWith(TELEGRAM_API)) {
      const method = req.url.slice(req.url.lastIndexOf("/") + 1);
      telegram.push({ method, body: await req.json() });
      return new Response(JSON.stringify({ ok: true, result: { message_id: 5 } }), { status: 200 });
    }
    model.push({ body: await req.json() });
    const next = anthropic.length > 1 ? anthropic.shift()! : anthropic[0];
    if (!next) throw new Error("no stubbed reply left");
    if (next instanceof Error) throw next;
    return new Response(JSON.stringify(next.body), {
      status: next.status ?? 200, headers: { "content-type": "application/json", ...(next.headers ?? {}) },
    });
  });
  return { telegram, model };
}

const ok = { body: { content: [{ type: "text", text: "fine" }], usage: { input_tokens: 3, output_tokens: 2 } } };

const complete = (token: string, extra: Partial<Env> = {}) =>
  call("/llm/complete", { token, body: { prompt: "hi" }, env: { ...BOT, ...extra } });

const health = async () => (await call("/health")).data;

let logged: ReturnType<typeof vi.spyOn>;
beforeEach(async () => {
  await env.DB.prepare("DELETE FROM ai_status").run();
  resetOutageCache();
  logged = vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => vi.restoreAllMocks());

describe("classifyUpstream", () => {
  const up = (status: number, message: string, retryAfter: number | null = null) =>
    new UpstreamError(status, JSON.stringify({ error: { message } }), retryAfter);

  it("billing, credit, a disabled org and a usage limit on 400/402/403 are the company's problem", () => {
    expect(classifyUpstream(up(400, "Your credit balance is too low"))?.reason).toBe("company");
    expect(classifyUpstream(up(402, "billing issue"))?.reason).toBe("company");
    expect(classifyUpstream(up(403, "Your organization has been disabled."))?.reason).toBe("company");
    expect(classifyUpstream(up(400, "You have reached your usage limit"))?.reason).toBe("company");
    expect(classifyUpstream(up(429, "exceeded your organization's monthly spending limit"))?.reason).toBe("company");
  });

  it("an invalid key (401) is the company's problem", () => {
    const o = classifyUpstream(up(401, "invalid x-api-key"));
    expect(o?.reason).toBe("company");
    expect(o?.note).toContain("invalid API key");
  });

  it("a rate limit, an overload, a 5xx and a dropped connection are transient, with a retry hint", () => {
    expect(classifyUpstream(up(429, "rate limit", 7))).toEqual({ reason: "transient", retryAfter: 7, note: "rate limited (429): rate limit" });
    expect(classifyUpstream(up(529, "Overloaded"))).toMatchObject({ reason: "transient", retryAfter: 15 });
    expect(classifyUpstream(up(500, "boom"))).toMatchObject({ reason: "transient" });
    expect(classifyUpstream(new TypeError("fetch failed"))).toMatchObject({ reason: "transient", note: "network: TypeError" });
  });

  it("a plain bad request is not an outage", () => {
    expect(classifyUpstream(up(400, "messages: bad role"))).toBeNull();
    expect(classifyUpstream(up(404, "model not found"))).toBeNull();
  });
});

describe("/llm/complete on a company outage", () => {
  it("answers 503 ai_unavailable/company with none of Anthropic's words, meters nothing", async () => {
    const { id, token } = await newAccount();
    stubBoth(CREDIT_400);
    const r = await complete(token);
    expect(r.status).toBe(503);
    expect(r.data).toEqual({ error: "ai_unavailable", reason: "company" });
    expect(r.text.toLowerCase()).not.toContain("credit");
    expect(r.text).not.toContain("Billing");
    expect(await env.DB.prepare("SELECT llm_tokens FROM usage WHERE user = ?").bind(id).first()).toBeNull();
  });

  it("the same for an invalid key (401) and a disabled organization (403)", async () => {
    const { token } = await newAccount();
    stubBoth({ status: 401, body: { error: { message: "invalid x-api-key sk-ant-leak" } } });
    let r = await complete(token);
    expect(r.status).toBe(503);
    expect(r.data).toEqual({ error: "ai_unavailable", reason: "company" });
    expect(r.text).not.toContain("sk-ant");
    vi.restoreAllMocks();
    logged = vi.spyOn(console, "error").mockImplementation(() => {});
    stubBoth({ status: 403, body: { error: { message: "Your organization has been disabled." } } });
    r = await complete(token);
    expect(r.data).toEqual({ error: "ai_unavailable", reason: "company" });
  });

  it("alerts the owner's Telegram chat ONCE with the cause and the billing link, then at most hourly", async () => {
    const { token } = await newAccount();
    const { telegram } = stubBoth(CREDIT_400);
    await complete(token);
    await complete(token);
    await complete(token);
    expect(telegram).toHaveLength(1);
    expect(telegram[0]!.method).toBe("sendMessage");
    expect(telegram[0]!.body.chat_id).toBe(OWNER_CHAT);
    const text: string = telegram[0]!.body.text;
    expect(text).toContain("SponsorJobs AI outage");
    expect(text).toContain("credit balance is too low");
    expect(text).toContain(BILLING_URL);
    expect(text).not.toMatch(/[–—]/);
    expect(logged).toHaveBeenCalled();                        // logged as well
    // An hour on: one reminder, and the outage still dates from the first failure.
    const since = (await env.DB.prepare("SELECT since FROM ai_status WHERE key = 'ai'").first<{ since: number }>())!.since;
    await env.DB.prepare("UPDATE ai_status SET last_alert_at = last_alert_at - ?, since = since - ? WHERE key = 'ai'")
      .bind(ALERT_INTERVAL, ALERT_INTERVAL).run();
    await complete(token);
    expect(telegram).toHaveLength(2);
    expect(telegram[1]!.body.text).toContain("STILL down");
    const row = await env.DB.prepare("SELECT state, since FROM ai_status WHERE key = 'ai'").first<{ state: string; since: number }>();
    expect(row!.state).toBe("company_outage");
    expect(row!.since).toBe(since - ALERT_INTERVAL);
  });

  it("/health and the admin stats say company_outage since <time>", async () => {
    const { token } = await newAccount();
    stubBoth(CREDIT_400);
    expect((await health()).ai).toBe("ok");
    await complete(token);
    const h = await health();
    expect(h.ai).toMatch(/^company_outage since \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
    expect(h.ai_state).toBe("company_outage");
    expect(typeof h.ai_since).toBe("number");
    expect(JSON.stringify(h)).not.toContain("credit");        // the cause is for the admin page only
    const { collectStats } = await import("../src/admin");
    const s = await collectStats({ ...env, ...BOT } as Env, Date.now());
    expect(s.ai.state).toBe("company_outage");
    expect(s.ai.summary).toBe(h.ai);
    expect(s.ai.note).toContain("credit balance");
    expect(s.ai.last_alert_at).not.toBeNull();
  });

  it("falls back to the first admin's linked chat when no owner chat id is set", async () => {
    const admin = await newAccount();
    await env.DB.prepare("UPDATE accounts SET email = ? WHERE account_id = ?").bind("kofi@example.com", admin.id).run();
    await env.DB.prepare("INSERT INTO telegram_chats(account, chat_id, username, paused, linked_at) VALUES(?, ?, ?, 0, ?)")
      .bind(admin.id, 777001, "kofi", "2026-11-01T00:00:00Z").run();
    const { token } = await newAccount();
    const { telegram } = stubBoth(CREDIT_400);
    await complete(token, { OWNER_TELEGRAM_CHAT_ID: "", ADMIN_EMAILS: "Kofi@example.com, other@example.com" });
    expect(telegram).toHaveLength(1);
    expect(telegram[0]!.body.chat_id).toBe(777001);
  });

  it("with no owner chat at all the outage is still recorded and logged, and the app still gets its 503", async () => {
    const { token } = await newAccount();
    const { telegram } = stubBoth(CREDIT_400);
    const r = await complete(token, { OWNER_TELEGRAM_CHAT_ID: "", ADMIN_EMAILS: "" });
    expect(r.status).toBe(503);
    expect(telegram).toHaveLength(0);
    expect((await health()).ai).toMatch(/^company_outage since/);
    expect(logged).toHaveBeenCalledWith(expect.stringContaining("no owner chat"));
  });

  it("the next call that goes through clears the outage and tells the owner the AI is back", async () => {
    const { token } = await newAccount();
    const { telegram } = stubBoth(CREDIT_400, CREDIT_400, ok);
    await complete(token);
    await complete(token);
    expect(telegram).toHaveLength(1);
    const r = await complete(token);
    expect(r.status).toBe(200);
    expect(r.data.text).toBe("fine");
    expect(telegram).toHaveLength(2);
    expect(telegram[1]!.body.text).toContain("SponsorJobs AI is back");
    expect((await health()).ai).toBe("ok");
    // Once back, a success writes nothing and sends nothing.
    await complete(token);
    expect(telegram).toHaveLength(2);
  });

  it("an isolate that saw ok BEFORE another isolate's outage still clears it, within a minute", async () => {
    // 2026-11-10: credits topped up, calls went through, /health kept saying company_outage,
    // because the isolate serving them had cached "ok" from before and never read the row again.
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(new Date("2026-11-10T15:00:00Z"));
      const { token } = await newAccount();
      stubBoth(ok);
      await complete(token);                                   // this isolate now knows "ok"
      // Another isolate records the outage: only the D1 row changes, not this isolate's cache.
      await env.DB.prepare("INSERT INTO ai_status(key, state, since, last_seen, last_alert_at, note) " +
        "VALUES('ai', 'company_outage', ?1, ?1, ?1, 'credit') ON CONFLICT(key) DO UPDATE SET " +
        "state = 'company_outage', since = ?1, last_seen = ?1, last_alert_at = ?1, note = 'credit'")
        .bind(Date.parse("2026-11-10T15:00:10Z") / 1000).run();
      expect((await health()).ai_state).toBe("company_outage");
      vi.setSystemTime(new Date("2026-11-10T15:01:30Z"));
      expect((await complete(token)).status).toBe(200);
      expect((await health()).ai).toBe("ok");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("/llm/complete on a transient failure", () => {
  it("a rate limit the retries could not clear is 503 ai_unavailable/transient with retry_after, and no alert", async () => {
    const { token } = await newAccount();
    const { telegram, model } = stubBoth({ status: 429, body: { error: { message: "rate limit" } }, headers: { "retry-after": "0" } });
    const r = await complete(token);
    expect(r.status).toBe(503);
    expect(r.data).toEqual({ error: "ai_unavailable", reason: "transient", retry_after: 0 });
    expect(model.length).toBe(3);                            // the SDK-style retries happened first
    expect(telegram).toHaveLength(0);
    expect((await health()).ai).toBe("transient");
  });

  it("an overload is transient with a default retry hint; a later success clears it quietly", async () => {
    const { token } = await newAccount();
    const { telegram } = stubBoth({ status: 529, body: { error: { message: "Overloaded" } }, headers: { "retry-after": "0" } },
                                  { status: 529, body: { error: { message: "Overloaded" } }, headers: { "retry-after": "0" } },
                                  { status: 529, body: { error: { message: "Overloaded" } }, headers: { "retry-after": "0" } },
                                  ok);
    const r = await complete(token);
    expect(r.status).toBe(503);
    expect(r.data).toEqual({ error: "ai_unavailable", reason: "transient", retry_after: 0 });
    expect((await complete(token)).status).toBe(200);
    expect((await health()).ai).toBe("ok");
    expect(telegram).toHaveLength(0);
  });

  it("a transient blip during a company outage does not clear the outage", async () => {
    const { token } = await newAccount();
    stubBoth(CREDIT_400, { status: 500, body: { error: { message: "boom" } }, headers: { "retry-after": "0" } });
    await complete(token);
    await complete(token);
    expect((await health()).ai).toMatch(/^company_outage since/);
  });
});

describe("what is NOT an AI outage keeps its own answer", () => {
  it("a malformed request is still the plain 502", async () => {
    const { token } = await newAccount();
    stubBoth({ status: 400, body: { error: { message: "messages: bad role" } } });
    const r = await complete(token);
    expect(r.status).toBe(502);
    expect(r.data).toEqual({ error: "the AI service is unavailable" });
    expect((await health()).ai).toBe("ok");
  });

  it("the person's own package limit keeps its 402 and reason", async () => {
    const { token } = await newAccount();
    for (let i = 0; i < 3; i++) expect((await call("/llm/package", { token, body: {} })).status).toBe(200);
    const r = await call("/llm/package", { token, body: {} });
    expect(r.status).toBe(402);
    expect(r.data.reason).toBe("package_limit");
  });
});
