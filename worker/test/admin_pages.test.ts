// The admin dashboard's pages and its few write actions: give a pass, add interviews, suspend and
// restore, reveal an e-mail, change or pause the free AI limit. Each action needs the admin host's
// own origin and a reason, and leaves an audit row; the pages read Stripe, the feed manifest and
// GitHub through stubbed fetches and keep rendering when those fail. No network.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";
import type { Env } from "../src/env";
import { resetAccessCertCache } from "../src/access";
import { resetAdminCache, productFor } from "../src/admin_data";
import { INTERVIEW_SECONDS } from "../src/metering";
import { FREE_POOL_USER } from "../src/store";
import { dayOf } from "../src/metering";
import { call, env, newAccount, okReply, signedInAccount } from "./helpers";

const TEAM = "sjteam";
const ISS = "https://sjteam.cloudflareaccess.com";
const AUD = "aud-tag-0123456789abcdef";
const ADMIN = "kofi@example.com";
const CFG: Partial<Env> = { ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD };
const HOST = "https://admin.sponsorjobs.ai";

const enc = new TextEncoder();
const b64url = (b: Uint8Array | string) => {
  const bytes = typeof b === "string" ? enc.encode(b) : b;
  let s = "";
  for (const x of bytes) s += String.fromCharCode(x);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};
let good: CryptoKeyPair;
let jwks: unknown;
beforeAll(async () => {
  good = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]),
    hash: "SHA-256" }, true, ["sign", "verify"]) as CryptoKeyPair;
  const pub = (await crypto.subtle.exportKey("jwk", good.publicKey)) as JsonWebKey;
  jwks = { keys: [{ kid: "kid-1", kty: "RSA", alg: "RS256", use: "sig", n: pub.n, e: pub.e }] };
});
async function jwt() {
  const now = Math.floor(Date.now() / 1000);
  const header = b64url(JSON.stringify({ alg: "RS256", kid: "kid-1", typ: "JWT" }));
  const body = b64url(JSON.stringify({ aud: [AUD], iss: ISS, email: ADMIN, iat: now, nbf: now - 5, exp: now + 600, sub: "u1" }));
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", good.privateKey, enc.encode(`${header}.${body}`));
  return `${header}.${body}.${b64url(new Uint8Array(sig))}`;
}

// The outside world, stubbed: Access certs, Stripe charges, the feed manifests, GitHub runs, Anthropic.
let stripeCharges: unknown[] = [];
let feedAt = new Date().toISOString();
let ghRuns: unknown[] = [];
let outside: { stripe: number; feed: number; github: number } = { stripe: 0, feed: 0, github: 0 };
function stubWorld() {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    const u = req.url;
    if (u === `${ISS}/cdn-cgi/access/certs`) return Response.json(jwks);
    if (u.startsWith("https://api.stripe.com/v1/charges")) { outside.stripe++; return Response.json({ data: stripeCharges }); }
    if (u === "https://feed.sponsorjobs.ai/feed/manifest.json") { outside.feed++; return Response.json({ generated_at: feedAt, count: 248256 }); }
    if (u === "https://feed.sponsorjobs.ai/feed/sponsors/manifest.json") return Response.json({ employers: 578979, generated_at: feedAt });
    if (u.startsWith("https://api.github.com/")) { outside.github++; return Response.json({ workflow_runs: ghRuns }); }
    if (u.startsWith("https://api.anthropic.com/")) return Response.json(okReply().body);
    throw new Error(`unexpected fetch ${u}`);
  });
}

