// Passwordless sign-in: e-mailed one-time codes and "Continue with Google". Resend and Google are
// stubbed (no network); the code is read back out of the stubbed e-mail, as the person would.
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Env } from "../src/env";
import { RESEND_API } from "../src/mailer";
import { allowedRedirect, emailFromIdToken, GOOGLE_AUTH, GOOGLE_TOKEN, STATE_COOKIE } from "../src/signin";
import { call, env, freshIp, givePass, newAccount } from "./helpers";

afterEach(() => vi.restoreAllMocks());

const MAIL: Partial<Env> = { RESEND_API_KEY: "re_test_not_real" };
const GOOGLE: Partial<Env> = { GOOGLE_CLIENT_ID: "cid.apps.googleusercontent.com", GOOGLE_CLIENT_SECRET: "gsecret" };

let seq = 0;
const freshEmail = () => `person${++seq}.${Date.now()}@example.com`;

interface Mail { url: string; auth: string | null; body: any }

/** Stub fetch as Resend (and optionally Google's token endpoint). */
function stubFetch(o: { resendStatus?: number; idClaims?: Record<string, unknown>; googleStatus?: number } = {}) {
  const mails: Mail[] = [];
  const google: Record<string, string>[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    if (req.url === RESEND_API) {
      mails.push({ url: req.url, auth: req.headers.get("Authorization"), body: await req.json() });
      return new Response(JSON.stringify({ id: "email_1" }), { status: o.resendStatus ?? 200 });
    }
    if (req.url === GOOGLE_TOKEN) {
      google.push(Object.fromEntries(new URLSearchParams(await req.text())));
      const enc = (x: unknown) => btoa(JSON.stringify(x)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
      const idToken = `${enc({ alg: "RS256" })}.${enc(o.idClaims ?? {})}.sig`;
      return new Response(JSON.stringify({ id_token: idToken, access_token: "x" }), { status: o.googleStatus ?? 200 });
    }
    throw new Error(`unexpected fetch ${req.url}`);
  });
  return { mails, google };
}

const codeIn = (m: Mail) => /\b(\d{6})\b/.exec(m.body.text)![1]!;

async function start(email: string, ip = freshIp(), extra: Partial<Env> = {}) {
  return call("/account/email/start", { body: { email }, headers: { "CF-Connecting-IP": ip }, env: { ...MAIL, ...extra } });
}
async function verify(email: string, code: string, token?: string) {
  return call("/account/email/verify", { body: { email, code }, token, env: MAIL });
}
async function codeFor(email: string): Promise<string> {
  const { mails } = stubFetch();
  expect((await start(email)).status).toBe(200);
  vi.restoreAllMocks();
  return codeIn(mails.at(-1)!);
}
const me = async (token: string) => (await call("/account/me", { token })).data;
const usage = async (token: string) => (await call("/me/usage", { token })).data;

describe("unconfigured sign-in", () => {
  it("without a sender, /account/email/* is 503 'email sign-in is not configured' and nothing is sent", async () => {
    const spy = vi.spyOn(globalThis, "fetch");
    for (const path of ["/account/email/start", "/account/email/verify"]) {
      const r = await call(path, { body: { email: "a@example.com", code: "123456" } });
      expect(r.status, path).toBe(503);
      expect(r.data, path).toEqual({ error: "email sign-in is not configured" });
    }
    expect(spy).not.toHaveBeenCalled();
  });

  it("without Google secrets, /account/google/* is 503 as before; password routes stay off", async () => {
    for (const p of ["/account/google/start?redirect=http://127.0.0.1:57000/", "/account/google/callback?state=x&code=y"]) {
      const r = await call(p, { env: { GOOGLE_CLIENT_ID: "only-the-id" } });
      expect(r.status, p).toBe(503);
      expect(r.data).toEqual({ error: "google sign-in is not configured" });
    }
    for (const p of ["/account/signup", "/account/login", "/account/claim"]) {
      expect((await call(p, { method: "POST", env: { ...MAIL, ...GOOGLE } })).status).toBe(503);
    }
  });
});

