// Who may read the job list and search it (src/feed.ts, src/search.ts): a SponsorJobs install
// holding a valid broker account token. Every install has one: the app registers an anonymous
// account on first need (ui/app.py _account_token), free or paid alike. A copycat app has none, so
// it cannot spend our free Cloudflare allowance or our crawl.
//
// The answer for a token is kept in this isolate for a minute, so a burst of feed files or search
// pages costs one broker-database lookup, not one per request (and does not queue behind billing on
// that database). The cost: a token revoked or an account suspended in that minute keeps reading
// the job list (public postings only) for at most ACCOUNT_CACHE_MS more. Tokens are never logged;
// the cache key is the token's sha256.

import { resolve, sha256Hex } from "./accounts";
import type { Env } from "./env";

export const ACCOUNT_CACHE_MS = 60_000;
const ACCOUNT_CACHE_MAX = 5_000;
const cache = new Map<string, { id: string; until: number }>();

/** Tests: forget every cached answer. */
export function forgetAccounts(): void {
  cache.clear();
}

/** The account behind the request's `Authorization: Bearer <token>`, or null. Same rule as
 * accounts.identify (a valid token wins; the X-Tailor-User header only when DEV_HEADER_AUTH=1),
 * with valid tokens remembered for a minute. An unknown token is never cached, so a fresh
 * registration works at once. */
export async function accountFor(req: Request, env: Env, nowMs: number): Promise<string | null> {
  const auth = req.headers.get("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice("Bearer ".length).trim() : "";
  if (token) {
    const key = await sha256Hex(token);
    const hit = cache.get(key);
    if (hit && hit.until > nowMs) return hit.id;
    if (hit) cache.delete(key);
    const id = await resolve(env.DB, token);
    if (id) {
      if (cache.size >= ACCOUNT_CACHE_MAX) {
        const oldest = cache.keys().next().value;     // a Map keeps insertion order
        if (oldest !== undefined) cache.delete(oldest);
      }
      cache.set(key, { id, until: nowMs + ACCOUNT_CACHE_MS });
      return id;
    }
  }
  if (env.DEV_HEADER_AUTH === "1") return req.headers.get("X-Tailor-User") || null;
  return null;
}

/** The client's network, as Cloudflare's edge reports it (a client cannot set this header). */
export const clientIp = (req: Request): string => req.headers.get("CF-Connecting-IP") || "unknown";

/** One Workers rate-limit check. A deployment (or test) without the binding has no limit here. */
export async function overLimit(limiter: RateLimit | undefined, key: string): Promise<boolean> {
  if (!limiter) return false;
  return !(await limiter.limit({ key })).success;
}