async function get(path: string, extra: Partial<Env> = {}) {
  const res = await worker.fetch(new Request(`${HOST}${path}`, { headers: { "Cf-Access-Jwt-Assertion": await jwt() } }),
    { ...env, ...CFG, ...extra } as Env);
  return { status: res.status, text: await res.text(), headers: res.headers };
}
async function post(path: string, form: Record<string, string>, o: { origin?: string | null } = {}) {
  const headers: Record<string, string> = { "Cf-Access-Jwt-Assertion": await jwt(), "content-type": "application/x-www-form-urlencoded" };
  if (o.origin !== null) headers.Origin = o.origin ?? HOST;
  const res = await worker.fetch(new Request(`${HOST}${path}`, { method: "POST", headers, body: new URLSearchParams(form).toString() }),
    { ...env, ...CFG } as Env);
  return { status: res.status, location: res.headers.get("location") ?? "", text: await res.text() };
}
const flashOf = (location: string) => {
  const u = new URL(location, HOST);
  return { ok: u.searchParams.get("ok"), err: u.searchParams.get("err"), path: u.pathname };
};
const auditRows = async (target: string) =>
  (await env.DB.prepare("SELECT admin, action, detail, reason FROM admin_actions WHERE target = ? ORDER BY id").bind(target)
    .all<{ admin: string; action: string; detail: string; reason: string }>()).results;

beforeEach(() => {
  resetAccessCertCache(); resetAdminCache();
  stripeCharges = []; ghRuns = []; feedAt = new Date().toISOString(); outside = { stripe: 0, feed: 0, github: 0 };
  stubWorld();
});
afterEach(() => vi.restoreAllMocks());

describe("every page renders behind Access", () => {
  it("overview, users, a person, revenue, usage, health and audit all answer 200, and nothing without a token", async () => {
    const a = await signedInAccount();
    for (const path of ["/", "/users", `/users/${a.id}`, "/revenue", "/usage", "/health", "/audit"]) {
      const r = await get(path);
      expect(r.status, path).toBe(200);
      expect(r.headers.get("content-security-policy")).toContain("default-src 'none'");
      expect(r.text).toContain("ByeWind");                         // the CC BY 4.0 credit
      const anon = await worker.fetch(new Request(`${HOST}${path}`), { ...env, ...CFG } as Env);
      expect(anon.status).toBe(403);
    }
    expect((await get("/users/acct_does_not_exist")).status).toBe(404);
    expect((await get("/nope")).status).toBe(404);
    expect((await get("/users/%E0%A4%A")).status).toBe(404);       // a malformed escape is not a crash
  });

  it("keeps rendering when Stripe, the feed and GitHub all fail", async () => {
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const u = new Request(input as RequestInfo).url;
      if (u === `${ISS}/cdn-cgi/access/certs`) return Response.json(jwks);
      return new Response("down", { status: 500 });
    });
    for (const path of ["/", "/revenue", "/health"]) expect((await get(path, { STRIPE_TEST_SECRET_KEY: "sk_test_x" })).status).toBe(200);
    expect((await get("/health")).text).toContain("Unreadable");
  });
});

describe("users", () => {
  it("lists accounts with masked e-mails, searches, filters and never shows the pool row", async () => {
    const a = await signedInAccount("zoe.findme@example.com");
    await newAccount();
    const all = await get("/users");
    expect(all.text).toContain("zo***@example.com");
    expect(all.text).not.toContain("zoe.findme@example.com");
    expect(all.text).not.toContain(FREE_POOL_USER);
    const found = await get("/users?q=findme");
    expect(found.text).toContain(`/users/${a.id}`);
    expect(found.text).toContain("1 matching");
    const byId = await get(`/users?q=${a.id.slice(0, 10)}`);
    expect(byId.text).toContain(`/users/${a.id}`);
    expect((await get("/users?filter=email")).text).toContain(`/users/${a.id}`);
    expect((await get("/users?filter=suspended")).text).not.toContain(`/users/${a.id}`);
  });

  it("a person's page masks the e-mail until 'Show full e-mail', which is logged", async () => {
    const a = await signedInAccount("reveal.me@example.com");
    const page = await get(`/users/${a.id}`);
    expect(page.text).toContain("re***@example.com");
    expect(page.text).not.toContain("reveal.me@example.com");
    const shown = await post(`/users/${a.id}/reveal`, {});
    expect(shown.status).toBe(200);
    expect(shown.text).toContain("reveal.me@example.com");
    expect(await auditRows(a.id)).toEqual([{ admin: ADMIN, action: "reveal_email", detail: "", reason: "" }]);
  });

  it("never shows a forwarded mail's code or link", async () => {
    const a = await signedInAccount();
    const now = Math.floor(Date.now() / 1000);
    await env.DB.batch([
      env.DB.prepare("INSERT INTO mail_aliases(local, user, forward_to, created_at) VALUES('zoe.test', ?, 'x@example.com', ?)").bind(a.id, now),
      env.DB.prepare("INSERT INTO mail_events(user, kind, code, link, subject, sender, at) VALUES(?, 'code', '918273', 'https://secret.example/verify', 's', 'f', ?)").bind(a.id, now),
    ]);
    const page = await get(`/users/${a.id}`);
    expect(page.text).toContain("zo***@mail.sponsorjobs.ai");     // the alias carries a name: masked too
    expect(page.text).not.toContain("zoe.test@");
    expect(page.text).toContain("1 mail in 30 days");
    expect((await post(`/users/${a.id}/reveal`, {})).text).toContain("zoe.test@mail.sponsorjobs.ai");
    expect(page.text).not.toContain("918273");
    expect(page.text).not.toContain("secret.example");
  });
});

