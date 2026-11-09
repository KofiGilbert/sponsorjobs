// Connect GitHub through the SponsorJobs GitHub App (src/github.ts). GitHub is stubbed (no
// network). What must hold: 503 until configured; the state is bound to the app's verifier and
// the one-time key reaches only the app's loopback page; the token comes out once, never after
// 5 minutes, never with the wrong verifier or key, never in any other response or a log line;
// the first connect detours through the install page; refresh uses the secret server-side.
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Env } from "../src/env";
import {
  GITHUB_AUTHORIZE, GITHUB_INSTALLATIONS, GITHUB_TOKEN, LIMITS, READY_TTL_SECONDS, s256, STATE_COOKIE, seal, unseal,
} from "../src/github";
import { call, env, freshIp } from "./helpers";

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

const APP: Partial<Env> = { GITHUB_APP_CLIENT_ID: "Iv23liTESTCLIENT", GITHUB_APP_CLIENT_SECRET: "gh-secret-not-real",
                            GITHUB_APP_SLUG: "sponsorjobs" };
const ACCESS = "ghu_ACCESSTOKENnotreal1234567890";
const REFRESH = "ghr_REFRESHTOKENnotreal1234567890";
const LOOPBACK = "http://127.0.0.1:57000/github/app/done";

let seq = 0;
const newVerifier = () => `verifier-${++seq}-${"x".repeat(60)}-${Date.now()}`;

interface Stub { token: Record<string, string>[]; installs: number }

/** Stub github.com: the token endpoint and GET /user/installations. */
function stubGitHub(o: { installs?: number[]; tokenStatus?: number; tokenError?: string; refreshError?: string } = {}) {
  const seen: Stub = { token: [], installs: 0 };
  const installs = [...(o.installs ?? [1])];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const req = new Request(input as RequestInfo, init as RequestInit);
    if (req.url === GITHUB_TOKEN) {
      const form = Object.fromEntries(await req.formData()) as Record<string, string>;
      seen.token.push(form);
      if (form.grant_type === "refresh_token") {
        if (o.refreshError) return new Response(JSON.stringify({ error: o.refreshError }), { status: 200 });
        return Response.json({ access_token: "ghu_REFRESHED", refresh_token: "ghr_NEW", expires_in: 28800,
                               refresh_token_expires_in: 15897600, token_type: "bearer", scope: "" });
      }
      if (o.tokenError) return new Response(JSON.stringify({ error: o.tokenError }), { status: o.tokenStatus ?? 200 });
      return Response.json({ access_token: ACCESS, refresh_token: REFRESH, expires_in: 28800,
                             refresh_token_expires_in: 15897600, token_type: "bearer", scope: "" });
    }
    if (req.url === GITHUB_INSTALLATIONS) {
      seen.installs++;
      return Response.json({ total_count: installs.length ? installs.shift() : 1, installations: [] });
    }
    throw new Error(`unexpected fetch ${req.url}`);
  });
  return seen;
}

async function connect(verifier: string, ip = freshIp(), e: Partial<Env> = APP) {
  const state = await s256(verifier);
  const r = await call(`/github/connect?state=${state}&redirect=${encodeURIComponent(LOOPBACK)}`,
                       { env: e, headers: { "CF-Connecting-IP": ip } });
  return { state, r };
}

/** The raw callback response (a 302) with the given query, sent with the state cookie. */
async function callback(query: string, state: string, withCookie = true) {
  const { default: worker } = await import("../src/index");
  const headers: Record<string, string> = withCookie ? { Cookie: `${STATE_COOKIE}=${state}` } : {};
  return worker.fetch(new Request(`https://broker.test/github/callback?${query}`, { headers }),
                      { ...env, ...APP } as Env);
}

async function connectRaw(verifier: string) {
  const { default: worker } = await import("../src/index");
  const state = await s256(verifier);
  const res = await worker.fetch(
    new Request(`https://broker.test/github/connect?state=${state}&redirect=${encodeURIComponent(LOOPBACK)}`,
                { headers: { "CF-Connecting-IP": freshIp() } }),
    { ...env, ...APP } as Env);
  return { state, res };
}

const fragment = (res: Response) => new URLSearchParams(new URL(res.headers.get("Location")!).hash.slice(1));

/** A whole connect up to the loopback hand-off; returns the verifier and the one-time key. */
async function connected(installs: number[] = [1]) {
  const verifier = newVerifier();
  const { state } = await connectRaw(verifier);
  const seen = stubGitHub({ installs });
  const res = await callback(`code=c1&state=${state}`, state);
  return { verifier, state, res, seen, nonce: fragment(res).get("nonce") ?? "" };
}

const claim = (b: unknown, ip = freshIp()) =>
  call("/github/claim", { body: b, env: APP, headers: { "CF-Connecting-IP": ip } });

