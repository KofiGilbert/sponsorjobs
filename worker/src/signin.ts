// Passwordless sign-in: an e-mailed one-time code, or "Continue with Google". No passwords.
//
// Both end in accounts.signInWithEmail (log in to the account that owns the e-mail, or attach the
// e-mail to the caller's device account so its pass and usage stay, or create one).
//
// E-mail codes: 6 random digits, valid 10 minutes, stored only as a salted sha256. Each verify call
// spends one of the code's 5 attempts BEFORE the code is compared, in one guarded UPDATE, so even
// parallel guesses get at most 5 tries per code. At most 5 code e-mails per address and per client
// network per hour.
//
// Google: the authorization-code flow with PKCE (S256) and a one-time random `state` kept in D1,
// bound to the browser that started it by a short-lived cookie (so a sign-in link started by
// someone else cannot be finished in your browser). The client secret stays here; the app never
// sees it. The final redirect goes only to the app's own loopback address (127.0.0.1 / localhost),
// carrying the bearer token in the URL FRAGMENT (#token=...), which browsers never send to a server.

import { identify, sha256Hex, signInWithEmail, tokenUrlsafe } from "./accounts";
import type { Env } from "./env";
import { mailerFrom } from "./mailer";

const json = (data: unknown, status = 200, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", ...headers } });

async function body(req: Request): Promise<Record<string, unknown>> {
  try {
    const b = await req.json();
    return b && typeof b === "object" && !Array.isArray(b) ? (b as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

export const CODE_TTL_SECONDS = 10 * 60;
export const MAX_CODE_ATTEMPTS = 5;
export const SENDS_PER_HOUR = 5;          // per e-mail address, and separately per client network
export const OAUTH_STATE_TTL_SECONDS = 10 * 60;

/** Trimmed and lowercased, or null when it is not a plausible address. */
export function normaliseEmail(v: unknown): string | null {
  const e = String(v ?? "").trim().toLowerCase();
  if (e.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@.]{2,}$/.test(e)) return null;
  return e;
}

/** A uniform 6-digit code (rejection sampling, so no digit pattern is likelier than another). */
export function newCode(): string {
  const limit = Math.floor(0x1_0000_0000 / 1_000_000) * 1_000_000;
  const buf = new Uint32Array(1);
  for (;;) {
    crypto.getRandomValues(buf);
    if (buf[0]! < limit) return String(buf[0]! % 1_000_000).padStart(6, "0");
  }
}

const codeHash = (salt: string, email: string, code: string) => sha256Hex(`${salt}:${email}:${code}`);

function sameHex(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  return x.byteLength === y.byteLength && crypto.subtle.timingSafeEqual(x, y);
}

const clientIp = (req: Request) => req.headers.get("CF-Connecting-IP") || "unknown";

// ---- e-mail code ------------------------------------------------------------------------------ //

const emailOff = () => json({ error: "email sign-in is not configured" }, 503);

/** Count one code e-mail for this address and this network this hour; false when either is at the
 * limit. Guarded upserts (no read-then-write), so parallel requests cannot slip past. */
async function allowSend(db: D1Database, email: string, ip: string, now: number): Promise<boolean> {
  const hour = Math.floor(now / 3600);
  const upsert = (key: string) => db.prepare(
    "INSERT INTO send_limits(key, hour, n) VALUES(?, ?, 1) " +
    "ON CONFLICT(key, hour) DO UPDATE SET n = n + 1 WHERE n < ? RETURNING n").bind(key, hour, SENDS_PER_HOUR);
  const [, byEmail, byIp] = await db.batch<{ n: number }>([
    db.prepare("DELETE FROM send_limits WHERE hour < ?").bind(hour),
    upsert(`e:${await sha256Hex(email)}`),
    upsert(`i:${await sha256Hex(ip)}`),
  ]);
  return byEmail!.results.length > 0 && byIp!.results.length > 0;
}

export async function emailStart(req: Request, env: Env, now: number): Promise<Response> {
  const mailer = mailerFrom(env);
  if (!mailer) return emailOff();
  const email = normaliseEmail((await body(req)).email);
  if (!email) return json({ error: "enter a valid email address", reason: "bad_email" }, 400);
  if (!(await allowSend(env.DB, email, clientIp(req), now))) {
    return json({ error: "too many codes requested; wait a while and try again", reason: "rate_limited" }, 429);
  }
  const code = newCode();
  const salt = tokenUrlsafe(16);
  // A new code replaces any earlier one for this address (and its attempt count).
  await env.DB.prepare(
    "INSERT INTO email_codes(email, code_hash, salt, expires, attempts) VALUES(?, ?, ?, ?, 0) " +
    "ON CONFLICT(email) DO UPDATE SET code_hash = excluded.code_hash, salt = excluded.salt, " +
    "expires = excluded.expires, attempts = 0")
    .bind(email, await codeHash(salt, email, code), salt, now + CODE_TTL_SECONDS).run();
  try {
    await mailer.send({
      to: email,
      subject: `Your SponsorJobs code is ${code}`,
      text: `Your SponsorJobs code is ${code}. It expires in 10 minutes.\n\n` +
            "If you did not ask for it, you can ignore this email.\n",
    });
  } catch (err) {
    console.error(`sign-in code e-mail failed (${mailer.name}):`, err instanceof Error ? err.message : err);
    return json({ error: "could not send the code; try again in a moment" }, 502);
  }
  // One more e-mail today, for the admin dashboard's Resend quota line. A counting hiccup must
  // never fail a sign-in whose code has already gone out.
  try {
    await env.DB.prepare("INSERT INTO email_sends(day, n) VALUES(?, 1) ON CONFLICT(day) DO UPDATE SET n = n + 1")
      .bind(new Date(now * 1000).toISOString().slice(0, 10)).run();
  } catch (err) {
    console.error("email_sends count failed:", err instanceof Error ? err.message : err);
  }
  return json({ sent: true });
}

export async function emailVerify(req: Request, env: Env, now: number): Promise<Response> {
  if (!mailerFrom(env)) return emailOff();
  const b = await body(req);
  const email = normaliseEmail(b.email);
  const code = String(b.code ?? "").replace(/\s+/g, "");
  if (!email || !/^\d{6}$/.test(code)) return json({ error: "that code is not right", reason: "bad_code" }, 400);
  const db = env.DB;
  // Spend one attempt FIRST (atomically, only while the code is live and has attempts left).
  const row = await db.prepare(
    "UPDATE email_codes SET attempts = attempts + 1 WHERE email = ? AND attempts < ? AND expires > ? " +
    "RETURNING code_hash, salt, attempts")
    .bind(email, MAX_CODE_ATTEMPTS, now).first<{ code_hash: string; salt: string; attempts: number }>();
  if (!row) {
    await db.prepare("DELETE FROM email_codes WHERE email = ? AND (expires <= ? OR attempts >= ?)")
      .bind(email, now, MAX_CODE_ATTEMPTS).run();
    return json({ error: "this code has expired; ask for a new one", reason: "expired" }, 400);
  }
  if (!sameHex(row.code_hash, await codeHash(row.salt, email, code))) {
    if (row.attempts >= MAX_CODE_ATTEMPTS) {          // that was the last try: burn the code
      await db.prepare("DELETE FROM email_codes WHERE email = ? AND code_hash = ?").bind(email, row.code_hash).run();
    }
    return json({ error: "that code is not right", reason: "bad_code" }, 400);
  }
  // Consume it: only one request can, even if the same right code arrives twice at once.
  const used = await db.prepare("DELETE FROM email_codes WHERE email = ? AND code_hash = ? RETURNING email")
    .bind(email, row.code_hash).first();
  if (!used) return json({ error: "this code has expired; ask for a new one", reason: "expired" }, 400);
  const { account_id, token } = await signInWithEmail(db, email, await identify(req, env));
  return json({ token, account_id, email });
}

// ---- Continue with Google --------------------------------------------------------------------- //

export const GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth";
export const GOOGLE_TOKEN = "https://oauth2.googleapis.com/token";
export const STATE_COOKIE = "sj_gstate";
const DEFAULT_APP_CALLBACK = "http://127.0.0.1:57000/";

const googleOff = () => json({ error: "google sign-in is not configured" }, 503);
const googleOn = (env: Env) => Boolean(env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET);

/** The app's callback, only when it is the person's own machine: http://127.0.0.1[:port]/... or
 * http://localhost[:port]/..., no credentials in it. Anything else is refused (no open redirect,
 * and a token can never be sent to someone else's site). The fragment is dropped. */
export function allowedRedirect(raw: string): string | null {
  let u: URL;
  try { u = new URL(raw); } catch { return null; }
  if (u.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(u.hostname)) return null;
  if (u.username || u.password) return null;
  u.hash = "";
  return u.toString();
}

const b64url = (bytes: Uint8Array) => {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};

const callbackUri = (req: Request, env: Env) =>
  env.GOOGLE_REDIRECT_URI || `${new URL(req.url).origin}/account/google/callback`;

const cookie = (value: string, maxAge: number) =>
  `${STATE_COOKIE}=${value}; Path=/account/google; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Lax`;

function redirect(location: string, extra: Record<string, string> = {}): Response {
  return new Response(null, { status: 302, headers: {
    Location: location, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", ...extra } });
}

export async function googleStart(req: Request, env: Env, now: number): Promise<Response> {
  if (!googleOn(env)) return googleOff();
  const raw = new URL(req.url).searchParams.get("redirect") || DEFAULT_APP_CALLBACK;
  const target = allowedRedirect(raw);
  if (!target) return json({ error: "redirect must be the app's own http://127.0.0.1 or http://localhost address" }, 400);
  // A device token on the start request (e.g. the desktop shell's loadURL extraHeaders) means
  // "attach to this account" if the Google e-mail is new; a plain browser visit just signs in.
  const caller = await identify(req, env);
  const state = tokenUrlsafe(24);
  const verifier = tokenUrlsafe(48);
  const challenge = b64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
  await env.DB.batch([
    env.DB.prepare("DELETE FROM oauth_states WHERE expires <= ?").bind(now),
    env.DB.prepare("INSERT INTO oauth_states(state_hash, redirect, account_id, verifier, expires) VALUES(?, ?, ?, ?, ?)")
      .bind(await sha256Hex(state), target, caller, verifier, now + OAUTH_STATE_TTL_SECONDS),
  ]);
  const q = new URLSearchParams({
    client_id: env.GOOGLE_CLIENT_ID!,
    redirect_uri: callbackUri(req, env),
    response_type: "code",
    scope: "openid email",
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
    access_type: "online",
    prompt: "select_account",
  });
  return redirect(`${GOOGLE_AUTH}?${q}`, { "Set-Cookie": cookie(state, OAUTH_STATE_TTL_SECONDS) });
}

function readCookie(req: Request, name: string): string {
  for (const part of (req.headers.get("Cookie") ?? "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  return "";
}

/** The verified e-mail inside Google's id_token. Its signature is not re-checked: the token came
 * straight from Google's token endpoint over TLS in our own server-side exchange (as in
 * backend/google_oauth.py). The audience must still be our client, and the e-mail verified. */
export function emailFromIdToken(idToken: string, clientId: string): string | null {
  try {
    const part = idToken.split(".")[1] ?? "";
    const pad = part.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (part.length % 4)) % 4);
    const claims = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(pad), (c) => c.charCodeAt(0))));
    const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (!aud.includes(clientId)) return null;
    if (!["accounts.google.com", "https://accounts.google.com"].includes(claims.iss)) return null;
    if (claims.email_verified !== true && claims.email_verified !== "true") return null;
    return normaliseEmail(claims.email);
  } catch {
    return null;
  }
}

const expiredPage = () => new Response(
  "<!doctype html><meta charset=utf-8><title>SponsorJobs</title>" +
  "<p style=\"font:16px system-ui;margin:3em auto;max-width:32em\">This sign-in link has expired or was " +
  "already used. Go back to SponsorJobs and choose Continue with Google again.</p>",
  { status: 400, headers: { "content-type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                            "Set-Cookie": cookie("", 0) } });

export async function googleCallback(req: Request, env: Env, now: number): Promise<Response> {
  if (!googleOn(env)) return googleOff();
  const p = new URL(req.url).searchParams;
  const state = p.get("state") ?? "";
  if (!state) return expiredPage();
  // One-time: the row is consumed by this lookup, whatever happens next.
  const row = await env.DB.prepare(
    "DELETE FROM oauth_states WHERE state_hash = ? RETURNING redirect, account_id, verifier, expires")
    .bind(await sha256Hex(state)).first<{ redirect: string; account_id: string | null; verifier: string; expires: number }>();
  if (!row || row.expires <= now) return expiredPage();
  const clear = { "Set-Cookie": cookie("", 0) };
  const fail = (why: string) => redirect(`${row.redirect}#error=${why}`, clear);
  // The browser finishing must be the one that started (login-CSRF guard).
  if (!sameHex(readCookie(req, STATE_COOKIE), state)) return fail("signin_failed");
  if (p.get("error")) return fail("cancelled");          // e.g. the person pressed Cancel at Google
  const code = p.get("code");
  if (!code) return fail("signin_failed");
  let email: string | null = null;
  try {
    const res = await fetch(GOOGLE_TOKEN, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        code, client_id: env.GOOGLE_CLIENT_ID!, client_secret: env.GOOGLE_CLIENT_SECRET!,
        redirect_uri: callbackUri(req, env), grant_type: "authorization_code", code_verifier: row.verifier,
      }),
    });
    if (!res.ok) throw new Error(`google token ${res.status}: ${(await res.text()).slice(0, 300)}`);
    const idToken = ((await res.json()) as { id_token?: string }).id_token;
    email = idToken ? emailFromIdToken(idToken, env.GOOGLE_CLIENT_ID!) : null;
  } catch (err) {
    console.error("google code exchange failed:", err instanceof Error ? err.message : err);
  }
  if (!email) return fail("signin_failed");
  const { token } = await signInWithEmail(env.DB, email, row.account_id);
  return redirect(`${row.redirect}#token=${encodeURIComponent(token)}`, clear);
}