describe("support actions", () => {
  it("give a pass: starts it, says so, logs it, and is not counted as a Stripe purchase", async () => {
    const a = await signedInAccount();
    const r = await post(`/users/${a.id}/give-pass`, { pass: "pass90", reason: "beta tester" });
    expect(r.status).toBe(303);
    expect(flashOf(r.location)).toEqual({ ok: "Season Pass given.", err: null, path: `/users/${a.id}` });
    const u = await env.DB.prepare("SELECT plan, pass_until FROM users WHERE user = ?").bind(a.id).first<{ plan: string; pass_until: number }>();
    expect(u!.plan).toBe("pass90");
    expect(u!.pass_until).toBeGreaterThan(Math.floor(Date.now() / 1000) + 89 * 86_400);
    expect(await auditRows(a.id)).toEqual([{ admin: ADMIN, action: "give_pass", detail: "pass90", reason: "beta tester" }]);
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM processed_events WHERE key LIKE 'checkout:%' AND key LIKE '%' || ? || '%'").bind(a.id).first<{ n: number }>();
    expect(n!.n).toBe(0);
    const page = await get(r.location);
    expect(page.text).toContain("Season Pass given.");
    expect(page.text).toContain("beta tester");                    // in the person's admin history
  });

  it("add interviews: 1 to 10, as never-expiring credit", async () => {
    const a = await newAccount();
    const r = await post(`/users/${a.id}/add-interviews`, { interviews: "2", reason: "bad call" });
    expect(flashOf(r.location).ok).toBe("2 interviews added.");
    const u = await env.DB.prepare("SELECT credit_seconds FROM users WHERE user = ?").bind(a.id).first<{ credit_seconds: number }>();
    expect(u!.credit_seconds).toBe(2 * INTERVIEW_SECONDS);
    expect(flashOf((await post(`/users/${a.id}/add-interviews`, { interviews: "11", reason: "too many" })).location).err)
      .toBe("Pick 1 to 10 interviews.");
  });

  it("every action needs a reason, and nothing changes without one", async () => {
    const a = await newAccount();
    const r = await post(`/users/${a.id}/give-pass`, { pass: "pass30", reason: " " });
    expect(flashOf(r.location).err).toBe("Type a short reason first.");
    expect(await env.DB.prepare("SELECT plan FROM users WHERE user = ?").bind(a.id).first()).toBeNull();
    expect(await auditRows(a.id)).toEqual([]);
  });

  it("suspend signs the account out everywhere until restored; it needs SUSPEND typed", async () => {
    const a = await newAccount();
    expect((await call("/account/me", { token: a.token })).status).toBe(200);
    const unconfirmed = await post(`/users/${a.id}/suspend`, { reason: "abuse", confirm: "yes" });
    expect(flashOf(unconfirmed.location).err).toBe("Type SUSPEND to confirm.");
    expect((await call("/account/me", { token: a.token })).status).toBe(200);

    const r = await post(`/users/${a.id}/suspend`, { reason: "scripted sign-ups", confirm: "SUSPEND" });
    expect(flashOf(r.location).ok).toContain("Account suspended.");
    expect((await call("/account/me", { token: a.token })).status).toBe(401);
    expect((await call("/llm/complete", { token: a.token, body: { prompt: "hi" } })).status).toBe(401);
    expect((await get("/users?filter=suspended")).text).toContain(`/users/${a.id}`);
    expect(flashOf((await post(`/users/${a.id}/suspend`, { reason: "again", confirm: "SUSPEND" })).location).err)
      .toBe("That account is already suspended.");

    const back = await post(`/users/${a.id}/unsuspend`, { reason: "appeal accepted" });
    expect(flashOf(back.location).ok).toBe("Account restored.");
    expect((await call("/account/me", { token: a.token })).status).toBe(200);
    expect((await auditRows(a.id)).map((x) => x.action)).toEqual(["suspend", "unsuspend"]);
  });

  it("refuses a write from another site, with no origin at all, or for an unknown account", async () => {
    const a = await newAccount();
    expect((await post(`/users/${a.id}/give-pass`, { pass: "pass30", reason: "x y z" }, { origin: "https://evil.example" })).status).toBe(403);
    expect((await post(`/users/${a.id}/give-pass`, { pass: "pass30", reason: "x y z" }, { origin: null })).status).toBe(403);
    expect(flashOf((await post("/users/acct_nobody/give-pass", { pass: "pass30", reason: "x y z" })).location).err).toBe("No account with that id.");
    expect(await env.DB.prepare("SELECT plan FROM users WHERE user = ?").bind(a.id).first()).toBeNull();
  });
});

