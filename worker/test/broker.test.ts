// Route-level parity with tests/test_broker.py and tests/test_accounts.py (phase-1 routes),
// against the local D1 with Anthropic stubbed. No network, no real key.
import { afterEach, describe, expect, it, vi } from "vitest";
import { sha256Hex } from "../src/accounts";
import { addLlmTokens, FREE_POOL_USER } from "../src/store";
import { dayOf, periodOf } from "../src/metering";
import { call, env, freshIp, givePass, newAccount, okReply, stubAnthropic } from "./helpers";

afterEach(() => vi.restoreAllMocks());

const period = () => periodOf(Date.now());

describe("health", () => {
  it("reports a real provider only when the Anthropic key is set", async () => {
    expect((await call("/health")).data).toEqual({ ok: true, real_providers: true });
    expect((await call("/health", { env: { ANTHROPIC_API_KEY: "" } })).data)
      .toEqual({ ok: true, real_providers: false });
  });
});

describe("accounts", () => {
  it("register issues an account id and a token, and stores only the token's hash", async () => {
    const { id, token } = await newAccount();
    expect(id).toMatch(/^acct_[0-9a-f]{16}$/);
    expect(token).toMatch(/^[A-Za-z0-9_-]{43}$/);           // token_urlsafe(32)
    const rows = await env.DB.prepare("SELECT token_hash FROM tokens WHERE account_id = ?").bind(id).all();
    expect(rows.results).toEqual([{ token_hash: await sha256Hex(token) }]);
    const raw = await env.DB.prepare("SELECT COUNT(*) AS n FROM tokens WHERE token_hash = ?").bind(token).first();
    expect(raw).toEqual({ n: 0 });
  });

  it("two registrations are distinct accounts", async () => {
    const a = await newAccount();
    const b = await newAccount();
    expect(a.id).not.toBe(b.id);
    expect(a.token).not.toBe(b.token);
  });

  it("limits new accounts per network per day (429 register_limit), other networks unaffected", async () => {
    const ip = freshIp();
    const reg = (from: string) => call("/account/register",
      { method: "POST", headers: { "CF-Connecting-IP": from }, env: { REGISTER_PER_IP_PER_DAY: "2" } });
    expect((await reg(ip)).status).toBe(200);
    expect((await reg(ip)).status).toBe(200);
    const r = await reg(ip);
    expect(r.status).toBe(429);
    expect(r.data.reason).toBe("register_limit");
    expect((await reg(freshIp())).status).toBe(200);
  });

  it("the default limit is 50 a day (a campus shares one address)", async () => {
    const ip = freshIp();
    for (let i = 0; i < 50; i++) {
      expect((await call("/account/register", { method: "POST", headers: { "CF-Connecting-IP": ip } })).status).toBe(200);
    }
    expect((await call("/account/register", { method: "POST", headers: { "CF-Connecting-IP": ip } })).status).toBe(429);
  });

  it("/account/me returns the id and a null email for an anonymous account", async () => {
    const { id, token } = await newAccount();
    expect((await call("/account/me", { token })).data).toEqual({ account_id: id, email: null });
    expect((await call("/account/me")).status).toBe(401);
  });

  it("bearer auth: a wrong token is no user; the dev header works only with DEV_HEADER_AUTH=1", async () => {
    expect((await call("/me/usage", { token: "not-a-real-token" })).status).toBe(401);
    expect((await call("/me/usage", { headers: { "X-Tailor-User": "dev" } })).status).toBe(401);
    const dev = await call("/me/usage", { headers: { "X-Tailor-User": "dev" }, env: { DEV_HEADER_AUTH: "1" } });
    expect(dev.status).toBe(200);
    // a valid token still wins over the header
    const { id, token } = await newAccount();
    const me = await call("/account/me", { token, headers: { "X-Tailor-User": "dev" }, env: { DEV_HEADER_AUTH: "1" } });
    expect(me.data.account_id).toBe(id);
  });

  it("two accounts have separate plans", async () => {
    const a = await newAccount();
    const b = await newAccount();
    await givePass(a.id, "pass90");
    expect((await call("/me/usage", { token: a.token })).data.plan).toBe("pass90");
    expect((await call("/me/usage", { token: b.token })).data.plan).toBe("free");
  });
});