describe("e-mail code: issue", () => {
  it("sends a plain 6-digit code from no-reply@sponsorjobs.ai and stores only a hash", async () => {
    const { mails } = stubFetch();
    const r = await start("  Kofi.Test@Example.COM ");
    expect(r.status).toBe(200);
    expect(r.data).toEqual({ sent: true });
    expect(mails).toHaveLength(1);
    const m = mails[0]!;
    expect(m.auth).toBe("Bearer re_test_not_real");
    expect(m.body.from).toBe("SponsorJobs <no-reply@sponsorjobs.ai>");
    expect(m.body.to).toEqual(["kofi.test@example.com"]);
    const code = codeIn(m);
    expect(m.body.text).toContain(`Your SponsorJobs code is ${code}. It expires in 10 minutes.`);
    const row = await env.DB.prepare("SELECT * FROM email_codes WHERE email = ?").bind("kofi.test@example.com").first<any>();
    expect(row.code_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(JSON.stringify(row)).not.toContain(code);
    expect(row.expires - Math.floor(Date.now() / 1000)).toBeGreaterThan(590);
  });

  it("rejects a malformed e-mail with 400 and sends nothing", async () => {
    const { mails } = stubFetch();
    for (const email of ["", "nobody", "a@b", "a b@example.com", "x@example.c"]) {
      const r = await start(email);
      expect(r.status, email).toBe(400);
      expect(r.data.reason).toBe("bad_email");
    }
    expect(mails).toHaveLength(0);
  });

  it("at most 5 sends per e-mail per hour (429 rate_limited), whatever the network", async () => {
    const { mails } = stubFetch();
    const email = freshEmail();
    for (let i = 0; i < 5; i++) expect((await start(email)).status).toBe(200);
    const r = await start(email);
    expect(r.status).toBe(429);
    expect(r.data.reason).toBe("rate_limited");
    expect(mails).toHaveLength(5);
  });

  it("at most 5 sends per network per hour, whatever the e-mail", async () => {
    stubFetch();
    const ip = freshIp();
    for (let i = 0; i < 5; i++) expect((await start(freshEmail(), ip)).status).toBe(200);
    expect((await start(freshEmail(), ip)).status).toBe(429);
    expect((await start(freshEmail())).status).toBe(200);        // another network is fine
  });

  it("a delivery failure is a clean 502", async () => {
    stubFetch({ resendStatus: 422 });
    const r = await start(freshEmail());
    expect(r.status).toBe(502);
    expect(r.data.error).not.toMatch(/resend|422/i);
  });

  it("works through Cloudflare's send_email binding when no Resend key is set", async () => {
    const sent: any[] = [];
    const r = await call("/account/email/start", { body: { email: "cf@example.com" },
      env: { EMAIL: { send: async (m: any) => { sent.push(m); } } } });
    expect(r.status).toBe(200);
    expect(sent[0]).toMatchObject({ to: "cf@example.com", from: "SponsorJobs <no-reply@sponsorjobs.ai>" });
  });
});

describe("e-mail code: verify", () => {
  it("with no device token, a right code creates an account with that e-mail", async () => {
    const email = freshEmail();
    const code = await codeFor(email);
    const r = await verify(email.toUpperCase(), code);
    expect(r.status).toBe(200);
    expect(r.data.email).toBe(email);
    expect(await me(r.data.token)).toEqual({ account_id: r.data.account_id, email });
    // the code is single-use
    const again = await verify(email, code);
    expect(again.status).toBe(400);
    expect(again.data.reason).toBe("expired");
  });

  it("a wrong code is 400 bad_code; the 5th wrong try burns the code, even the right one fails after", async () => {
    const email = freshEmail();
    const code = await codeFor(email);
    const wrong = code === "000000" ? "111111" : "000000";
    for (let i = 0; i < 5; i++) {
      const r = await verify(email, wrong);
      expect(r.status).toBe(400);
      expect(r.data.reason).toBe("bad_code");
    }
    const r = await verify(email, code);
    expect(r.status).toBe(400);
    expect(r.data.reason).toBe("expired");
  });

  it("parallel guesses still get at most 5 tries per code", async () => {
    const email = freshEmail();
    const code = await codeFor(email);
    const wrong = code === "000000" ? "111111" : "000000";
    await Promise.all(Array.from({ length: 12 }, () => verify(email, wrong)));
    expect((await verify(email, code)).status).toBe(400);
  });

  it("an expired code is 400 expired", async () => {
    const email = freshEmail();
    const code = await codeFor(email);
    await env.DB.prepare("UPDATE email_codes SET expires = ? WHERE email = ?").bind(Math.floor(Date.now() / 1000) - 1, email).run();
    const r = await verify(email, code);
    expect(r.status).toBe(400);
    expect(r.data.reason).toBe("expired");
  });

  it("asking again replaces the code: the old one no longer works", async () => {
    const email = freshEmail();
    const first = await codeFor(email);
    const second = await codeFor(email);
    if (first !== second) expect((await verify(email, first)).data.reason).toBe("bad_code");
    expect((await verify(email, second)).status).toBe(200);
  });

  it("a malformed code is bad_code", async () => {
    expect((await verify(freshEmail(), "12ab56")).data.reason).toBe("bad_code");
  });

  it("attach: a new e-mail joins the caller's device account and keeps its pass and usage", async () => {
    const { id, token } = await newAccount();
    await givePass(id);
    const before = await usage(token);
    const email = freshEmail();
    const r = await verify(email, await codeFor(email), token);
    expect(r.status).toBe(200);
    expect(r.data.account_id).toBe(id);
    expect(r.data.email).toBe(email);
    expect(await me(r.data.token)).toEqual({ account_id: id, email });
    expect(await usage(r.data.token)).toEqual(before);
    expect((await usage(token)).tier).toBe("pass30");          // the old device token still works
  });

  it("an e-mail that already has an account logs in to it: the device switches account", async () => {
    const owner = await newAccount();
    await givePass(owner.id, "pass90");
    const email = freshEmail();
    expect((await verify(email, await codeFor(email), owner.token)).data.account_id).toBe(owner.id);

    const device = await newAccount();                      // a second machine, anonymous
    const r = await verify(email, await codeFor(email), device.token);
    expect(r.status).toBe(200);
    expect(r.data.account_id).toBe(owner.id);
    expect(r.data.token).not.toBe(owner.token);
    expect((await usage(r.data.token)).tier).toBe("pass90");
    expect(await me(device.token)).toEqual({ account_id: device.id, email: null });   // left untouched
  });

  it("a caller already signed in as another e-mail gets a new account, never a renamed one", async () => {
    const a = freshEmail();
    const first = await verify(a, await codeFor(a));
    const b = freshEmail();
    const r = await verify(b, await codeFor(b), first.data.token);
    expect(r.data.account_id).not.toBe(first.data.account_id);
    expect((await me(first.data.token)).email).toBe(a);
  });
});

// ---- Google ----------------------------------------------------------------------------------- //

const APP = "http://127.0.0.1:57000/signin/done";

async function googleStart(o: { redirect?: string; token?: string } = {}) {
  const q = o.redirect === undefined ? `?redirect=${encodeURIComponent(APP)}` : `?redirect=${encodeURIComponent(o.redirect)}`;
  return call(`/account/google/start${q}`, { token: o.token, env: GOOGLE });
}

/** Start, then come back from Google with a code, in the same browser (cookie) unless told not to. */
async function googleRoundTrip(claims: Record<string, unknown>, o: { token?: string; cookie?: string | null; extra?: string } = {}) {
  const s = await googleStartRaw(o.token);
  const loc = new URL(s.headers.get("Location")!);
  const state = loc.searchParams.get("state")!;
  const cookie = o.cookie === null ? undefined : (o.cookie ?? `${STATE_COOKIE}=${state}`);
  const f = stubFetch({ idClaims: claims });
  const r = await rawCall(`/account/google/callback?state=${encodeURIComponent(state)}&code=abc${o.extra ?? ""}`,
                          cookie ? { Cookie: cookie } : {});
  return { start: s, loc, state, res: r, google: f.google };
}

import worker from "../src/index";
async function rawCall(path: string, headers: Record<string, string> = {}) {
  return worker.fetch(new Request(`https://broker.test${path}`, { headers }), { ...env, ...GOOGLE } as Env);
}
async function googleStartRaw(token?: string) {
  return rawCall(`/account/google/start?redirect=${encodeURIComponent(APP)}`, token ? { Authorization: `Bearer ${token}` } : {});
}

const goodClaims = (email: string) => ({
  iss: "https://accounts.google.com", aud: GOOGLE.GOOGLE_CLIENT_ID, email, email_verified: true });

const tokenFrom = (res: Response) => {
  const loc = res.headers.get("Location")!;
  expect(loc.startsWith(`${APP}#token=`)).toBe(true);
  return decodeURIComponent(loc.split("#token=")[1]!);
};

describe("Google: start", () => {
  it("302s to Google with PKCE (S256), openid email scope, a state, and a browser-binding cookie", async () => {
    const s = await googleStartRaw();
    expect(s.status).toBe(302);
    const loc = new URL(s.headers.get("Location")!);
    expect(`${loc.origin}${loc.pathname}`).toBe(GOOGLE_AUTH);
    const q = loc.searchParams;
    expect(q.get("client_id")).toBe(GOOGLE.GOOGLE_CLIENT_ID);
    expect(q.get("redirect_uri")).toBe("https://broker.test/account/google/callback");
    expect(q.get("scope")).toBe("openid email");
    expect(q.get("code_challenge_method")).toBe("S256");
    expect(q.get("code_challenge")).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(q.get("state")!.length).toBeGreaterThan(20);
    const cookie = s.headers.get("Set-Cookie")!;
    expect(cookie).toContain(`${STATE_COOKIE}=${q.get("state")}`);
    expect(cookie).toMatch(/HttpOnly/);
    expect(cookie).toMatch(/Secure/);
  });

  it("only allows the app's own loopback callback as the redirect", async () => {
    for (const bad of ["https://evil.example/cb", "http://evil.example/cb", "https://127.0.0.1/cb",
                       "http://127.0.0.1.evil.example/cb", "http://user:pw@127.0.0.1/cb", "javascript:alert(1)",
                       "http://[::1]:5/cb", "not a url"]) {
      const r = await googleStart({ redirect: bad });
      expect(r.status, bad).toBe(400);
    }
    expect(allowedRedirect("http://localhost:8123/x?y=1#frag")).toBe("http://localhost:8123/x?y=1");
    expect(allowedRedirect("http://127.0.0.1/")).toBe("http://127.0.0.1/");
    // no redirect given: the desktop app's default address
    const d = await call("/account/google/start", { env: GOOGLE });
    expect(d.status).toBe(302);
  });
});

describe("Google: callback", () => {
  it("a new Google e-mail with no device token creates an account and 302s to the app with #token", async () => {
    const email = freshEmail();
    const { res, google } = await googleRoundTrip(goodClaims(email.toUpperCase()));
    expect(res.status).toBe(302);
    const token = tokenFrom(res);
    expect((await me(token)).email).toBe(email);
    expect(google[0]).toMatchObject({ code: "abc", grant_type: "authorization_code", client_secret: "gsecret",
                                      redirect_uri: "https://broker.test/account/google/callback" });
    expect(google[0]!.code_verifier!.length).toBeGreaterThanOrEqual(43);
    expect(res.headers.get("Set-Cookie")).toMatch(/Max-Age=0/);
  });

  it("an e-mail that already has an account logs in to it (the device switches)", async () => {
    const owner = await newAccount();
    await givePass(owner.id);
    const email = freshEmail();
    await env.DB.prepare("UPDATE accounts SET email = ? WHERE account_id = ?").bind(email, owner.id).run();
    const device = await newAccount();
    const { res } = await googleRoundTrip(goodClaims(email), { token: device.token });
    const token = tokenFrom(res);
    expect((await me(token)).account_id).toBe(owner.id);
    expect((await usage(token)).tier).toBe("pass30");
  });

  it("a start carrying the device token attaches a new e-mail to that account, keeping its pass", async () => {
    const device = await newAccount();
    await givePass(device.id, "pass90");
    const email = freshEmail();
    const { res } = await googleRoundTrip(goodClaims(email), { token: device.token });
    const token = tokenFrom(res);
    expect(await me(token)).toEqual({ account_id: device.id, email });
    expect((await usage(token)).tier).toBe("pass90");
  });

  it("the state is one-time: replaying the callback gets the expired page", async () => {
    const { state } = await googleRoundTrip(goodClaims(freshEmail()));
    const r = await rawCall(`/account/google/callback?state=${state}&code=abc`, { Cookie: `${STATE_COOKIE}=${state}` });
    expect(r.status).toBe(400);
    expect(r.headers.get("Location")).toBeNull();
  });

  it("an unknown or missing state never redirects anywhere", async () => {
    for (const p of ["/account/google/callback?code=abc", "/account/google/callback?state=forged&code=abc"]) {
      const r = await rawCall(p);
      expect(r.status).toBe(400);
      expect(r.headers.get("Location")).toBeNull();
    }
  });

  it("a different browser (no or wrong cookie) is refused: #error, no account, no token", async () => {
    for (const cookie of [null, `${STATE_COOKIE}=someone-elses`]) {
      const email = freshEmail();
      const { res, google } = await googleRoundTrip(goodClaims(email), { cookie });
      expect(res.headers.get("Location")).toBe(`${APP}#error=signin_failed`);
      expect(google).toHaveLength(0);
      expect(await env.DB.prepare("SELECT 1 FROM accounts WHERE email = ?").bind(email).first()).toBeNull();
    }
  });

  it("cancel at Google, an unverified e-mail, a foreign audience or a token failure end in #error", async () => {
    const email = freshEmail();
    expect((await googleRoundTrip(goodClaims(email), { extra: "&error=access_denied" })).res.headers.get("Location"))
      .toBe(`${APP}#error=cancelled`);
    for (const claims of [{ ...goodClaims(email), email_verified: false },
                          { ...goodClaims(email), aud: "someone-else" },
                          { ...goodClaims(email), iss: "https://evil.example" },
                          { aud: GOOGLE.GOOGLE_CLIENT_ID, iss: "accounts.google.com", email_verified: true }]) {
      const { res } = await googleRoundTrip(claims);
      expect(res.headers.get("Location")).toBe(`${APP}#error=signin_failed`);
    }
    expect(await env.DB.prepare("SELECT 1 FROM accounts WHERE email = ?").bind(email).first()).toBeNull();
  });

  it("reads only a verified e-mail for our client from the id_token", () => {
    const enc = (x: unknown) => btoa(JSON.stringify(x)).replace(/=+$/, "");
    const t = (c: unknown) => `h.${enc(c)}.s`;
    expect(emailFromIdToken(t(goodClaims("A@B.com")), GOOGLE.GOOGLE_CLIENT_ID!)).toBe("a@b.com");
    expect(emailFromIdToken("garbage", GOOGLE.GOOGLE_CLIENT_ID!)).toBeNull();
  });
});