describe("the free AI limit (kill switch)", () => {
  async function freeCall() {
    const a = await newAccount();
    return call("/llm/complete", { token: a.token, body: { prompt: "hi" } });
  }

  it("pause needs PAUSE typed; once paused, free AI answers 503 free_busy; 'default' brings it back", async () => {
    await env.DB.prepare("DELETE FROM admin_settings").run();
    expect((await freeCall()).status).toBe(200);
    expect(flashOf((await post("/settings/free-pool", { mode: "paused", reason: "bill spike" })).location).err).toBe("Type PAUSE to confirm.");
    const r = await post("/settings/free-pool", { mode: "paused", reason: "bill spike", confirm: "PAUSE" });
    expect(flashOf(r.location)).toEqual({ ok: "The free AI is paused. Pass holders are not affected.", err: null, path: "/usage" });
    const busy = await freeCall();
    expect(busy.status).toBe(503);
    expect(busy.data.reason).toBe("free_busy");
    expect((await get("/")).text).toContain("The free AI is paused");
    expect((await get("/usage")).text).toContain(`Paused by ${ADMIN}`);
    await post("/settings/free-pool", { mode: "default", reason: "back to normal" });
    expect((await freeCall()).status).toBe(200);
    const log = (await env.DB.prepare("SELECT action, detail FROM admin_actions WHERE action = 'set_free_pool' ORDER BY id").all()).results;
    expect(log.slice(-2)).toEqual([{ action: "set_free_pool", detail: "paused" }, { action: "set_free_pool", detail: "default" }]);
  });

  it("a custom limit applies to today's spend; bad numbers are refused", async () => {
    await env.DB.prepare("DELETE FROM admin_settings").run();
    await env.DB.prepare("INSERT INTO usage(user, period, llm_tokens) VALUES(?, ?, 5000) ON CONFLICT(user, period) DO UPDATE SET llm_tokens = 5000")
      .bind(FREE_POOL_USER, dayOf(Date.now())).run();
    expect(flashOf((await post("/settings/free-pool", { mode: "custom", tokens: "abc", reason: "test it" })).location).err).toContain("Enter a daily limit");
    await post("/settings/free-pool", { mode: "custom", tokens: "4,000", reason: "test it" });
    expect((await freeCall()).status).toBe(503);
    await post("/settings/free-pool", { mode: "custom", tokens: "1000000", reason: "test it" });
    expect((await freeCall()).status).toBe(200);
    const stats = JSON.parse((await get("/admin/api/stats")).text);
    expect(stats.free_pool_today.cap).toBe(1_000_000);
    await env.DB.prepare("DELETE FROM admin_settings").run();
  });
});

