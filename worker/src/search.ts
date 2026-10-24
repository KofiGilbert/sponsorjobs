// Server-side job search over the D1 index (docs/server-search-plan.md, phase 2):
//   GET /v1/jobs/search      the Jobs page's board, at most 50 rows a page
//   GET /v1/jobs/candidates  the Auto-apply picker's pool, at most 500 rows a page, plus titles=A|B
//
// The facets mean EXACTLY what sourcing/filters.apply_facets (and, for titles,
// autoapply/picker._matches_focus) mean on the device, and the order is the board's: newest first
// (feedfile.recency_key), or highest pay first with that order breaking ties. The loader
// (scripts/load_search_index.py) precomputes every derived column in Python, so this file only
// compares values. worker/test/search-parity.test.ts checks the two agree on ~50 facet mixes.
//
// Privacy: the request carries only the query, the facets and a cursor; nothing here logs them,
// and the install token (X-Install-Token) is used only as a rate-limit key, never stored or logged.
// Abuse: 60 requests a minute per install token and 300 per network (the Workers rate-limit
// binding), at most 1,000 results deep per query, and counts capped at 200 ("200+").

import type { Env } from "./env";

export const SEARCH_MAX = 50;
export const CANDIDATES_MAX = 500;
export const DEPTH_MAX = 1000;          // refine to see more; the API is not a bulk export
export const COUNT_CAP = 200;           // "200+": counting further was most of a keyword search's cost
const CACHE_SECONDS = 600;              // an index_version lives ~3 h; the cache at most 10 min
const VERSION_TTL_MS = 30_000;          // how long an isolate trusts the index_version it read

type Mode = "search" | "candidates";

export interface Facets {
  q: string; loc: string; days: number; remote: string; visa: string[]; level: string;
  pay: number; sort: "recent" | "pay"; sponsored: boolean; titles: string[];
}

interface Parsed { facets: Facets; limit: number; cursor: string }

export class BadParam extends Error {
  constructor(readonly param: string, message: string) { super(message); }
}

// ---- Python's str.strip() and int() -------------------------------------------------------- //

// Python's whitespace (str.isspace), which differs from JS \s: \x1c-\x1f and \x85 are space in
// Python, ﻿ is not.
const PY_WS = "\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const STRIP = new RegExp(`^[${PY_WS}]+|[${PY_WS}]+$`, "g");
export const pyStrip = (s: string): string => s.replace(STRIP, "");

/** int(x or 0) for days and pay. apply_facets turns a value int() refuses into 0; the API says
 * 400 instead, since the app always sends a number. */
function pyIntParam(name: string, raw: string): number {
  const s = pyStrip(raw);
  if (!s) return 0;
  if (!/^[+-]?\d+(_\d+)*$/.test(s)) throw new BadParam(name, `${name} must be a whole number`);
  const n = Number.parseInt(s.replace(/_/g, ""), 10);
  if (Math.abs(n) > 1_000_000_000) throw new BadParam(name, `${name} is out of range`);
  return n;
}

// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u0008\u000e-\u001b\u007f]/;

function text(a: URLSearchParams, name: string, max: number): string {
  const v = a.get(name) ?? "";
  if (v.length > max) throw new BadParam(name, `${name} is too long (at most ${max} characters)`);
  if (CONTROL.test(v)) throw new BadParam(name, `${name} holds a control character`);
  return v;
}

// ---- parameters --------------------------------------------------------------------------- //

