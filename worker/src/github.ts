// "Connect GitHub" through the SponsorJobs GitHub App: the one-click flow GitHub Desktop, VS Code
// and Vercel use. The person clicks Connect GitHub in the app, their own browser opens GitHub's
// authorize (and, the first time, install) page where they pick repositories, and the browser
// lands back on the app already connected. No code to copy, no token to paste.
//
// Why a Worker sits in the middle: a GitHub App's web flow needs the client secret to exchange
// the code and to refresh tokens, and a secret shipped inside a desktop app is not a secret. It
// lives only here (GITHUB_APP_CLIENT_SECRET, `wrangler secret put`), and every route answers 503
// not_configured until the App's three settings are set; the desktop then keeps its fallback
// (device flow, or the token box).
//
// The hand-back, step by step:
//  1. The app makes a random verifier, keeps it in memory, and opens
//     /github/connect?state=<base64url(sha256(verifier))>&redirect=<its own loopback page>.
//  2. We remember the state (one row), set a short cookie (GitHub's install-time hop can drop
//     `state`), and send the browser to GitHub's authorize URL with our own PKCE challenge.
//  3. /github/callback exchanges the code with the secret. If the person has not installed the
//     App yet, we send them to its install page (pick repositories, Install) and GitHub comes back
//     here with a fresh code. Then the tokens are sealed with a one-time key, kept at most
//     5 minutes, and the browser goes to the app's loopback page with that key in the URL
//     FRAGMENT (never sent to any server, so never in a log).
//  4. The app's page hands the key to the app, which calls /github/claim with {verifier, nonce}.
//     Both must match; the row is deleted as it is read, so the tokens come out exactly once.
// Why both: the verifier proves "the app that started this", the nonce proves "the browser that
// finished it is on this same machine". A connect link someone else started and sent you can
// never deliver YOUR token to THEM, because the key only ever reaches your own loopback.
//
// Tokens never appear in a log line and in no response other than the one claim (or a refresh).

import { sha256Hex, tokenUrlsafe } from "./accounts";
import type { Env } from "./env";
import { allowedRedirect } from "./signin";

export const GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize";
export const GITHUB_TOKEN = "https://github.com/login/oauth/access_token";
export const GITHUB_INSTALLATIONS = "https://api.github.com/user/installations";
export const MANAGE_URL = "https://github.com/settings/installations";
export const STATE_COOKIE = "sj_ghstate";
export const CONNECT_TTL_SECONDS = 15 * 60;     // time to authorize and pick repositories
export const READY_TTL_SECONDS = 5 * 60;        // a sealed token waits at most this long
export const LIMITS = { connect: 30, claim: 120, refresh: 120 } as const;   // per network per hour

const STATE_RE = /^[A-Za-z0-9_-]{43}$/;          // base64url of a 32-byte sha256, no padding

const json = (data: unknown, status = 200): Response =>
  new Response(JSON.stringify(data), { status, headers: {
    "content-type": "application/json", "Cache-Control": "no-store" } });

const off = () => json({ error: "github connect is not configured", reason: "not_configured" }, 503);

export const githubOn = (env: Env) =>
  Boolean(env.GITHUB_APP_CLIENT_ID && env.GITHUB_APP_CLIENT_SECRET && env.GITHUB_APP_SLUG);

const clientIp = (req: Request) => req.headers.get("CF-Connecting-IP") || "unknown";

export function b64url(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function unb64url(s: string): Uint8Array {
  const pad = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  return Uint8Array.from(atob(pad), (c) => c.charCodeAt(0));
}

/** base64url(sha256(text)): the PKCE S256 transform, and how a verifier becomes its state. */
export async function s256(text: string): Promise<string> {
  return b64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text))));
}