describe("revenue (Stripe)", () => {
  it("nets out refunds, labels products by price, masks buyers, flags disputes, and checks the webhook count", async () => {
    const now = Math.floor(Date.now() / 1000);
    stripeCharges = [
      { id: "ch_1", created: now - 3600, amount: 2900, amount_refunded: 0, currency: "usd", paid: true, status: "succeeded", billing_details: { email: "buyer.one@example.com" } },
      { id: "ch_2", created: now - 10 * 86_400, amount: 6900, amount_refunded: 6900, currency: "usd", paid: true, status: "succeeded", receipt_email: "buyer.two@example.com" },
      { id: "ch_3", created: now - 2 * 86_400, amount: 2400, amount_refunded: 0, currency: "usd", paid: true, status: "succeeded", disputed: true },
    ];
    const r = await get("/revenue", { STRIPE_TEST_SECRET_KEY: "sk_test_x" });
    expect(r.text).toContain("$53.00");                            // 30 days: 29 + (69 - 69) + 24
    expect(r.text).toContain("Job Hunt Pass");
    expect(r.text).toContain("3-interview pack");
    expect(r.text).toContain("bu***@example.com");
    expect(r.text).not.toContain("buyer.one@example.com");
    expect(r.text).toContain("Disputed");
    expect(r.text).toContain("Test mode");
    expect(r.text).toContain("https://dashboard.stripe.com/test/payments/ch_1");
    expect(r.text).toContain("Differs");                          // 3 paid, 0 granted by the webhook here
    expect((await get("/", { STRIPE_TEST_SECRET_KEY: "sk_test_x" })).text).toContain("1 payment is disputed");
    expect(outside.stripe).toBe(1);                                 // cached for 5 minutes
  });

  it("maps our price labels back to products", () => {
    expect(productFor(2900)).toBe("pass30");
    expect(productFor(6900)).toBe("pass90");
    expect(productFor(900)).toBe("pack_1");
    expect(productFor(3900)).toBe("pack_5");
    expect(productFor(1234)).toBe("other");
  });
});

describe("health", () => {
  it("shows the feed's age and a failed GitHub run, and raises both on the overview", async () => {
    feedAt = new Date(Date.now() - 20 * 3600_000).toISOString();
    ghRuns = [
      { name: "grow-boards", conclusion: "failure", status: "completed", updated_at: new Date().toISOString(), html_url: "https://github.com/x/1", head_branch: "main" },
      { name: "tests", conclusion: "success", status: "completed", updated_at: new Date().toISOString(), html_url: "https://github.com/x/2", head_branch: "main" },
    ];
    const h = await get("/health");
    expect(h.text).toContain("248,256 jobs");
    expect(h.text).toContain("Stale");
    expect(h.text).toContain("578,979 employers");
    expect(h.text).toContain("GitHub: grow-boards");
    const o = await get("/");
    expect(o.text).toContain("The job feed is out of date");
    expect(o.text).toContain('GitHub &quot;grow-boards&quot; failed');
    expect(o.text).toContain('class="nbadge"');                  // a count beside Health
  });

  it("the test alert is logged with its result", async () => {
    const r = await post("/admin/test-alert", {});
    expect(r.status).toBe(200);
    const row = await env.DB.prepare("SELECT action, detail FROM admin_actions WHERE action = 'test_alert' ORDER BY id DESC LIMIT 1").first();
    expect(row).toEqual({ action: "test_alert", detail: "telegram not configured" });
  });
});

describe("audit log", () => {
  it("lists every action with who, what and why, and offers older pages", async () => {
    const a = await newAccount();
    await post(`/users/${a.id}/add-interviews`, { interviews: "1", reason: "goodwill <b>gesture</b>" });
    const r = await get("/audit");
    expect(r.text).toContain("added interviews");
    expect(r.text).toContain(ADMIN);
    expect(r.text).toContain("goodwill &lt;b&gt;gesture&lt;/b&gt;");
    expect(r.text).not.toContain("<b>gesture</b>");
  });
});
