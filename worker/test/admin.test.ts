// The founder's admin dashboard: the Cloudflare Access check (with a locally generated RSA key and
// a stubbed certs endpoint), the numbers on seeded data, and that no secret ever leaves.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";
import type { Env } from "../src/env";
import { resetAccessCertCache, teamHost } from "../src/access";
import { dayOf, INTERVIEW_SECONDS, periodOf } from "../src/metering";
import { FREE_POOL_USER } from "../src/store";
import { RESEND_API } from "../src/mailer";
import { call, env, freshIp, newAccount } from "./helpers";

const TEAM = "sjteam";
const ISS = "https://sjteam.cloudflareaccess.com";
const AUD = "aud-tag-0123456789abcdef";
const ADMIN = "kofi@example.com";
const CFG: Partial<Env> = { ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD };

const enc = new TextEncoder();
const b64url = (b: Uint8Array | string) => {
  const bytes = typeof b === "string" ? enc.encode(b) : b;
  let s = "";
  for (const x of bytes) s += String.fromCharCode(x);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};

let good: CryptoKeyPair;
let rogue: CryptoKeyPair;
let jwks: unknown;
const KID = "kid-1";

async function rsa() {
  return crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true, ["sign", "verify"]) as Promise<CryptoKeyPair>;
}

beforeAll(async () => {
  good = await rsa();
  rogue = await rsa();
  const pub = (await crypto.subtle.exportKey("jwk", good.publicKey)) as JsonWebKey;
  jwks = { keys: [{ kid: KID, kty: "RSA", alg: "RS256", use: "sig", n: pub.n, e: pub.e }] };
});

async function jwt(claims: Record<string, unknown>, o: { key?: CryptoKey; kid?: string; alg?: string } = {}) {
  const now = Math.floor(Date.now() / 1000);
  const header = b64url(JSON.stringify({ alg: o.alg ?? "RS256", kid: o.kid ?? KID, typ: "JWT" }));
  const body = b64url(JSON.stringify({ aud: [AUD], iss: ISS, email: ADMIN, iat: now, nbf: now - 5,
                                       exp: now + 600, sub: "u1", ...claims }));
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", o.key ?? good.privateKey, enc.encode(`${header}.${body}`));
  return `${header}.${body}.${b64url(new Uint8Array(sig))}`;
}

let certFetches: string[] = [];
function stubCerts() {
  certFetches = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = new Request(input as RequestInfo).url;
    certFetches.push(url);
    if (url === `${ISS}/cdn-cgi/access/certs`) return new Response(JSON.stringify(jwks), { status: 200 });
    throw new Error(`unexpected fetch ${url}`);
  });
}

async function admin(path: string, token?: string, extraEnv: Partial<Env> = CFG, host = "admin.sponsorjobs.ai") {
  const headers: Record<string, string> = {};
  if (token !== undefined) headers["Cf-Access-Jwt-Assertion"] = token;
  const res = await worker.fetch(new Request(`https://${host}${path}`, { headers }), { ...env, ...extraEnv } as Env);
  return { status: res.status, text: await res.text(), headers: res.headers };
}

beforeEach(() => { resetAccessCertCache(); stubCerts(); });
afterEach(() => vi.restoreAllMocks());