export function parseParams(a: URLSearchParams, mode: Mode): Parsed {
  const max = mode === "search" ? SEARCH_MAX : CANDIDATES_MAX;
  const rawLimit = pyStrip(a.get("limit") ?? "");
  let limit = mode === "search" ? 30 : CANDIDATES_MAX;
  if (rawLimit) {
    if (!/^\d+$/.test(rawLimit)) throw new BadParam("limit", "limit must be a whole number");
    limit = Number.parseInt(rawLimit, 10);
    if (limit < 1 || limit > max) throw new BadParam("limit", `limit must be between 1 and ${max}`);
  }
  const sort = pyStrip(text(a, "sort", 20)).toLowerCase();
  if (sort !== "" && sort !== "recent" && sort !== "pay") {
    throw new BadParam("sort", "sort must be recent or pay");
  }
  const level = pyStrip(text(a, "level", 20)).toLowerCase();
  if (level !== "" && level !== "entry") throw new BadParam("level", "level must be entry or empty");
  const visa = text(a, "visa", 200).split(",").filter((v) => v);
  if (visa.length > 10) throw new BadParam("visa", "at most 10 visa codes");
  for (const v of visa) {
    if (!/^[A-Za-z0-9_-]{1,32}$/.test(v)) throw new BadParam("visa", "visa codes are letters, digits and -");
  }
  let titles: string[] = [];
  const rawTitles = a.get("titles");
  if (rawTitles != null && rawTitles !== "") {
    if (mode !== "candidates") throw new BadParam("titles", "titles is only for /v1/jobs/candidates");
    // picker.candidates drops blank titles but matches the rest as typed (not stripped).
    titles = text(a, "titles", 1000).split("|").filter((t) => pyStrip(t));
    if (titles.length > 10) throw new BadParam("titles", "at most 10 titles");
    for (const t of titles) if (t.length > 100) throw new BadParam("titles", "a title is too long");
  }
  const cursor = a.get("cursor") ?? "";
  if (cursor.length > 2048) throw new BadParam("cursor", "cursor is not valid");
  return {
    limit, cursor,
    facets: {
      q: pyStrip(text(a, "q", 200)).toLowerCase(),
      loc: pyStrip(text(a, "loc", 200)).toLowerCase(),
      days: pyIntParam("days", text(a, "days", 12)),
      remote: pyStrip(text(a, "remote", 20)).toLowerCase(),
      visa: [...new Set(visa)].sort(),        // OR within the facet: the order does not matter
      level,
      pay: pyIntParam("pay", text(a, "pay", 12)),
      sort: sort === "pay" ? "pay" : "recent",
      // apply_facets: str(sponsored).strip().lower() in ("1", "true", "yes", "on")
      sponsored: ["1", "true", "yes", "on"].includes(pyStrip(text(a, "sponsored", 10)).toLowerCase()),
      titles: titles.map((t) => t.toLowerCase()),
    },
  };
}

/** The facets as one canonical string: the cache key and the cursor's fingerprint. */
export function facetKey(f: Facets): string {
  return JSON.stringify([f.q, f.loc, f.days, f.remote, f.visa, f.level, f.pay, f.sort, f.sponsored, f.titles]);
}

// ---- SQL ---------------------------------------------------------------------------------- //

/** A user string as ONE literal FTS5 phrase. Inside double quotes every character is plain text
 * (NEAR, AND, OR, NOT, *, ^, column filters and parentheses lose their meaning); a double quote
 * is written twice. With the trigram tokenizer a phrase matches rows CONTAINING the string. */
export const ftsPhrase = (s: string): string => `"${s.replace(/"/g, '""')}"`;

/** A substring as a GLOB pattern piece: *, ? and [ are the only special characters. */
const globLiteral = (s: string): string => s.replace(/[*?[]/g, (c) => `[${c}]`);

const chars = (s: string) => [...s].length;   // code points, as the trigram tokenizer counts

export interface Query { where: string[]; binds: unknown[]; match: string | null }

export function buildQuery(f: Facets, nowMs: number, useFts = true): Query {
  const where: string[] = [];
  const binds: unknown[] = [];
  const fts: string[] = [];
  // Trigram phrases need 3+ characters; shorter strings fall back to a scan with instr(). The
  // instr() checks stay on even when the index narrows, so the answer never depends on the index.
  if (f.q) {
    if (chars(f.q) >= 3) fts.push(`{title_l company_l} : ${ftsPhrase(f.q)}`);
    where.push("(instr(j.title_l, ?) > 0 OR instr(j.company_l, ?) > 0)");
    binds.push(f.q, f.q);
  }
  if (f.loc) {
    if (f.loc === "remote") {
      where.push("(instr(j.location_l, ?) > 0 OR j.remote_l = 'remote')");
    } else {
      if (chars(f.loc) >= 3) fts.push(`location_l : ${ftsPhrase(f.loc)}`);
      where.push("instr(j.location_l, ?) > 0");
    }
    binds.push(f.loc);
  }
  if (f.titles.length) {
    if (f.titles.every((t) => chars(t) >= 3)) {
      fts.push("(" + f.titles.map((t) => `title_l : ${ftsPhrase(t)}`).join(" OR ") + ")");
    }
    // picker._matches_focus: the title appears with no letter or digit right before or after it.
    // The title is padded with spaces so the edges count as boundaries, as in the regex.
    where.push("(" + f.titles.map(() => "(' ' || j.title_l || ' ') GLOB ?").join(" OR ") + ")");
    for (const t of f.titles) binds.push(`*[^a-z0-9]${globLiteral(t)}[^a-z0-9]*`);
  }
  if (f.remote) { where.push("j.remote_l = ?"); binds.push(f.remote); }
  if (f.level === "entry") where.push("j.entry = 1");
  if (f.visa.length) {
    const parts: string[] = [];
    for (const v of f.visa) {
      if (v === "SPONSORED") parts.push("j.visa_any = 1");
      else { parts.push("instr(j.visa_codes, ?) > 0"); binds.push(`,${v},`); }
    }
    where.push("(" + parts.join(" OR ") + ")");
  }
  if (f.sponsored) where.push("j.sponsored = 1");
  if (f.days > 0) {
    // dt < now - days drops the row; undated rows (NULL) stay. dt is whole seconds.
    where.push("(j.posted_ts IS NULL OR j.posted_ts * 1000 >= ?)");
    binds.push(nowMs - f.days * 86_400_000);
  }
  if (f.pay > 0) { where.push("j.pay >= ?"); binds.push(f.pay); }
  const match = fts.length ? fts.join(" AND ") : null;
  if (match && useFts) {
    where.unshift("j.id IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)");
    binds.unshift(match);
  }
  return { where, binds, match };
}

// ---- cursors ------------------------------------------------------------------------------ //

interface CursorBody {
  v: string;            // index_version it was issued against
  f: string;            // fingerprint of the facets
  n: number;            // results already served (the depth so far)
  c: number;            // the count reported on page 1
  k: [number, string];  // keyset: (pay, rkey) of the last row served; pay is 0 for sort=recent
  p?: number;           // 1 when page 1 chose the FTS plan, so later pages need no probe
}

const enc = new TextEncoder();
const b64url = (b: Uint8Array) =>
  btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64url = (s: string) =>
  Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (ch) => ch.charCodeAt(0));

