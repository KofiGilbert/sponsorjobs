// Accounts and bearer-token identity, ported from backend/accounts.py: the anonymous device
// account, plus the passwordless "sign in with an e-mail" rule (signInWithEmail) shared by the
// e-mail code and Google routes in signin.ts.
//
// Same token scheme as Python, so a token minted by either broker resolves in the other after a
// data move: 32 random bytes as url-safe base64 (secrets.token_urlsafe(32)), stored only as the
// sha256 hex of its UTF-8 bytes. The account id is "acct_" + 16 hex chars (secrets.token_hex(8)).

import type { Env } from "./env";

const enc = new TextEncoder();

function hex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function sha256Hex(s: string): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}

function randomBytes(n: number): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(n));
}

/** Python's secrets.token_urlsafe: base64url without padding. */
export function tokenUrlsafe(nbytes: number): string {
  let bin = "";
  for (const b of randomBytes(nbytes)) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function newAccountId(): string {
  return "acct_" + hex(randomBytes(8).buffer as ArrayBuffer);
}

/** Create an anonymous account and its first token. The raw token is returned once and never
 * stored; both rows are written in one batch so an account never exists without its token. */
export async function register(db: D1Database): Promise<{ account_id: string; token: string }> {
  const account_id = newAccountId();
  const token = tokenUrlsafe(32);
  await db.batch([
    db.prepare("INSERT INTO accounts(account_id) VALUES(?)").bind(account_id),
    db.prepare("INSERT INTO tokens(token_hash, account_id) VALUES(?, ?)").bind(await sha256Hex(token), account_id),
  ]);
  return { account_id, token };
}

/** Bearer token -> account id, or null. The lookup is by hash; the stored hash is then compared
 * in constant time as well, so nothing about a near-miss token can be learned from timing. */
export async function resolve(db: D1Database, token: string): Promise<string | null> {
  if (!token) return null;
  const h = await sha256Hex(token);
  const row = await db.prepare("SELECT token_hash, account_id FROM tokens WHERE token_hash = ?")
    .bind(h).first<{ token_hash: string; account_id: string }>();
  if (!row) return null;
  const a = enc.encode(row.token_hash);
  const b = enc.encode(h);
  return a.byteLength === b.byteLength && crypto.subtle.timingSafeEqual(a, b) ? row.account_id : null;
}

export async function emailOf(db: D1Database, accountId: string): Promise<string | null> {
  const row = await db.prepare("SELECT email FROM accounts WHERE account_id = ?")
    .bind(accountId).first<{ email: string | null }>();
  return row?.email || null;
}

/** Who is calling: a valid bearer token wins; otherwise, ONLY when DEV_HEADER_AUTH=1, the
 * X-Tailor-User header (the Python broker's dev fallback). In production an unknown or missing
 * token is simply no user (401). */
export async function identify(req: Request, env: Env): Promise<string | null> {
  const auth = req.headers.get("Authorization") ?? "";
  if (auth.startsWith("Bearer ")) {
    const id = await resolve(env.DB, auth.slice("Bearer ".length).trim());
    if (id) return id;
  }
  if (env.DEV_HEADER_AUTH === "1") return req.headers.get("X-Tailor-User") || null;
  return null;
}

/** Count one new account for this network today; false once the daily limit is reached. One
 * guarded upsert, so parallel sign-ups from one IP cannot slip past the limit. Older days are
 * dropped in the same batch to keep the table small. */
export async function allowRegistration(db: D1Database, ip: string, day: string, limit: number): Promise<boolean> {
  if (limit <= 0) return false;
  const [, counted] = await db.batch<{ n: number }>([
    db.prepare("DELETE FROM register_limits WHERE day < ?").bind(day),
    db.prepare(
      "INSERT INTO register_limits(ip_hash, day, n) VALUES(?, ?, 1) " +
      "ON CONFLICT(ip_hash, day) DO UPDATE SET n = n + 1 WHERE n < ? RETURNING n")
      .bind(await sha256Hex(ip), day, limit),
  ]);
  return counted!.results.length > 0;
}

/** A new bearer token for an existing account (the raw value is returned once, never stored). */
export async function issueToken(db: D1Database, accountId: string): Promise<string> {
  const token = tokenUrlsafe(32);
  await db.prepare("INSERT INTO tokens(token_hash, account_id) VALUES(?, ?)")
    .bind(await sha256Hex(token), accountId).run();
  return token;
}

export async function accountByEmail(db: D1Database, email: string): Promise<string | null> {
  const row = await db.prepare("SELECT account_id FROM accounts WHERE email = ?")
    .bind(email).first<{ account_id: string }>();
  return row?.account_id ?? null;
}

/** The one rule both sign-in methods use, once the e-mail is PROVEN (a correct code, or Google's
 * verified e-mail):
 *  1. the e-mail already belongs to an account -> log in to it (a fresh token; the device switches
 *     to that account; the caller's anonymous account is left as it was);
 *  2. otherwise, the caller's own account has no e-mail yet -> attach the e-mail to it, so its
 *     pass, credits and usage stay (account id unchanged; a fresh token is returned, the old one
 *     keeps working);
 *  3. otherwise (no caller, or the caller is already signed in as another e-mail) -> a new account
 *     with this e-mail. An existing e-mail is never silently replaced.
 * The UNIQUE e-mail column settles races: whoever loses a parallel claim falls back to rule 1. */
export async function signInWithEmail(db: D1Database, email: string, callerId: string | null):
    Promise<{ account_id: string; token: string }> {
  const existing = await accountByEmail(db, email);
  if (existing) return { account_id: existing, token: await issueToken(db, existing) };
  if (callerId) {
    try {
      const r = await db.prepare("UPDATE accounts SET email = ? WHERE account_id = ? AND email IS NULL")
        .bind(email, callerId).run();
      if ((r.meta.changes ?? 0) > 0) return { account_id: callerId, token: await issueToken(db, callerId) };
    } catch {
      return loginExisting(db, email);                 // someone else claimed the e-mail just now
    }
  }
  const account_id = newAccountId();
  const token = tokenUrlsafe(32);
  try {
    await db.batch([
      db.prepare("INSERT INTO accounts(account_id, email) VALUES(?, ?)").bind(account_id, email),
      db.prepare("INSERT INTO tokens(token_hash, account_id) VALUES(?, ?)").bind(await sha256Hex(token), account_id),
    ]);
  } catch {
    return loginExisting(db, email);
  }
  return { account_id, token };
}

async function loginExisting(db: D1Database, email: string) {
  const id = await accountByEmail(db, email);
  if (!id) throw new Error("e-mail claim failed without an owner");
  return { account_id: id, token: await issueToken(db, id) };
}