describe("Cloudflare Access check", () => {
  it("lets a valid token in, and fetches the team's certs", async () => {
    const r = await admin("/", await jwt({}));
    expect(r.status).toBe(200);
    expect(r.headers.get("content-type")).toContain("text/html");
    expect(r.text).toContain("SponsorJobs admin");
    expect(r.text).toContain(ADMIN);
    expect(certFetches).toEqual([`${ISS}/cdn-cgi/access/certs`]);
    const j = await admin("/admin/api/stats", await jwt({}));
    expect(j.status).toBe(200);
    expect(JSON.parse(j.text).accounts).toBeTruthy();
  });

  it("refuses a missing or malformed token", async () => {
    expect((await admin("/")).status).toBe(403);
    expect((await admin("/admin/api/stats")).status).toBe(403);
    expect((await admin("/", "not.a.jwt")).status).toBe(403);
    expect((await admin("/", "garbage")).status).toBe(403);
  });

  it("refuses a token signed by another key, with an unknown kid, or not RS256", async () => {
    expect((await admin("/", await jwt({}, { key: rogue.privateKey }))).status).toBe(403);
    expect((await admin("/", await jwt({}, { kid: "nope" }))).status).toBe(403);
    expect((await admin("/", await jwt({}, { alg: "HS256" }))).status).toBe(403);
    // A tampered payload under a valid signature.
    const t = (await jwt({})).split(".");
    t[1] = b64url(JSON.stringify({ aud: [AUD], iss: ISS, email: "evil@example.com", exp: 9_999_999_999 }));
    expect((await admin("/", t.join("."))).status).toBe(403);
  });

  it("refuses an expired, not-yet-valid, wrong-audience or wrong-issuer token", async () => {
    const now = Math.floor(Date.now() / 1000);
    expect((await admin("/", await jwt({ exp: now - 3600 }))).status).toBe(403);
    expect((await admin("/", await jwt({ nbf: now + 3600 }))).status).toBe(403);
    expect((await admin("/", await jwt({ aud: ["some-other-app"] }))).status).toBe(403);
    expect((await admin("/", await jwt({ iss: "https://other.cloudflareaccess.com" }))).status).toBe(403);
    expect((await admin("/", await jwt({ email: "" }))).status).toBe(403);
    // A single-string aud is accepted when it matches.
    expect((await admin("/", await jwt({ aud: AUD }))).status).toBe(200);
  });

  it("is closed (403) while ACCESS_TEAM_DOMAIN or ACCESS_AUD is unset, even with a valid token", async () => {
    const t = await jwt({});
    expect((await admin("/", t, {})).status).toBe(403);
    expect((await admin("/", t, { ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: "" })).status).toBe(403);
    expect((await admin("/", t, { ACCESS_TEAM_DOMAIN: "", ACCESS_AUD: AUD })).status).toBe(403);
  });

  it("ADMIN_EMAILS, when set, admits only those e-mails", async () => {
    expect((await admin("/", await jwt({}), { ...CFG, ADMIN_EMAILS: "someone@else.com" })).status).toBe(403);
    expect((await admin("/", await jwt({ email: "KOFI@example.com" }), { ...CFG, ADMIN_EMAILS: ` ${ADMIN} ` })).status).toBe(200);
  });

  it("is read-only (no POST) and serves nothing else on the admin host", async () => {
    const t = await jwt({});
    const post = await worker.fetch(new Request("https://admin.sponsorjobs.ai/admin/api/stats",
      { method: "POST", headers: { "Cf-Access-Jwt-Assertion": t } }), { ...env, ...CFG } as Env);
    expect(post.status).toBe(405);
    expect((await admin("/health", t)).status).toBe(404);          // broker routes are not on this host
    expect((await admin("/health")).status).toBe(403);             // and nothing at all without a token
  });

  it("normalises the team domain", () => {
    expect(teamHost("sjteam")).toBe("sjteam.cloudflareaccess.com");
    expect(teamHost("https://sjteam.cloudflareaccess.com/")).toBe("sjteam.cloudflareaccess.com");
    expect(teamHost(" ")).toBe("");
  });
});

describe("other hosts are unaffected", () => {
  it("the broker still answers on its own host, and the dashboard is not there", async () => {
    expect((await call("/health")).status).toBe(200);
    const t = await jwt({});
    const r = await admin("/admin/api/stats", t, CFG, "api.sponsorjobs.ai");
    expect(r.status).toBe(404);
    expect((await admin("/", t, CFG, "api.sponsorjobs.ai")).status).toBe(404);
    expect(certFetches).toEqual([]);                                // never even consulted Access
  });
});