async function hmacKey(env: Env): Promise<CryptoKey> {
  // Without SEARCH_CURSOR_SECRET a cursor is checked against a public key: it still cannot be
  // garbled by accident, but a determined caller could forge one past the depth limit. Set the
  // secret (`wrangler secret put SEARCH_CURSOR_SECRET`) before the API takes public traffic.
  const secret = env.SEARCH_CURSOR_SECRET || "sponsorjobs-search-cursor-v1";
  return crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false,
                                 ["sign", "verify"]);
}

export async function encodeCursor(env: Env, body: CursorBody): Promise<string> {
  const payload = enc.encode(JSON.stringify(body));
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", await hmacKey(env), payload));
  return `${b64url(payload)}.${b64url(mac.slice(0, 16))}`;
}

export async function decodeCursor(env: Env, raw: string): Promise<CursorBody> {
  const bad = new BadParam("cursor", "cursor is not valid");
  const [p, m, extra] = raw.split(".");
  if (!p || !m || extra !== undefined) throw bad;
  let payload: Uint8Array, mac: Uint8Array;
  try { payload = unb64url(p); mac = unb64url(m); } catch { throw bad; }
  const want = new Uint8Array(await crypto.subtle.sign("HMAC", await hmacKey(env), payload));
  if (mac.length !== 16 || !mac.every((b, i) => b === want[i])) throw bad;
  let body: CursorBody;
  try { body = JSON.parse(new TextDecoder().decode(payload)); } catch { throw bad; }
  if (typeof body?.v !== "string" || typeof body.f !== "string" || !Number.isInteger(body.n)
      || !Number.isInteger(body.c) || !Array.isArray(body.k) || typeof body.k[1] !== "string") throw bad;
  return body;
}

async function fingerprint(f: Facets): Promise<string> {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(facetKey(f))));
  return b64url(d.slice(0, 9));
}

// ---- index version ------------------------------------------------------------------------ //

interface IndexInfo { v: string | null; g: string; rows: number }
let versionCache: (IndexInfo & { at: number }) | null = null;

/** The loaded index's version, feed time and size, re-read at most every 30 s per isolate. */
async function indexVersion(env: Env, nowMs: number): Promise<IndexInfo> {
  if (versionCache && nowMs - versionCache.at < VERSION_TTL_MS) return versionCache;
  const rows = await env.SEARCH_DB.prepare("SELECT k, v FROM search_meta WHERE k IN ('index_version', 'generated_at', 'rows')")
    .all<{ k: string; v: string }>();
  const m = Object.fromEntries(rows.results.map((r) => [r.k, r.v]));
  versionCache = { at: nowMs, v: m.index_version ?? null, g: m.generated_at ?? "",
                   rows: Number.parseInt(m.rows ?? "0", 10) || 0 };
  return versionCache;
}

/** How many rows the ordered walk may read before the FTS index takes over. A common term finds
 * its page and the capped count well inside it (`engineer`: ~700 rows); a rare one runs out and
 * the index answers, reading about twice its matches. Measured: docs/server-search-plan.md. */