describe("not configured", () => {
  it("answers 503 on every route until all three settings are set", async () => {
    for (const e of [{}, { ...APP, GITHUB_APP_CLIENT_SECRET: "" }, { ...APP, GITHUB_APP_SLUG: "" }]) {
      expect((await call("/github/available", { env: e })).status).toBe(503);
      expect((await call(`/github/connect?state=${"a".repeat(43)}&redirect=${encodeURIComponent(LOOPBACK)}`, { env: e })).status).toBe(503);
      expect((await call("/github/callback?code=x", { env: e })).status).toBe(503);
      expect((await call("/github/claim", { body: { verifier: "v".repeat(50) }, env: e })).status).toBe(503);
      const r = await call("/github/refresh", { body: { refresh_token: REFRESH }, env: e });
      expect(r.status).toBe(503);
      expect(r.data.reason).toBe("not_configured");
    }
  });

  it("says where to manage the App once configured", async () => {
    const r = await call("/github/available", { env: APP });
    expect(r.status).toBe(200);
    expect(r.data).toMatchObject({ configured: true, slug: "sponsorjobs", manage_url: "https://github.com/settings/installations" });
    expect(r.text).not.toContain("gh-secret");
  });
});

describe("connect", () => {
  it("sends the browser to GitHub's authorize page with PKCE and the app's state", async () => {
    const { state, res } = await connectRaw(newVerifier());
    expect(res.status).toBe(302);
    const loc = new URL(res.headers.get("Location")!);
    expect(`${loc.origin}${loc.pathname}`).toBe(GITHUB_AUTHORIZE);
    expect(loc.searchParams.get("client_id")).toBe(APP.GITHUB_APP_CLIENT_ID);
    expect(loc.searchParams.get("state")).toBe(state);
    expect(loc.searchParams.get("redirect_uri")).toBe("https://broker.test/github/callback");
    expect(loc.searchParams.get("code_challenge_method")).toBe("S256");
    expect(loc.searchParams.get("code_challenge")).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(loc.toString()).not.toContain("gh-secret");
    expect(res.headers.get("Set-Cookie")).toContain(`${STATE_COOKIE}=${state}`);
    expect(res.headers.get("Set-Cookie")).toContain("HttpOnly");
  });

  it("refuses a malformed state and a redirect off the person's machine", async () => {
    expect((await call(`/github/connect?state=short&redirect=${encodeURIComponent(LOOPBACK)}`, { env: APP })).status).toBe(400);
    const state = await s256(newVerifier());
    for (const bad of ["https://evil.example/cb", "http://evil.example/cb", "sponsorjobs://x", ""]) {
      expect((await call(`/github/connect?state=${state}&redirect=${encodeURIComponent(bad)}`, { env: APP })).status).toBe(400);
    }
  });

  it("is rate limited per network", async () => {
    const ip = freshIp();
    for (let i = 0; i < LIMITS.connect; i++) expect((await connect(newVerifier(), ip)).r.status).toBe(302);
    expect((await connect(newVerifier(), ip)).r.status).toBe(429);
  });
});