describe("stats", () => {
  const DB = () => env.DB;
  async function wipe() {
    await DB().batch(["accounts", "tokens", "users", "usage", "avatar_sessions", "processed_events", "email_codes", "email_sends"]
      .map((t) => DB().prepare(`DELETE FROM ${t}`)));
  }

  it("counts correctly on seeded data", async () => {
    await wipe();
    const nowMs = Date.now();
    const now = Math.floor(nowMs / 1000);
    const period = periodOf(nowMs);
    const today = dayOf(nowMs);
    const day = 86_400;
    // 5 accounts: 2 legacy (no created_at), 3 dated (today, 3 days ago, 20 days ago); 3 with e-mail.
    await DB().batch([
      DB().prepare("INSERT INTO accounts(account_id, email) VALUES('acct_old1', NULL)"),
      DB().prepare("INSERT INTO accounts(account_id, email) VALUES('acct_old2', 'old@example.com')"),
      DB().prepare("INSERT INTO accounts(account_id, email, created_at, email_at) VALUES('acct_a', 'a@example.com', ?, ?)").bind(now - 60, now - 30),
      DB().prepare("INSERT INTO accounts(account_id, email, created_at, email_at) VALUES('acct_b', 'b@example.com', ?, ?)").bind(now - 3 * day, now - 3 * day),
      DB().prepare("INSERT INTO accounts(account_id, created_at) VALUES('acct_c', ?)").bind(now - 20 * day),
      DB().prepare("INSERT INTO tokens(token_hash, account_id) VALUES('deadbeefhash', 'acct_a')"),
      DB().prepare("INSERT INTO email_codes(email, code_hash, salt, expires) VALUES('a@example.com', 'codehashvalue', 'saltvalue', ?)").bind(now + 600),
      // Plans: a on an active Job Hunt Pass, b on an active Season Pass, old2 on an expired pass.
      DB().prepare("INSERT INTO users(user, plan, pass_until, credit_seconds) VALUES('acct_a', 'pass30', ?, ?)").bind(now + 10 * day, 2 * INTERVIEW_SECONDS),
      DB().prepare("INSERT INTO users(user, plan, pass_until, credit_seconds) VALUES('acct_b', 'pass90', ?, ?)").bind(now + 80 * day, INTERVIEW_SECONDS),
      DB().prepare("INSERT INTO users(user, plan, pass_until) VALUES('acct_old2', 'pass30', ?)").bind(now - day),
      DB().prepare("INSERT INTO processed_events(key, at) VALUES('checkout:cs_1', ?), ('checkout:cs_2', ?), ('checkout:cs_3', ?)").bind(now, now, now),
      // Usage this month: c is free (100k tokens, 2 packages); a on the pass (900k tokens, 4 packages).
      DB().prepare("INSERT INTO usage(user, period, llm_tokens, packages) VALUES('acct_c', ?, 100000, 2)").bind(period),
      DB().prepare("INSERT INTO usage(user, period, llm_tokens, pass_packages) VALUES('acct_a', ?, 900000, 4)").bind(period),
      DB().prepare("INSERT INTO usage(user, period, llm_tokens, packages) VALUES('acct_old1', '2000-01', 555, 1)"),
      // The free pool: 100k this month (today), and a day from another month that must not count.
      DB().prepare("INSERT INTO usage(user, period, llm_tokens) VALUES(?, ?, 100000)").bind(FREE_POOL_USER, today),
      DB().prepare("INSERT INTO usage(user, period, llm_tokens) VALUES(?, '2000-01-01', 777)").bind(FREE_POOL_USER),
      // Sign-in e-mails: 12 today, 40 on a day of another month (not counted).
      DB().prepare("INSERT INTO email_sends(day, n) VALUES(?, 12), ('2000-01-05', 40)").bind(today),
      // Interviews: b had two this month (20 + 10 minutes); one last year is ignored.
      DB().prepare("INSERT INTO avatar_sessions(conversation_id, user, started, used) VALUES('c1', 'acct_b', ?, 1200), ('c2', 'acct_b', ?, 600), ('c0', 'acct_b', 1000, 900)").bind(now - 60, now - 30),
    ]);

    const r = await admin("/admin/api/stats", await jwt({}));
    expect(r.status).toBe(200);
    const s = JSON.parse(r.text);
    expect(s.accounts.total).toBe(5);
    expect(s.accounts.with_email).toBe(3);
    expect(s.accounts.created_unknown).toBe(2);
    expect(s.accounts.new_this_week).toBe(2);
    expect(s.accounts.new_per_day).toHaveLength(30);
    expect(s.accounts.new_per_day.reduce((a: number, d: { count: number }) => a + d.count, 0)).toBe(3);
    expect(s.accounts.new_per_day.at(-1)).toEqual({ day: today, count: 1 });

    expect(s.plans.free).toBe(3);
    expect(s.plans.active_pass30).toBe(1);
    expect(s.plans.active_pass90).toBe(1);
    expect(s.plans.accounts_ever_on_pass).toEqual({ pass30: 2, pass90: 1 });
    expect(s.plans.checkout_purchases).toBe(3);
    expect(s.plans.extra_interview_credits).toEqual({ seconds: 3 * INTERVIEW_SECONDS, interviews: 3 });

    const m = s.usage_month;
    expect(m.period).toBe(period);
    expect(m.active_accounts).toBe(3);                             // a, c (usage) + b (interviews)
    expect(m.packages).toEqual({ free: 2, pass: 4, total: 6 });
    expect(m.ai_tokens).toEqual({ free: 100000, pass: 900000, total: 1000000 });
    // Haiku: 0.8*$1 + 0.2*$5 = $1.80/M -> $0.18; Sonnet 5.5: 0.8*$2 + 0.2*$10 = $3.60/M -> $3.24.
    expect(m.ai_cost_usd).toEqual({ free: 0.18, pass: 3.24, total: 3.42 });
    expect(m.interviews).toEqual({ count: 2, minutes: 30, cost_usd: 10.5 });

    expect(s.free_pool_today).toEqual({ day: today, tokens: 100000, cap: 5_000_000, percent: 2 });

    expect(s.sign_in_emails).toEqual({ day: today, today: 12, daily_cap: 100, month: 12, monthly_cap: 3000,
      level: "ok", advice: "Resend free plan: 100 a day. Upgrade to Pro ($20/month, no daily cap) before this fills." });

    expect(s.recent_sign_ins.map((x: { email: string }) => x.email)).toEqual(["a@example.com", "b@example.com", "old@example.com"]);
    expect(s.recent_sign_ins[0]).toEqual({ email: "a@example.com", account_id: "acct_a", created_at: now - 60,
                                           email_at: now - 30, plan: "pass30" });
    expect(s.recent_sign_ins[2]).toMatchObject({ created_at: null, email_at: null, plan: "free" });   // expired pass

    // Neither the JSON nor the page carries a token, a hash or a salt, or the pool's id.
    const page = await admin("/", await jwt({}));
    for (const out of [r.text, page.text]) {
      for (const secret of ["deadbeefhash", "codehashvalue", "saltvalue", "token_hash", "code_hash", FREE_POOL_USER]) {
        expect(out).not.toContain(secret);
      }
    }
    expect(page.text).toContain("old@example.com");
    expect(page.text).toContain("12 of 100 today");
    expect(page.text).toContain("Upgrade to Pro ($20/month, no daily cap) before this fills.");
    expect(page.text).toContain("unknown");                         // legacy rows are labelled, not invented
    expect(page.headers.get("cache-control")).toBe("no-store");
  });

  it("escapes what it prints", async () => {
    await wipe();
    await DB().prepare("INSERT INTO accounts(account_id, email, created_at) VALUES('acct_x', '<script>x</script>@e.com', ?)")
      .bind(Math.floor(Date.now() / 1000)).run();
    const page = await admin("/", await jwt({}));
    expect(page.text).not.toContain("<script>x</script>");
    expect(page.text).toContain("&lt;script&gt;");
  });

  it("new accounts and e-mail sign-ins are dated from now on", async () => {
    await wipe();
    const before = Math.floor(Date.now() / 1000);
    const { id } = await newAccount();
    const row = await DB().prepare("SELECT created_at, email_at FROM accounts WHERE account_id = ?").bind(id)
      .first<{ created_at: number; email_at: number | null }>();
    expect(row!.created_at).toBeGreaterThanOrEqual(before);
    expect(row!.email_at).toBeNull();
    const { signInWithEmail } = await import("../src/accounts");
    await signInWithEmail(DB(), "new@example.com", id);
    const after = await DB().prepare("SELECT email_at FROM accounts WHERE account_id = ?").bind(id).first<{ email_at: number }>();
    expect(after!.email_at).toBeGreaterThanOrEqual(before);
    const fresh = await signInWithEmail(DB(), "brandnew@example.com", null);
    const f = await DB().prepare("SELECT created_at, email_at FROM accounts WHERE account_id = ?").bind(fresh.account_id)
      .first<{ created_at: number; email_at: number }>();
    expect(f!.created_at).toBeGreaterThanOrEqual(before);
    expect(f!.email_at).toBeGreaterThanOrEqual(before);
  });
});