export const WALK_BUDGET = 3000;

/** Tests load a new index and need the next request to see it at once. */
export function forgetIndexVersion(): void { versionCache = null; }

// ---- the search itself -------------------------------------------------------------------- //

export interface Page {
  rows: string[];           // the slim rows as JSON strings
  count: number;
  count_capped: boolean;
  next: CursorBody | null;
  stats?: { rows_read: number; plan: string };
}

type Plan = "fts" | "scan" | "walk";
interface Key { id: number; pay: number; rkey: string }

/** One page of results for parsed facets. `after` is a decoded, already-checked cursor.
 *
 * Plans, all answering the same rows (search-parity.test.ts runs each):
 *   walk  the ordered index, reading at most `budget` rows; when that is not enough to fill the
 *         page and the count, the FTS plan (a keyword or location) or the full walk takes over
 *   fts   the trigram index finds the matches, which are then sorted
 *   scan  the ordered index with no budget (sponsored: its partial index is tiny)
 * Keys and the count come from ONE query (LIMIT count cap + 1 on page 1), then the page's rows
 * are read by id, so no plan reads its matches twice. */
export async function searchPage(env: Env, f: Facets, limit: number, after: CursorBody | null,
                                 version: string, nowMs: number,
                                 opts: { rows?: number; plan?: Plan; budget?: number } = {}): Promise<Page> {
  const db = env.SEARCH_DB;
  const served = after?.n ?? 0;
  const take = Math.min(limit, DEPTH_MAX - served);
  const want = after ? take + 1 : Math.max(take + 1, COUNT_CAP + 1);
  const desc = f.sort === "pay" ? "pay DESC, rkey DESC" : "rkey DESC";
  const keyset = !after ? null
    : f.sort === "pay" ? { sql: "(pay, rkey) < (?, ?)", binds: [after.k[0], after.k[1]] }
    : { sql: "rkey < ?", binds: [after.k[1]] };
  const hasText = buildQuery(f, nowMs).match !== null;
  let read = 0;

  const run = async (sql: string, binds: unknown[]) => {
    const r = await db.prepare(sql).bind(...binds).all<Key>();
    read += r.meta?.rows_read ?? 0;
    return r.results;
  };
  const keysFull = (useFts: boolean) => {
    const q = buildQuery(f, nowMs, useFts);
    const where = [...q.where, ...(keyset ? [keyset.sql.replace(/\b(pay|rkey)\b/g, "j.$1")] : [])];
    return run(`SELECT j.id, j.pay, j.rkey FROM jobs j ${where.length ? `WHERE ${where.join(" AND ")}` : ""}
                ORDER BY ${desc.replace(/(pay|rkey)/g, "j.$1")} LIMIT ?`,
               [...q.binds, ...(keyset?.binds ?? []), want]);
  };
  const keysWalk = (budget: number) => {
    const q = buildQuery(f, nowMs, false);
    const inner = `SELECT * FROM jobs ${keyset ? `WHERE ${keyset.sql}` : ""} ORDER BY ${desc} LIMIT ?`;
    return run(`SELECT j.id, j.pay, j.rkey FROM (${inner}) j ${q.where.length ? `WHERE ${q.where.join(" AND ")}` : ""}
                ORDER BY ${desc.replace(/(pay|rkey)/g, "j.$1")} LIMIT ?`,
               [...(keyset?.binds ?? []), budget, ...q.binds, want]);
  };

  let plan: Plan = opts.plan ?? (after?.p === 1 ? "fts" : f.sponsored ? "scan" : "walk");
  if (plan === "fts" && !hasText) plan = "scan";
  let keys: Key[];
  if (plan === "walk") {
    keys = await keysWalk(opts.budget ?? WALK_BUDGET);
    // Fewer than wanted: the budget ran out (or the list ended). The exact plan answers instead.
    if (keys.length < want) {
      plan = hasText ? "fts" : "scan";
      keys = await keysFull(plan === "fts");
    }
  } else {
    keys = await keysFull(plan === "fts");
  }

  const page = keys.slice(0, take);
  let rows: string[] = [];
  if (page.length) {
    const r = await db.prepare(`SELECT j.id, j.row, x.v AS extra FROM jobs j
                                LEFT JOIN job_extras x ON x.h = j.xh AND j.xh != ''
                                WHERE j.id IN (SELECT value FROM json_each(?))`)
      .bind(JSON.stringify(page.map((k) => k.id))).all<{ id: number; row: string; extra: string | null }>();
    read += r.meta?.rows_read ?? 0;
    const byId = new Map(r.results.map((x) => [x.id, x]));
    rows = page.map((k) => {
      const x = byId.get(k.id)!;
      return x.extra ? JSON.stringify({ ...JSON.parse(x.row), ...JSON.parse(x.extra) }) : x.row;
    });
  }
  const count = after ? after.c : keys.length;
  const last = page[page.length - 1];
  const more = keys.length > take && served + page.length < DEPTH_MAX;
  return {
    rows,
    count: Math.min(count, COUNT_CAP),
    count_capped: count > COUNT_CAP,
    next: more && last ? { v: version, f: await fingerprint(f), n: served + page.length, c: count,
                           k: [f.sort === "pay" ? last.pay : 0, last.rkey],
                           ...(plan === "fts" ? { p: 1 } : {}) } : null,
    stats: { rows_read: read, plan },
  };
}