async function body(req: Request): Promise<Record<string, unknown>> {
  try {
    const b = await req.json();
    return b && typeof b === "object" && !Array.isArray(b) ? (b as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** Count one call for this route from this network this hour; false at the limit. A guarded
 * upsert, so parallel calls cannot slip past. */
async function allow(db: D1Database, route: keyof typeof LIMITS, ip: string, now: number): Promise<boolean> {
  const hour = Math.floor(now / 3600);
  const [, hit] = await db.batch<{ n: number }>([
    db.prepare("DELETE FROM github_limits WHERE hour < ?").bind(hour),
    db.prepare("INSERT INTO github_limits(key, hour, n) VALUES(?, ?, 1) " +
               "ON CONFLICT(key, hour) DO UPDATE SET n = n + 1 WHERE n < ? RETURNING n")
      .bind(`${route}:${await sha256Hex(ip)}`, hour, LIMITS[route]),
  ]);
  return hit!.results.length > 0;
}

const limited = () => json({ error: "too many tries; wait a while and try again", reason: "rate_limited" }, 429);

// ---- sealing: the stored tokens are useless without the key that only the person's machine gets

async function keyFrom(nonce: string): Promise<CryptoKey> {
  const raw = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`sj-github-seal:${nonce}`));
  return crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
}

export async function seal(nonce: string, plain: string): Promise<string> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await keyFrom(nonce),
                                                         new TextEncoder().encode(plain)));
  const out = new Uint8Array(iv.length + ct.length);
  out.set(iv); out.set(ct, iv.length);
  return b64url(out);
}

export async function unseal(nonce: string, sealed: string): Promise<string | null> {
  try {
    const all = unb64url(sealed);
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: all.slice(0, 12) }, await keyFrom(nonce),
                                           all.slice(12));
    return new TextDecoder().decode(pt);
  } catch {
    return null;
  }
}

// ---- GitHub calls -------------------------------------------------------------------------- //

export interface Tokens {
  access_token: string;
  refresh_token?: string;
  expires_in?: number;
  refresh_token_expires_in?: number;
}

/** POST to GitHub's token endpoint (code exchange or refresh). The form carries the secret, so
 * neither it nor the answer is ever logged; failures log only GitHub's error code. */
async function tokenCall(env: Env, form: Record<string, string>): Promise<Tokens | { error: string }> {
  let res: Response;
  try {
    res = await fetch(GITHUB_TOKEN, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json",
                 "User-Agent": "SponsorJobs" },
      body: new URLSearchParams({ client_id: env.GITHUB_APP_CLIENT_ID!,
                                  client_secret: env.GITHUB_APP_CLIENT_SECRET!, ...form }),
    });
  } catch {
    return { error: "unreachable" };
  }
  const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  if (res.ok && typeof data.access_token === "string" && data.access_token) {
    const t: Tokens = { access_token: data.access_token };
    if (typeof data.refresh_token === "string" && data.refresh_token) t.refresh_token = data.refresh_token;
    if (typeof data.expires_in === "number") t.expires_in = data.expires_in;
    if (typeof data.refresh_token_expires_in === "number") t.refresh_token_expires_in = data.refresh_token_expires_in;
    return t;
  }
  return { error: typeof data.error === "string" ? data.error : `http_${res.status}` };
}

/** How many installations of this App the token's user can reach; null when GitHub did not say. */
async function installations(token: string): Promise<number | null> {
  try {
    const res = await fetch(GITHUB_INSTALLATIONS, { headers: {
      Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "SponsorJobs" } });
    if (!res.ok) return null;
    const n = ((await res.json()) as { total_count?: unknown }).total_count;
    return typeof n === "number" ? n : null;
  } catch {
    return null;
  }
}

// ---- pages and redirects --------------------------------------------------------------------- //

const callbackUri = (req: Request, env: Env) =>
  env.GITHUB_APP_REDIRECT_URI || `${new URL(req.url).origin}/github/callback`;

const cookie = (value: string, maxAge: number) =>
  `${STATE_COOKIE}=${value}; Path=/github; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Lax`;