describe("callback and claim", () => {
  it("hands the one-time key only to the loopback page, in the fragment", async () => {
    const { res, nonce, seen } = await connected();
    expect(res.status).toBe(302);
    const loc = new URL(res.headers.get("Location")!);
    expect(`${loc.origin}${loc.pathname}`).toBe(LOOPBACK);
    expect(loc.search).toBe("");
    expect(nonce).toMatch(/^[A-Za-z0-9_-]{40,}$/);
    expect(res.headers.get("Location")).not.toContain(ACCESS);
    // the exchange used the secret and our PKCE verifier
    expect(seen.token[0]).toMatchObject({ client_id: APP.GITHUB_APP_CLIENT_ID, client_secret: "gh-secret-not-real", code: "c1" });
    expect(seen.token[0]!.code_verifier).toBeTruthy();
  });

  it("stores the token only sealed, never in the clear", async () => {
    const { state } = await connected();
    const row = await env.DB.prepare("SELECT * FROM github_connects WHERE state = ?").bind(state).first<Record<string, unknown>>();
    expect(row!.status).toBe("ready");
    expect(JSON.stringify(row)).not.toContain(ACCESS);
    expect(JSON.stringify(row)).not.toContain(REFRESH);
  });

  it("returns the tokens once to the right verifier and key, then they are gone", async () => {
    const { verifier, nonce } = await connected();
    const r = await claim({ verifier, nonce });
    expect(r.status).toBe(200);
    expect(r.data).toMatchObject({ access_token: ACCESS, refresh_token: REFRESH, expires_in: 28800 });
    const again = await claim({ verifier, nonce });
    expect(again.status).toBe(404);
    expect(again.text).not.toContain(ACCESS);
  });

  it("gives a wrong verifier nothing", async () => {
    const { verifier, nonce } = await connected();
    const r = await claim({ verifier: newVerifier(), nonce });
    expect(r.status).toBe(404);
    expect(r.text).not.toContain(ACCESS);
    expect((await claim({ verifier, nonce })).status).toBe(200);     // the real one still works
  });

  it("gives the right verifier without the key nothing (a link someone else started)", async () => {
    const { verifier, nonce } = await connected();
    const r = await claim({ verifier });
    expect(r.status).toBe(202);
    expect(r.data.status).toBe("pending");
    expect(r.text).not.toContain(ACCESS);
    const wrong = await claim({ verifier, nonce: "A".repeat(43) });
    expect(wrong.text).not.toContain(ACCESS);
    expect((await claim({ verifier, nonce })).status).toBe(200);
  });

  it("is gone after 5 minutes", async () => {
    const { verifier, nonce } = await connected();
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(Date.now() + (READY_TTL_SECONDS + 5) * 1000);
    const r = await claim({ verifier, nonce });
    expect([404, 410]).toContain(r.status);
    expect(r.text).not.toContain(ACCESS);
  });

  it("detours a first-time person through the install page, then finishes", async () => {
    const verifier = newVerifier();
    const { state } = await connectRaw(verifier);
    const seen = stubGitHub({ installs: [0, 1] });
    const first = await callback(`code=c1&state=${state}`, state);
    expect(first.status).toBe(302);
    expect(first.headers.get("Location")).toBe(`https://github.com/apps/sponsorjobs/installations/new?state=${state}`);
    expect((await claim({ verifier })).data.status).toBe("pending");
    // GitHub's install-time authorization comes back without our state: the cookie carries it,
    // and its code is exchanged without the PKCE verifier (GitHub started that one itself).
    const second = await callback("code=c2&installation_id=42&setup_action=install", state);
    const nonce = fragment(second).get("nonce")!;
    expect(nonce).toBeTruthy();
    expect(seen.token[1]).toMatchObject({ code: "c2" });
    expect(seen.token[1]!.code_verifier).toBeUndefined();
    expect((await claim({ verifier, nonce })).data.access_token).toBe(ACCESS);
  });

  it("tells the app when the person cancels on GitHub", async () => {
    const verifier = newVerifier();
    const { state } = await connectRaw(verifier);
    stubGitHub();
    const res = await callback(`error=access_denied&state=${state}`, state);
    expect(fragment(res).get("error")).toBe("cancelled");
    const r = await claim({ verifier });
    expect(r.status).toBe(403);
    expect(r.data.reason).toBe("denied");
  });

  it("shows a plain page for a stale or unknown callback", async () => {
    stubGitHub();
    const res = await callback(`code=c&state=${await s256(newVerifier())}`, "", false);
    expect(res.status).toBe(400);
    expect(await res.text()).toContain("Connect GitHub again");
  });

  it("never writes a token to the log", async () => {
    const logs: string[] = [];
    for (const m of ["log", "error", "warn", "info"] as const) {
      vi.spyOn(console, m).mockImplementation((...a: unknown[]) => { logs.push(a.map(String).join(" ")); });
    }
    const { verifier, nonce } = await connected();
    await claim({ verifier, nonce });
    await call("/github/refresh", { body: { refresh_token: REFRESH }, env: APP, headers: { "CF-Connecting-IP": freshIp() } });
    const failing = newVerifier();
    const { state } = await connectRaw(failing);
    vi.mocked(globalThis.fetch).mockRestore();
    stubGitHub({ tokenError: "bad_verification_code" });
    await callback(`code=bad&state=${state}`, state);
    const all = logs.join("\n");
    for (const secret of [ACCESS, REFRESH, "ghu_REFRESHED", "ghr_NEW", "gh-secret-not-real", nonce]) {
      expect(all).not.toContain(secret);
    }
  });
});

describe("refresh", () => {
  it("swaps a refresh token for new tokens using the secret", async () => {
    const seen = stubGitHub();
    const r = await call("/github/refresh", { body: { refresh_token: REFRESH }, env: APP, headers: { "CF-Connecting-IP": freshIp() } });
    expect(r.status).toBe(200);
    expect(r.data).toMatchObject({ access_token: "ghu_REFRESHED", refresh_token: "ghr_NEW", expires_in: 28800 });
    expect(seen.token[0]).toMatchObject({ grant_type: "refresh_token", refresh_token: REFRESH, client_secret: "gh-secret-not-real" });
  });

  it("says sign in again when GitHub refuses the refresh token", async () => {
    stubGitHub({ refreshError: "bad_refresh_token" });
    const r = await call("/github/refresh", { body: { refresh_token: REFRESH }, env: APP, headers: { "CF-Connecting-IP": freshIp() } });
    expect(r.status).toBe(400);
    expect(r.data.reason).toBe("bad_refresh_token");
  });

  it("is rate limited per network", async () => {
    stubGitHub();
    const ip = freshIp();
    for (let i = 0; i < LIMITS.refresh; i++) {
      await call("/github/refresh", { body: { refresh_token: REFRESH }, env: APP, headers: { "CF-Connecting-IP": ip } });
    }
    const r = await call("/github/refresh", { body: { refresh_token: REFRESH }, env: APP, headers: { "CF-Connecting-IP": ip } });
    expect(r.status).toBe(429);
  });
});

describe("sealing", () => {
  it("opens only with the same key", async () => {
    const s = await seal("key-one", "hello");
    expect(await unseal("key-one", s)).toBe("hello");
    expect(await unseal("key-two", s)).toBeNull();
  });
});