describe("pass packages are counted per month for the dashboard", () => {
  it("a package taken from a pass adds to usage.pass_packages, not to the free count", async () => {
    const { takePassPackage, setPass } = await import("../src/store");
    const now = Math.floor(Date.now() / 1000);
    await setPass(env.DB, "acct_pp", "pass30", now + 86_400, 0, 5);
    expect(await takePassPackage(env.DB, "acct_pp", now, "2031-01")).toBe(4);
    expect(await takePassPackage(env.DB, "acct_pp", now, "2031-01")).toBe(3);
    const row = await env.DB.prepare("SELECT packages, pass_packages FROM usage WHERE user = 'acct_pp' AND period = '2031-01'")
      .first();
    expect(row).toEqual({ packages: 0, pass_packages: 2 });
  });
});

describe("sign-in code e-mails against Resend's free plan", () => {
  async function emails(today: number) {
    await env.DB.prepare("DELETE FROM email_sends").run();
    if (today > 0) {
      await env.DB.prepare("INSERT INTO email_sends(day, n) VALUES(?, ?)").bind(dayOf(Date.now()), today).run();
    }
    const r = await admin("/admin/api/stats", await jwt({}));
    return { s: JSON.parse(r.text).sign_in_emails, page: (await admin("/", await jwt({}))).text };
  }

  it("is ok below 70 a day, amber from 70, red from 90", async () => {
    expect((await emails(69)).s.level).toBe("ok");
    const amber = await emails(70);
    expect(amber.s.level).toBe("amber");
    expect(amber.page).toContain('class="card amber"');
    expect((await emails(89)).s.level).toBe("amber");
    const red = await emails(90);
    expect(red.s.level).toBe("red");
    expect(red.page).toContain('class="card red"');
  });

  it("every code e-mail actually sent adds one to today's count; a failed send does not", async () => {
    await env.DB.prepare("DELETE FROM email_sends").run();
    let resendStatus = 200;
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new Request(input as RequestInfo).url;
      if (url === RESEND_API) return new Response(JSON.stringify({ id: "e1" }), { status: resendStatus });
      if (url === `${ISS}/cdn-cgi/access/certs`) return new Response(JSON.stringify(jwks), { status: 200 });
      throw new Error(`unexpected fetch ${url}`);
    });
    const MAIL = { RESEND_API_KEY: "re_test_not_real" };
    const start = (email: string) => call("/account/email/start",
      { body: { email }, headers: { "CF-Connecting-IP": freshIp() }, env: MAIL });
    expect((await start("one@example.com")).status).toBe(200);
    expect((await start("two@example.com")).status).toBe(200);
    resendStatus = 500;
    expect((await start("three@example.com")).status).toBe(502);
    const s = JSON.parse((await admin("/admin/api/stats", await jwt({}))).text).sign_in_emails;
    expect(s.today).toBe(2);
    expect(s.month).toBe(2);
  });
});