// ---- the route ---------------------------------------------------------------------------- //

const reply = (data: unknown, status: number, extra: Record<string, string> = {}) =>
  new Response(typeof data === "string" ? data : JSON.stringify(data),
               { status, headers: { "content-type": "application/json", ...extra } });

/** Rate limits from the Workers rate-limit binding. A deployment (or test) without the binding
 * simply has no limit here; the Cloudflare WAF rule is the backstop either way. */
async function limited(req: Request, env: Env): Promise<boolean> {
  const token = req.headers.get("X-Install-Token") ?? "";
  if (env.SEARCH_TOKEN_LIMITER && /^[A-Za-z0-9_-]{8,128}$/.test(token)) {
    if (!(await env.SEARCH_TOKEN_LIMITER.limit({ key: `t:${token}` })).success) return true;
  }
  const ip = req.headers.get("CF-Connecting-IP") || "unknown";
  if (env.SEARCH_IP_LIMITER && !(await env.SEARCH_IP_LIMITER.limit({ key: `ip:${ip}` })).success) return true;
  return false;
}

export async function handleSearch(req: Request, env: Env, mode: Mode, nowMs: number,
                                   ctx?: { waitUntil(p: Promise<unknown>): void }): Promise<Response> {
  if (await limited(req, env)) {
    return reply({ error: "too many searches; wait a minute and try again", reason: "rate_limited" }, 429,
                 { "retry-after": "60" });
  }
  let p: Parsed;
  try {
    p = parseParams(new URL(req.url).searchParams, mode);
  } catch (e) {
    if (e instanceof BadParam) return reply({ error: e.message, param: e.param }, 400);
    throw e;
  }
  const { v: version, g: generatedAt, rows: indexRows } = await indexVersion(env, nowMs);
  if (!version) return reply({ error: "the search index is not loaded yet", reason: "no_index" }, 503);

  let after: CursorBody | null = null;
  if (p.cursor) {
    try {
      after = await decodeCursor(env, p.cursor);
    } catch (e) {
      if (e instanceof BadParam) return reply({ error: e.message, param: e.param }, 400);
      throw e;
    }
    if (after.v !== version) {
      return reply({ error: "the job list was updated; start again from the first page",
                     reason: "stale_cursor", index_version: version }, 409);
    }
    if (after.f !== (await fingerprint(p.facets))) {
      return reply({ error: "this cursor belongs to a different search", param: "cursor" }, 400);
    }
    if (after.n >= DEPTH_MAX) {
      return reply({ error: "search results stop at 1,000; refine the search to see more",
                     param: "cursor" }, 400);
    }
  }

  // The edge cache: normalized facets + page + index_version. Never the install token.
  const cache = (globalThis as { caches?: { default?: Cache } }).caches?.default;
  const key = new Request(`https://search-cache.sponsorjobs.internal/v1/jobs/${mode}?` + new URLSearchParams({
    f: facetKey(p.facets), limit: String(p.limit), cursor: p.cursor, iv: version }).toString());
  if (cache) {
    const hit = await cache.match(key);
    if (hit) return hit;
  }
  const page = await searchPage(env, p.facets, p.limit, after, version, nowMs, { rows: indexRows });
  const body = `{"jobs":[${page.rows.join(",")}],"count":${page.count},"count_capped":${page.count_capped},`
    + `"next_cursor":${page.next ? JSON.stringify(await encodeCursor(env, page.next)) : "null"},`
    + `"index_version":${JSON.stringify(version)},"generated_at":${JSON.stringify(generatedAt)}}`;
  const res = reply(body, 200, { "cache-control": `public, max-age=${CACHE_SECONDS}` });
  if (cache) {
    const put = cache.put(key, res.clone());
    if (ctx) ctx.waitUntil(put); else await put;
  }
  return res;
}