function redirect(location: string, extra: Record<string, string> = {}): Response {
  return new Response(null, { status: 302, headers: {
    Location: location, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", ...extra } });
}

function readCookie(req: Request, name: string): string {
  for (const part of (req.headers.get("Cookie") ?? "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  return "";
}

/** A small branded page for the one case with nowhere to send the person: the link is stale. */
export function page(title: string, line: string, status = 400): Response {
  const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]!));
  return new Response(
    "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">" +
    "<title>SponsorJobs</title><style>:root{color-scheme:light dark;--bg:#131316;--ink:#e7e7ea;--muted:#82828c}" +
    "@media (prefers-color-scheme: light){:root{--bg:#f7f7f8;--ink:#18181b;--muted:#71717a}}" +
    "body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);" +
    "font:14px/1.5 system-ui,-apple-system,\"Segoe UI\",Arial,sans-serif;text-align:center}" +
    "h1{font-size:19px;font-weight:600;margin:0}p{margin:6px 0 0;color:var(--muted)}</style></head>" +
    `<body><main><h1>${esc(title)}</h1><p>${esc(line)}</p></main></body></html>`,
    { status, headers: { "content-type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                         "Referrer-Policy": "no-referrer", "Set-Cookie": cookie("", 0) } });
}

const stale = () => page("This link has expired", "Go back to SponsorJobs and click Connect GitHub again.");

// ---- routes ------------------------------------------------------------------------------- //

/** GET /github/available: whether this Worker can run the connect. The app asks before opening a
 * browser, so a missing setting falls back in the app instead of showing the person an error. */
export async function githubAvailable(_req: Request, env: Env): Promise<Response> {
  if (!githubOn(env)) return off();
  return json({ configured: true, slug: env.GITHUB_APP_SLUG, manage_url: MANAGE_URL,
                install_url: `https://github.com/apps/${env.GITHUB_APP_SLUG}/installations/new` });
}

/** GET /github/connect?state=&redirect=: opened in the person's own browser by the app. */
export async function githubConnect(req: Request, env: Env, now: number): Promise<Response> {
  if (!githubOn(env)) return off();
  const p = new URL(req.url).searchParams;
  const state = p.get("state") ?? "";
  if (!STATE_RE.test(state)) return json({ error: "bad state" }, 400);
  const target = allowedRedirect(p.get("redirect") ?? "");
  if (!target) return json({ error: "redirect must be the app's own http://127.0.0.1 or http://localhost address" }, 400);
  if (!(await allow(env.DB, "connect", clientIp(req), now))) return limited();
  const pkce = tokenUrlsafe(48);
  // A second click on the same connect reuses its row (and its PKCE) rather than failing.
  await env.DB.batch([
    env.DB.prepare("DELETE FROM github_connects WHERE expires <= ?").bind(now),
    env.DB.prepare("INSERT INTO github_connects(state, redirect, pkce, status, expires) VALUES(?, ?, ?, 'pending', ?) " +
                   "ON CONFLICT(state) DO NOTHING").bind(state, target, pkce, now + CONNECT_TTL_SECONDS),
  ]);
  const row = await env.DB.prepare("SELECT redirect, pkce, status FROM github_connects WHERE state = ?")
    .bind(state).first<{ redirect: string; pkce: string; status: string }>();
  if (!row || row.redirect !== target || row.status === "ready") return stale();
  const q = new URLSearchParams({
    client_id: env.GITHUB_APP_CLIENT_ID!,
    redirect_uri: callbackUri(req, env),
    state,
    code_challenge: await s256(row.pkce),
    code_challenge_method: "S256",
  });
  return redirect(`${GITHUB_AUTHORIZE}?${q}`, { "Set-Cookie": cookie(state, CONNECT_TTL_SECONDS) });
}

/** GET /github/callback: GitHub sends the browser here after Authorize (and after Install). */
export async function githubCallback(req: Request, env: Env, now: number): Promise<Response> {
  if (!githubOn(env)) return off();
  const p = new URL(req.url).searchParams;
  // GitHub's install-time hop may come back without `state`; the cookie set at connect has it.
  const state = p.get("state") || readCookie(req, STATE_COOKIE);
  if (!STATE_RE.test(state)) return stale();
  const row = await env.DB.prepare(
    "SELECT redirect, pkce, status FROM github_connects WHERE state = ? AND expires > ? AND status IN ('pending', 'installing')")
    .bind(state, now).first<{ redirect: string; pkce: string; status: string }>();
  if (!row) return stale();
  const clear = { "Set-Cookie": cookie("", 0) };
  const back = (fragment: string) => redirect(`${row.redirect}#${fragment}`, clear);
  const deny = async (why: string) => {
    await env.DB.prepare("UPDATE github_connects SET status = 'denied', expires = ? WHERE state = ?")
      .bind(now + READY_TTL_SECONDS, state).run();
    return back(`error=${why}`);
  };
  if (p.get("error")) return deny("cancelled");               // Cancel on GitHub's page
  if (p.get("setup_action") === "request") return deny("approval");   // an org owner must approve
  const code = p.get("code");
  if (!code) return deny("failed");
  // The install-time hop starts its own authorization without our PKCE challenge, so its code is
  // exchanged without the verifier; our own authorize link always carries one.
  const installHop = p.has("installation_id") || p.has("setup_action");
  // (That hop also names no redirect_uri, so none is sent back for it.)
  const ours = { code, redirect_uri: callbackUri(req, env), code_verifier: row.pkce };
  let tokens = await tokenCall(env, installHop ? { code } : ours);
  if ("error" in tokens && !installHop && tokens.error !== "unreachable") {
    tokens = await tokenCall(env, { code });    // a code GitHub issued without our challenge
  }
  if ("error" in tokens) {
    console.error(`github code exchange failed: ${tokens.error}`);
    return deny("failed");
  }
  // First time: the App is not installed on their account yet, so the token reaches no repo.
  // Send them to the install page (they pick repositories there); GitHub then authorizes again
  // and comes back here. Once only: a second pass stores what we have rather than loop.
  if (row.status === "pending" && (await installations(tokens.access_token)) === 0) {
    await env.DB.prepare("UPDATE github_connects SET status = 'installing' WHERE state = ?").bind(state).run();
    const q = new URLSearchParams({ state });
    return redirect(`https://github.com/apps/${env.GITHUB_APP_SLUG}/installations/new?${q}`);
  }
  const nonce = tokenUrlsafe(32);
  await env.DB.prepare(
    "UPDATE github_connects SET status = 'ready', nonce_hash = ?, sealed = ?, expires = ? WHERE state = ?")
    .bind(await sha256Hex(nonce), await seal(nonce, JSON.stringify(tokens)), now + READY_TTL_SECONDS, state).run();
  return back(`nonce=${nonce}`);
}

/** POST /github/claim {verifier, nonce}: the app collects its tokens, exactly once. */
export async function githubClaim(req: Request, env: Env, now: number): Promise<Response> {
  if (!githubOn(env)) return off();
  if (!(await allow(env.DB, "claim", clientIp(req), now))) return limited();
  const b = await body(req);
  const verifier = String(b.verifier ?? "");
  const nonce = String(b.nonce ?? "");
  if (verifier.length < 43 || verifier.length > 128) return json({ error: "bad verifier" }, 400);
  const state = await s256(verifier);
  if (nonce) {
    // Consumed by this read whatever happens next: a wrong key cannot be retried against it.
    const row = await env.DB.prepare(
      "DELETE FROM github_connects WHERE state = ? AND status = 'ready' AND nonce_hash = ? RETURNING sealed, expires")
      .bind(state, await sha256Hex(nonce)).first<{ sealed: string; expires: number }>();
    if (row) {
      if (row.expires <= now) return json({ error: "expired", reason: "expired" }, 410);
      const plain = await unseal(nonce, row.sealed);
      if (!plain) return json({ error: "not found", reason: "not_found" }, 404);
      return json({ ok: true, ...(JSON.parse(plain) as Tokens) });
    }
  }
  const row = await env.DB.prepare("SELECT status, expires FROM github_connects WHERE state = ?")
    .bind(state).first<{ status: string; expires: number }>();
  if (!row || row.expires <= now) return json({ error: "not found", reason: "not_found" }, 404);
  if (row.status === "denied") {
    await env.DB.prepare("DELETE FROM github_connects WHERE state = ?").bind(state).run();
    return json({ error: "cancelled", reason: "denied" }, 403);
  }
  return json({ ok: false, status: "pending" }, 202);
}

/** POST /github/refresh {refresh_token}: a new 8-hour token. GitHub needs the secret for this. */
export async function githubRefresh(req: Request, env: Env, now: number): Promise<Response> {
  if (!githubOn(env)) return off();
  if (!(await allow(env.DB, "refresh", clientIp(req), now))) return limited();
  const rt = String((await body(req)).refresh_token ?? "");
  if (!rt || rt.length > 512) return json({ error: "bad refresh token", reason: "bad_refresh_token" }, 400);
  const t = await tokenCall(env, { grant_type: "refresh_token", refresh_token: rt });
  if ("error" in t) {
    if (t.error === "unreachable") return json({ error: "github is unreachable", reason: "unreachable" }, 502);
    console.error(`github refresh failed: ${t.error}`);
    return json({ error: "sign in again", reason: "bad_refresh_token" }, 400);
  }
  return json({ ok: true, ...t });
}