describe("/me/usage", () => {
  it("reports tier, pass and balances (free, then a pass)", async () => {
    const { id, token } = await newAccount();
    expect((await call("/me/usage", { token })).data).toEqual({
      plan: "free", tier: "free", pass_until: null, interviews_left: 0, packages_left: 3,
      avatar_seconds_left: 0, llm_model: "claude-haiku-4-5" });
    await givePass(id, "pass90");
    const b = (await call("/me/usage", { token })).data;
    expect(b.plan).toBe("pass90");
    expect(b.tier).toBe("pass90");
    expect(b.pass_until).toBeGreaterThan(Date.now() / 1000);
    expect(b.avatar_seconds_left).toBe(10800);
    expect(b.interviews_left).toBe(9);
    expect(b.packages_left).toBe(150);
    expect(b.llm_model).toBe("claude-sonnet-5-5");
  });

  it("an expired pass reads as free", async () => {
    const { id, token } = await newAccount();
    const { setPass } = await import("../src/store");
    await setPass(env.DB, id, "pass30", Math.floor(Date.now() / 1000) - 1, 2700, 60);
    const b = (await call("/me/usage", { token })).data;
    expect(b.tier).toBe("free");
    expect(b.pass_until).toBeNull();
    expect(b.packages_left).toBe(3);
  });
});

describe("/llm/complete", () => {
  it("free user: Haiku, no effort sent, tokens metered, free pool charged", async () => {
    const { id, token } = await newAccount();
    const { sent } = stubAnthropic(okReply("hello", 30, 12));
    const poolBefore = await env.DB.prepare("SELECT llm_tokens FROM usage WHERE user = ? AND period = ?")
      .bind(FREE_POOL_USER, dayOf(Date.now())).first<{ llm_tokens: number }>();
    const r = await call("/llm/complete", { token, body: { prompt: "hi", effort: "low" } });
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ text: "hello", model: "claude-haiku-4-5",
                             tokens: { used: 42, cap: 400_000, over_cap: false } });
    expect(sent).toHaveLength(1);
    expect(sent[0]!.url).toBe("https://api.anthropic.com/v1/messages");
    expect(sent[0]!.headers["x-api-key"]).toBe("sk-test-not-real");
    expect(sent[0]!.headers["anthropic-version"]).toBe("2023-06-01");
    expect(sent[0]!.headers["content-type"]).toBe("application/json");
    expect(sent[0]!.body).toEqual({ model: "claude-haiku-4-5", max_tokens: 1024,
                                    messages: [{ role: "user", content: "hi" }] });
    const pool = await env.DB.prepare("SELECT llm_tokens FROM usage WHERE user = ? AND period = ?")
      .bind(FREE_POOL_USER, dayOf(Date.now())).first<{ llm_tokens: number }>();
    expect(pool!.llm_tokens - (poolBefore?.llm_tokens ?? 0)).toBe(42);
    const mine = await env.DB.prepare("SELECT llm_tokens FROM usage WHERE user = ? AND period = ?")
      .bind(id, period()).first<{ llm_tokens: number }>();
    expect(mine!.llm_tokens).toBe(42);
  });

  it("forwards system, history, max_tokens and effort; a pass runs on Sonnet; no temperature/thinking", async () => {
    const { id, token } = await newAccount();
    await givePass(id);
    const { sent } = stubAnthropic(okReply("ok", 1, 1));
    const msgs = [{ role: "user", content: "hi" }, { role: "assistant", content: "yo" }, { role: "user", content: "go" }];
    const r = await call("/llm/complete",
      { token, body: { system: "Be terse", messages: msgs, max_tokens: 321, effort: "low", temperature: 0.9 } });
    expect(r.status).toBe(200);
    expect(r.data.model).toBe("claude-sonnet-5-5");
    expect(r.data.tokens).toEqual({ used: 2, cap: 8_000_000, over_cap: false });
    expect(sent[0]!.body).toEqual({ model: "claude-sonnet-5-5", max_tokens: 321, messages: msgs,
                                    system: "Be terse", output_config: { effort: "low" } });
  });

  it("the model per plan honours TAILOR_FREE_MODEL / TAILOR_PASS_MODEL", async () => {
    const { token } = await newAccount();
    const { sent } = stubAnthropic(okReply());
    const r = await call("/llm/complete", { token, body: { prompt: "hi" }, env: { TAILOR_FREE_MODEL: "claude-haiku-x" } });
    expect(r.data.model).toBe("claude-haiku-x");
    expect(sent[0]!.body.model).toBe("claude-haiku-x");
  });

  it("retries once without effort when the model rejects it", async () => {
    const { id, token } = await newAccount();
    await givePass(id);
    const { sent } = stubAnthropic(
      { status: 400, body: { type: "error", error: { type: "invalid_request_error",
        message: "This model does not support the effort parameter." } } },
      okReply("after retry"));
    const r = await call("/llm/complete", { token, body: { prompt: "hi", effort: "high" } });
    expect(r.status).toBe(200);
    expect(r.data.text).toBe("after retry");
    expect(sent).toHaveLength(2);
    expect(sent[0]!.body.output_config).toEqual({ effort: "high" });
    expect(sent[1]!.body).not.toHaveProperty("output_config");
  });

  it("a 400 that is not about effort is not retried, and fails clean", async () => {
    const { id, token } = await newAccount();
    await givePass(id);
    const { sent } = stubAnthropic({ status: 400, body: { error: { message: "messages: bad role" } } });
    vi.spyOn(console, "error").mockImplementation(() => {});
    const r = await call("/llm/complete", { token, body: { prompt: "hi", effort: "high" } });
    expect(r.status).toBe(502);
    expect(sent).toHaveLength(1);
  });

  it("an upstream failure is a 502 that leaks nothing, and nothing is metered", async () => {
    const { id, token } = await newAccount();
    stubAnthropic({ status: 401, body: { error: { message: "invalid x-api-key sk-ant-leak" } } });
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const r = await call("/llm/complete", { token, body: { prompt: "hi" } });
    expect(r.status).toBe(502);
    expect(r.data).toEqual({ error: "the AI service is unavailable" });
    expect(r.text).not.toContain("sk-ant");
    expect(log).toHaveBeenCalled();                          // logged for the operator
    const mine = await env.DB.prepare("SELECT llm_tokens FROM usage WHERE user = ?").bind(id).first();
    expect(mine).toBeNull();
  });

  it("retries an overloaded upstream (like the SDK) before giving up", async () => {
    const { token } = await newAccount();
    const { sent } = stubAnthropic({ status: 529, body: { error: { message: "overloaded" } } }, okReply("fine"));
    const r = await call("/llm/complete", { token, body: { prompt: "hi" } });
    expect(r.status).toBe(200);
    expect(sent).toHaveLength(2);
  });

  it("free user over the monthly token cap gets 402 token_cap; a pass has a far higher cap", async () => {
    const { id, token } = await newAccount();
    await addLlmTokens(env.DB, id, period(), 400_001);
    const { sent } = stubAnthropic(okReply());
    const r = await call("/llm/complete", { token, body: { prompt: "hi" } });
    expect(r.status).toBe(402);
    expect(r.data.reason).toBe("token_cap");
    expect(r.data.tier).toBe("free");
    expect(sent).toHaveLength(0);
    await givePass(id);
    expect((await call("/llm/complete", { token, body: { prompt: "tailor my resume" } })).status).toBe(200);
  });

  it("an oversized single prompt is refused before the model is called", async () => {
    const { token } = await newAccount();
    const { sent } = stubAnthropic(okReply());
    const r = await call("/llm/complete", { token, body: { prompt: "x".repeat(2_000_000) } });
    expect(r.status).toBe(402);
    expect(sent).toHaveLength(0);
  });

  it("a big history counts against the cap the same as a big prompt", async () => {
    const { id, token } = await newAccount();
    await addLlmTokens(env.DB, id, period(), 300_000);
    stubAnthropic(okReply());
    const huge = [{ role: "user", content: "x".repeat(800_000) }];       // ~200k tokens
    expect((await call("/llm/complete", { token, body: { messages: huge } })).status).toBe(402);
  });

  it("the whole free tier shares a daily budget (503 free_busy); passes are untouched", async () => {
    await env.DB.prepare("DELETE FROM usage WHERE user = ?").bind(FREE_POOL_USER).run();
    const a = await newAccount();
    const b = await newAccount();
    const paid = await newAccount();
    await givePass(paid.id);
    stubAnthropic(okReply());
    const pool = { FREE_POOL_DAILY_TOKENS: "1" };              // spent after the first free call
    expect((await call("/llm/complete", { token: a.token, body: { prompt: "hi" }, env: pool })).status).toBe(200);
    const r = await call("/llm/complete", { token: b.token, body: { prompt: "hi" }, env: pool });
    expect(r.status).toBe(503);
    expect(r.data.reason).toBe("free_busy");
    expect(r.data.tier).toBe("free");
    expect((await call("/llm/complete", { token: paid.token, body: { prompt: "hi" }, env: pool })).status).toBe(200);
    await env.DB.prepare("DELETE FROM usage WHERE user = ?").bind(FREE_POOL_USER).run();
  });

  it("no key configured: 503 not configured, never placeholder text", async () => {
    const { token } = await newAccount();
    const { sent } = stubAnthropic(okReply());
    const r = await call("/llm/complete", { token, body: { prompt: "hi" }, env: { ANTHROPIC_API_KEY: "" } });
    expect(r.status).toBe(503);
    expect(sent).toHaveLength(0);
  });

  it("a malformed body is read as empty, and no user is a 401", async () => {
    const { token } = await newAccount();
    const { sent } = stubAnthropic(okReply());
    const r = await call("/llm/complete", { token, rawBody: "{not json", method: "POST" });
    expect(r.status).toBe(200);
    expect(sent[0]!.body.messages).toEqual([{ role: "user", content: "" }]);
    expect((await call("/llm/complete", { body: { prompt: "hi" } })).status).toBe(401);
  });
});

describe("/llm/package", () => {
  it("counts runs and stops at the free three; a pass draws from its own balance", async () => {
    const { id, token } = await newAccount();
    for (const left of [2, 1, 0]) {
      const r = await call("/llm/package", { token, method: "POST" });
      expect(r.status).toBe(200);
      expect(r.data).toEqual({ model: "claude-haiku-4-5", packages_left: left, tier: "free" });
    }
    const r = await call("/llm/package", { token, method: "POST" });
    expect(r.status).toBe(402);
    expect(r.data).toEqual({ error: "the free plan's 3 tailored packages this month are used up",
                             reason: "package_limit", tier: "free", packages_left: 0 });
    await givePass(id);
    const p = await call("/llm/package", { token, method: "POST" });
    expect(p.status).toBe(200);
    expect(p.data).toEqual({ model: "claude-sonnet-5-5", packages_left: 59, tier: "pass30" });
    expect((await call("/llm/package", { method: "POST" })).status).toBe(401);
  });

  it("a pass's packages run out with 402 package_limit", async () => {
    const { id, token } = await newAccount();
    await givePass(id, "pass30", 1);
    expect((await call("/llm/package", { token, method: "POST" })).data.packages_left).toBe(0);
    const r = await call("/llm/package", { token, method: "POST" });
    expect(r.status).toBe(402);
    expect(r.data.reason).toBe("package_limit");
    expect(r.data.tier).toBe("pass30");
    expect(r.data.error).toBe("this pass's tailored packages are used up");
  });

  it("parallel package starts never overspend the free allowance", async () => {
    const { token } = await newAccount();
    const results = await Promise.all(
      Array.from({ length: 6 }, () => call("/llm/package", { token, method: "POST" })));
    expect(results.filter((r) => r.status === 200)).toHaveLength(3);
    expect(results.filter((r) => r.status === 402)).toHaveLength(3);
  });
});

describe("routing", () => {
  it("unknown routes are a JSON 404; a wrong method is a 405", async () => {
    const r = await call("/nope");
    expect(r.status).toBe(404);
    expect(r.data).toEqual({ error: "not found" });
    expect((await call("/llm/complete")).status).toBe(405);
  });

  it("phase-2 routes answer 503 not configured, so the app degrades cleanly", async () => {
    const cases: Array<[string, string]> = [
      ["POST", "/billing/passes/pass30/checkout"], ["POST", "/billing/packs/pack_1/checkout"],
      ["POST", "/billing/webhook"], ["POST", "/account/signup"], ["POST", "/account/login"],
      ["POST", "/account/claim"], ["GET", "/account/google/start"], ["GET", "/account/google/callback"],
      ["POST", "/avatar/session/start"], ["POST", "/avatar/heartbeat"],
      ["POST", "/notify/telegram/link"], ["GET", "/notify/telegram/status"], ["POST", "/telegram/webhook"],
    ];
    for (const [method, path] of cases) {
      const r = await call(path, { method });
      expect(r.status, `${method} ${path}`).toBe(503);
      expect(r.data.error, path).toMatch(/not.configured/);
    }
  });

  it("billing reads mirror Python with billing off; the free-pass dev stubs are closed", async () => {
    const { token } = await newAccount();
    expect((await call("/billing/offers", { token })).data).toEqual({
      passes: [], packs: [], current: { tier: "free", pass_until: null, interviews_left: 0, packages_left: 3 } });
    expect((await call("/billing/offers")).data).toEqual({ passes: [], packs: [], current: null });
    expect((await call("/billing/packs")).data).toEqual({ packs: [] });
    expect((await call("/billing/plan", { token, body: { plan: "pass90" } })).status).toBe(403);
    expect((await call("/billing/credits", { token, body: { seconds: 900 } })).status).toBe(403);
  });
});
