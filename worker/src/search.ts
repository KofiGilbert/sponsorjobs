// Server-side job search over the D1 index (docs/server-search-plan.md):
//   GET /v1/jobs/search      the Jobs page's board, at most 50 rows a page
//   GET /v1/jobs/candidates  the Auto-apply picker's pool, at most 500 rows a page, plus titles=A|B
//
// The facets mean EXACTLY what sourcing/filters.apply_facets (and, for titles,
// autoapply/picker._matches_focus) mean on the device, and the order is the board's: newest first
// (feedfile.recency_key), or highest pay first with that order breaking ties. The loader
// (scripts/load_search_index.py) precomputes every derived value in Python, so this file only
// compares numbers. worker/test/search-parity.test.ts checks the two agree on ~60 facet mixes.
//
// Built for Cloudflare's FREE plan (5 million D1 rows read a day, for the whole account):
//   * jobs.id IS the board's order, so a page is a short walk down the table, and the keyword
//     index hands back matches newest first with no sort;
//   * a request reads a bounded number of rows (SCAN_CAP). When a rare filter mix needs more, it
//     answers the rows found so far with a cursor that carries on from where the scan stopped
//     (the app follows it), so no single request can read the whole table;
//   * no counting past the page: the count is exact when the scan reached the end, otherwise an
//     estimate ("about N") from what the scan saw;
//   * the rows read are added up per UTC day (search_usage) and search stops itself (503
//     daily_budget) before it can use the allowance the broker's own database needs.
//
// Locked to SponsorJobs installs (src/installauth.ts): every request needs a valid broker account
// token (`Authorization: Bearer`), else 401 no_account. The account id is used only as a rate-limit
// key; it is never stored next to a query or logged.
// Privacy: the request carries only the query, the facets and a cursor; nothing here logs them.
// Abuse: 300 requests a minute per network (checked before the token is looked up), then 60 per
// account (the Workers rate-limit bindings, when bound), at most 1,000 results deep per query.

import type { Env } from "./env";
import { accountFor, clientIp, overLimit } from "./installauth";

export const SEARCH_MAX = 50;
export const CANDIDATES_MAX = 500;
export const DEPTH_MAX = 1000;          // refine to see more; the API is not a bulk export
export const SCAN_CAP = 2000;           // index rows one search request may walk
export const CANDIDATES_SCAN_CAP = 6000;
export const FTS_COUNT_CAP = 250;       // a keyword's matches are counted up to here ("250+")
export const FTS_SORT_MAX = 600;        // sort=pay: a keyword with fewer matches is sorted whole
const CACHE_SECONDS = 600;              // an index_version lives hours; the cache at most 10 min
const META_TTL_MS = 30_000;             // how long an isolate trusts the index info it read
const FLUSH_ROWS = 25_000;              // add this isolate's reads to search_usage this often
const FLUSH_REQUESTS = 500;
export const DAILY_ROWS_READ = 3_500_000;   // of the free plan's 5,000,000; 0 = no limit
export const DAILY_REQUESTS = 60_000;       // of the free plan's 100,000; 0 = no limit

// jobs.f bits and the id layout: the same numbers as scripts/load_search_index.py.
const F_SPONSOR = 1, F_ENTRY = 2, F_OFFERED = 4, F_VISA_ANY = 8, F_REMOTE_SHIFT = 4;
const REMOTE_KINDS: Record<string, number> = { remote: 1, hybrid: 2, onsite: 3 };
const LABEL_SPAN = 2 ** 24;
const DATED = 2 ** 40;
const DAY_MAX = 65535;
const ID_END = Number.MAX_SAFE_INTEGER;
const STRIDE = 64;                     // the loader's usual gap between neighbouring labels

type Mode = "search" | "candidates";

export interface Facets {
  q: string; loc: string; days: number; remote: string; visa: string[]; level: string;
  pay: number; sort: "recent" | "pay"; sponsored: boolean; scope: "sponsors" | "all"; titles: string[];
}

interface Parsed { facets: Facets; limit: number; cursor: string }

export class BadParam extends Error {
  readonly param: string;
  constructor(param: string, message: string) { super(message); this.param = param; }
}

// ---- Python's str.strip() and int() -------------------------------------------------------- //

// Python's whitespace (str.isspace), which differs from JS \s: \x1c-\x1f and \x85 are space in
// Python, the byte-order mark is not.
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
  // sourcing/filters.apply_facets: only "sponsors" narrows; anything else is every job.
  const scope = pyStrip(text(a, "scope", 20)).toLowerCase();
  if (scope !== "" && scope !== "sponsors" && scope !== "all") {
    throw new BadParam("scope", "scope must be sponsors or all");
  }
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
      scope: scope === "sponsors" ? "sponsors" : "all",
      titles: titles.map((t) => t.toLowerCase()),
    },
  };
}

/** The facets as one canonical string: the cache key and the cursor's fingerprint. */
export function facetKey(f: Facets): string {
  return JSON.stringify([f.q, f.loc, f.days, f.remote, f.visa, f.level, f.pay, f.sort, f.sponsored,
                         f.scope, f.titles]);
}

// ---- SQL ---------------------------------------------------------------------------------- //

const chars = (s: string) => [...s];   // code points, as the trigram tokenizer counts

/** A user string as an FTS5 query: every 3-character window of it as its own quoted token,
 * ANDed. Inside double quotes every character is plain text (NEAR, AND, OR, NOT, *, ^, column
 * filters and parentheses lose their meaning); a double quote is written twice. The index
 * (detail=none) then answers the rows holding all of those trigrams somewhere in their title,
 * company or location, and the caller checks the real substring on each. null under 3 characters. */
export function ftsAll(s: string): string | null {
  const c = chars(s);
  if (c.length < 3) return null;
  const grams = new Set<string>();
  for (let i = 0; i + 3 <= c.length; i++) grams.add(c.slice(i, i + 3).join(""));
  return [...grams].map((g) => `"${g.replace(/"/g, '""')}"`).join(" AND ");
}

/** A substring as a GLOB pattern piece: *, ? and [ are the only special characters. */
const globLiteral = (s: string): string => s.replace(/[*?[]/g, (c) => `[${c}]`);

const LOW = (col: string, low: string) => `coalesce(j.${low}, lower(j.${col}))`;

export interface Query { where: string[]; binds: unknown[]; match: string | null }

/** The row test for the facets (every condition is checked on the row itself, so the answer
 * never depends on which index found the row) and the keyword index's query, when the text is
 * long enough to use it. `visaBits` maps a visa code to its bit in jobs.vm. */
export function buildQuery(f: Facets, nowMs: number, visaBits: Map<string, number>): Query {
  const where: string[] = [];
  const binds: unknown[] = [];
  const fts: string[] = [];
  if (f.scope === "sponsors") where.push(`(j.f & ${F_SPONSOR}) != 0`);
  if (f.q) {
    const m = ftsAll(f.q);
    if (m) fts.push(`(${m})`);
    where.push(`(instr(${LOW("title", "tl")}, ?) > 0 OR instr(${LOW("company", "cl")}, ?) > 0)`);
    binds.push(f.q, f.q);
  }
  if (f.loc) {
    if (f.loc === "remote") {
      where.push(`(instr(${LOW("location", "ll")}, ?) > 0 OR ((j.f >> ${F_REMOTE_SHIFT}) & 3) = 1)`);
    } else {
      const m = ftsAll(f.loc);
      if (m) fts.push(`(${m})`);
      where.push(`instr(${LOW("location", "ll")}, ?) > 0`);
    }
    binds.push(f.loc);
  }
  if (f.titles.length) {
    const ms = f.titles.map(ftsAll);
    if (ms.every((m) => m)) fts.push("(" + ms.map((m) => `(${m})`).join(" OR ") + ")");
    // picker._matches_focus: the title appears with no letter or digit right before or after it.
    // The title is padded with spaces so the edges count as boundaries, as in the regex.
    where.push("(" + f.titles.map(() => `(' ' || ${LOW("title", "tl")} || ' ') GLOB ?`).join(" OR ") + ")");
    for (const t of f.titles) binds.push(`*[^a-z0-9]${globLiteral(t)}[^a-z0-9]*`);
  }
  if (f.remote) {
    const kind = REMOTE_KINDS[f.remote];
    where.push(kind ? `((j.f >> ${F_REMOTE_SHIFT}) & 3) = ${kind}` : "0");
  }
  if (f.level === "entry") where.push(`(j.f & ${F_ENTRY}) != 0`);
  if (f.visa.length) {
    const parts: string[] = [];
    let mask = 0;
    for (const v of f.visa) {
      if (v === "SPONSORED") parts.push(`(j.f & ${F_VISA_ANY}) != 0`);
      else if (visaBits.has(v)) mask += 2 ** visaBits.get(v)!;
    }
    if (mask) { parts.push("(j.vm & ?) != 0"); binds.push(mask); }
    where.push(parts.length ? "(" + parts.join(" OR ") + ")" : "0");
  }
  if (f.sponsored) where.push(`(j.f & ${F_OFFERED}) != 0`);
  if (f.days > 0) {
    // dt < now - days drops the row; rows with no readable date (NULL) stay. Whole seconds.
    where.push("(j.ts IS NULL OR j.ts * 1000 >= ?)");
    binds.push(nowMs - f.days * 86_400_000);
  }
  if (f.pay > 0) { where.push("j.pay >= ?"); binds.push(f.pay); }
  return { where, binds, match: fts.length ? fts.join(" AND ") : null };
}

// ---- cursors ------------------------------------------------------------------------------ //

/** Where a scan stands. `p`: W walking the table (or the keyword index) newest first, P walking
 * the pay index, Z the rows with no pay after it, S a small keyword result sorted whole.
 * `s`: which id range of a W / Z walk (the date facet has two). `b`: carry on below this id;
 * `k`: and, for P and S, at or below this pay. */
interface Pos { p: "W" | "P" | "Z" | "S"; s: number; b: number; k: number }

interface CursorBody extends Pos {
  v: string;            // index_version it was issued against
  f: string;            // fingerprint of the facets
  n: number;            // results already served (the depth so far)
  c: number;            // the count reported on page 1 ...
  e: number | null;     // ... its estimate ...
  x: 0 | 1;             // ... and whether it was exact
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
  const secret = env.SEARCH_CURSOR_SECRET || "sponsorjobs-search-cursor-v2";
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
      || !Number.isInteger(body.c) || !Number.isInteger(body.s) || !Number.isFinite(body.b)
      || !Number.isFinite(body.k) || !["W", "P", "Z", "S"].includes(body.p)) throw bad;
  return body;
}

async function fingerprint(f: Facets): Promise<string> {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(facetKey(f))));
  return b64url(d.slice(0, 9));
}

// ---- the index's own information, and the day's usage --------------------------------------- //

export interface IndexInfo {
  v: string | null;       // index_version; null until the first full load has finished
  g: string;              // generated_at of the feed build it holds
  rows: number; sponsorRows: number;
  fields: string[]; tags: string[]; visaBits: Map<string, number>;
  hist: Array<[number, number]>;   // [day key (id / 2^24), rows], highest key first
  payq: number[]; payqStep: number;
}
let metaCache: (IndexInfo & { at: number }) | null = null;

const usage = { day: "", read: 0, requests: 0,          // the day's totals as last read from D1
                 newRead: 0, newRequests: 0 };           // this isolate's share not yet written
let budgetNoted = "";                                    // the day this isolate last noted a 503 daily_budget

const utcDay = (ms: number) => new Date(ms).toISOString().slice(0, 10);

function list(raw: string | undefined): string[] {
  try {
    const v = JSON.parse(raw ?? "[]");
    return Array.isArray(v) ? v.map(String) : [];
  } catch { return []; }
}

/** The loader's histograms out of search_meta (empty when absent: the walks are then unbounded). */
export function histograms(m: Record<string, string | undefined>): Pick<IndexInfo, "hist" | "payq" | "payqStep"> {
  const nums = (raw: string | undefined): unknown[] => {
    try { const v = JSON.parse(raw ?? "[]"); return Array.isArray(v) ? v : []; } catch { return []; }
  };
  const hist = nums(m.hist).filter((e): e is [number, number] =>
    Array.isArray(e) && Number.isFinite(e[0]) && Number.isFinite(e[1])).sort((a, b) => b[0] - a[0]);
  const payq = nums(m.payq).filter((v): v is number => Number.isFinite(v));
  return { hist, payq, payqStep: Math.max(1, Number.parseInt(m.payq_step ?? "500", 10) || 500) };
}

/** The loaded index's version, sizes and lists, and today's usage, re-read at most every 30 s
 * per isolate. */
async function indexInfo(db: D1Database, nowMs: number): Promise<IndexInfo> {
  if (metaCache && nowMs - metaCache.at < META_TTL_MS) return metaCache;
  const day = utcDay(nowMs);
  const [meta, used] = await db.batch<{ k: string; v: string }>([
    db.prepare("SELECT k, v FROM search_meta"),
    db.prepare("SELECT 'read' AS k, CAST(rows_read AS TEXT) AS v FROM search_usage WHERE day = ?1 "
               + "UNION ALL SELECT 'requests', CAST(requests AS TEXT) FROM search_usage WHERE day = ?1").bind(day),
  ]);
  const m = Object.fromEntries(meta!.results.map((r) => [r.k, r.v]));
  const u = Object.fromEntries(used!.results.map((r) => [r.k, Number(r.v) || 0]));
  if (usage.day !== day) { usage.day = day; usage.newRead = 0; usage.newRequests = 0; }
  usage.read = u.read ?? 0;
  usage.requests = u.requests ?? 0;
  metaCache = {
    at: nowMs, v: m.ready === "1" ? (m.index_version ?? null) : null, g: m.generated_at ?? "",
    rows: Number.parseInt(m.rows ?? "0", 10) || 0,
    sponsorRows: Number.parseInt(m.sponsor_rows ?? "0", 10) || 0,
    fields: list(m.fields), tags: list(m.tags),
    visaBits: new Map(list(m.visa).map((c, i) => [c, i])),
    ...histograms(m),
  };
  return metaCache;
}

/** Tests load a new index and need the next request to see it at once. */
export function forgetIndexVersion(): void {
  metaCache = null;
  usage.day = ""; usage.read = 0; usage.requests = 0; usage.newRead = 0; usage.newRequests = 0;
  budgetNoted = "";
}

const limitOf = (raw: string | undefined, fallback: number) => {
  const n = Number.parseInt(raw ?? "", 10);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
};

/** Is today's search allowance used up? (rows read, or requests; 0 = no limit.) */
function overBudget(env: Env): boolean {
  const rows = limitOf(env.SEARCH_DAILY_ROWS_READ, DAILY_ROWS_READ);
  const reqs = limitOf(env.SEARCH_DAILY_REQUESTS, DAILY_REQUESTS);
  return (rows > 0 && usage.read + usage.newRead >= rows)
    || (reqs > 0 && usage.requests + usage.newRequests >= reqs);
}

/** Count one request and the rows it read; write this isolate's share to search_usage once it
 * is worth a row written (every 25,000 rows read or 500 requests). */
function noteUsage(db: D1Database, nowMs: number, rowsRead: number,
                   ctx?: { waitUntil(p: Promise<unknown>): void }): void {
  const day = utcDay(nowMs);
  if (usage.day !== day) { usage.day = day; usage.read = 0; usage.requests = 0; usage.newRead = 0; usage.newRequests = 0; }
  usage.newRead += rowsRead;
  usage.newRequests += 1;
  if (usage.newRead < FLUSH_ROWS && usage.newRequests < FLUSH_REQUESTS) return;
  const read = usage.newRead, requests = usage.newRequests;
  usage.newRead = 0; usage.newRequests = 0;
  usage.read += read; usage.requests += requests;
  const p = db.prepare(
    "INSERT INTO search_usage (day, rows_read, rows_written, requests) VALUES (?, ?, 0, ?) "
    + "ON CONFLICT(day) DO UPDATE SET rows_read = rows_read + excluded.rows_read, "
    + "requests = requests + excluded.requests RETURNING rows_read, requests")
    .bind(day, read, requests).first<{ rows_read: number; requests: number }>()
    .then((r) => { if (r && usage.day === day) { usage.read = r.rows_read; usage.requests = r.requests; } })
    .catch(() => { /* a lost count only makes the budget a little generous */ });
  if (ctx) ctx.waitUntil(p);
}

/** Note, once per isolate per UTC day, that search turned requests away (503 daily_budget), so
 * the admin dashboard can say the free plan is too small (src/admin_cf.ts). One row written per
 * isolate on such a day, never one per request. */
async function noteBudgetHit(db: D1Database, nowMs: number, ctx?: { waitUntil(p: Promise<unknown>): void }): Promise<void> {
  const day = utcDay(nowMs);
  if (budgetNoted === day) return;
  budgetNoted = day;
  const p = db.prepare("INSERT INTO search_budget_days (day, hits) VALUES (?, 1) "
                       + "ON CONFLICT(day) DO UPDATE SET hits = hits + 1").bind(day).run()
    .then(() => undefined, () => { /* not migrated yet, or D1 refused: the dashboard also reads the counters */ });
  if (ctx) ctx.waitUntil(p); else await p;
}

// ---- the search itself -------------------------------------------------------------------- //

export interface Page {
  rows: Record<string, unknown>[];   // the slim rows, as the feed carries them
  count: number;                     // exact when count_exact, else the rows known so far
  count_exact: boolean;
  count_estimate: number | null;     // "about N" when not exact and the scan could tell
  next: CursorBody | null;
  stats: { rows_read: number; plan: string; queries: number };
}

interface Hit {
  id: number; pay: number; title: string; company: string; location: string; r: string; xv: string | null;
  at?: Pos;             // where a scan resumes right after this row (set as rows are found)
}

const COLS = "j.id AS id, j.pay AS pay, j.title AS title, j.company AS company, "
  + "j.location AS location, j.r AS r, x.v AS xv";
const EXTRAS = "LEFT JOIN extras x ON x.x = j.x AND j.x != 0";

/** The id ranges a newest-first walk covers, newest range first: [low, high] inclusive. The date
 * facet keeps a dated row only from the cut-off's day on, so the walk reads those days and then
 * jumps to the rows with no readable date (undated rows sit below every dated one). */
function ranges(f: Facets, nowMs: number): Array<[number, number]> {
  if (!(f.days > 0)) return [[0, ID_END]];
  const sec = Math.floor((nowMs - f.days * 86_400_000) / 1000);
  const day = Math.max(0, Math.min(DAY_MAX, Math.floor(sec / 86_400)));
  return [[DATED + day * LABEL_SPAN, ID_END], [0, DATED + LABEL_SPAN - 1]];
}

function toRow(h: Hit, info: IndexInfo): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  let rest: unknown[] = [];
  try { rest = JSON.parse(h.r); } catch { /* an unreadable row keeps its text columns */ }
  for (let i = 0; i < rest.length && i < info.fields.length; i++) {
    let v = rest[i];
    if (v === null || v === undefined) continue;
    if (info.fields[i] === "tags" && Array.isArray(v)) {
      v = v.map((t) => (typeof t === "number" ? info.tags[t] ?? String(t) : t));
    }
    out[info.fields[i]!] = v;
  }
  out.title = h.title; out.company = h.company; out.location = h.location;
  if (h.xv) { try { Object.assign(out, JSON.parse(h.xv)); } catch { /* no employer fields */ } }
  return out;
}

/** The lowest id a walk from `below` down needs to cover about `span` rows of the table, from
 * the loader's per-day row counts (search_meta 'hist'). Labels inside a day are about STRIDE
 * apart, so a position inside a day is read off its label. 0 (no bound) without a histogram. */
export function idBound(info: IndexInfo, below: number, span: number): number {
  if (!info.hist.length) return 0;
  let need = Math.max(1, span);
  const top = Math.floor((below - 1) / LABEL_SPAN);
  for (const [k, n] of info.hist) {
    if (k > top) continue;
    const base = k * LABEL_SPAN;
    const avail = k === top ? Math.min(n, Math.floor((below - base) / STRIDE)) : n;
    if (avail >= need) return base + STRIDE * (avail - need);
    need -= avail;
  }
  return 0;
}

/** The lowest pay a walk of the pay index from `pay` down needs to cover about `span` rows, from
 * the loader's pay quantiles (search_meta 'payq': every payq_step-th pay, highest first). */
export function payBound(info: IndexInfo, pay: number, span: number): number {
  const qs = info.payq;
  if (!qs.length) return 1;
  // Always strictly below `pay`, so every range makes progress; the rows AT `pay` (a tie, and
  // salaries tie a lot) are read whole in the same range.
  let i = 0;
  while (i < qs.length && qs[i]! >= pay) i++;
  const j = i + Math.max(1, Math.ceil(span / info.payqStep)) - 1;
  return j < qs.length ? qs[j]! : 1;
}

/** One page of results for parsed facets. `after` is a decoded, already-checked cursor.
 *
 * Plans, all answering the same rows in the same order (search-parity.test.ts runs each):
 *   W  walk the table newest first, or, with a keyword / place / titles of 3+ characters, the
 *      keyword index's matches newest first; each row is then tested against the facets
 *   P  sort=pay: walk the pay index highest first, then (Z) the rows with no pay, newest first
 *   S  sort=pay with a keyword that has few matches: read them all and sort them
 * W, P and Z stop after `cap` index rows; the page is then short and the cursor carries on. */
export async function searchPage(env: Env, f: Facets, limit: number, after: CursorBody | null,
                                 info: IndexInfo, nowMs: number,
                                 opts: { cap?: number; noFts?: boolean; noSort?: boolean;
                                         trace?: Array<{ sql: string; binds: unknown[]; read: number }> } = {}): Promise<Page> {
  const db = env.SEARCH_DB!;
  const version = info.v ?? "";
  const served = after?.n ?? 0;
  const take = Math.min(limit, DEPTH_MAX - served);
  const q = buildQuery(f, nowMs, info.visaBits);
  const match = opts.noFts ? null : q.match;
  const segs = ranges(f, nowMs);
  const cap = opts.cap ?? SCAN_CAP;
  let read = 0, queries = 0;

  const run = async <T>(sql: string, binds: unknown[]) => {
    const r = await db.prepare(sql).bind(...binds).all<T>();
    read += r.meta?.rows_read ?? 0;
    queries += 1;
    opts.trace?.push({ sql: sql.replace(/\s+/g, " "), binds, read: r.meta?.rows_read ?? 0 });
    return { rows: r.results, read: r.meta?.rows_read ?? 0 };
  };
  const and = (conds: string[]) => (conds.length ? ` WHERE ${conds.join(" AND ")}` : "");

  // ---- page 1: pick the plan, and count a keyword's matches ------------------------------- //
  let pos: Pos;
  let probe: number | null = null;
  if (after) {
    pos = { p: after.p, s: after.s, b: after.b, k: after.k };
  } else {
    if (match) {
      const lim = (f.sort === "pay" && !opts.noSort ? FTS_SORT_MAX : FTS_COUNT_CAP) + 1;
      probe = (await run<{ n: number }>(
        "SELECT count(*) AS n FROM (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ? LIMIT ?)",
        [match, lim])).rows[0]?.n ?? 0;
    }
    const p = f.sort !== "pay" ? "W" : probe !== null && probe <= FTS_SORT_MAX && !opts.noSort ? "S" : "P";
    pos = { p, s: 0, b: ID_END, k: Number.MAX_SAFE_INTEGER };
  }

  // ---- the scan -------------------------------------------------------------------------- //
  // Every query is a plain ordered walk (the table by id, the keyword index by rowid, or the pay
  // index) that SQLite stops as soon as the page is full, so a common search reads about a page
  // of rows. A rare filter mix could walk far, so each query also gets a lower bound from the
  // loader's histograms: about `budget` rows of the index, no more. When a range runs out before
  // the page is full, the next range starts below it, until the request has read SCAN_CAP rows;
  // then the page goes back short and the cursor carries on from that bound.
  const hits: Hit[] = [];
  let ended = false;        // nothing is left below `pos`
  let scanned = 0;          // table rows walked this request without a keyword (for the estimate)
  let matchedAll = 0;       // rows found this request, including one past the page
  const want = () => take + 1 - hits.length;
  let budget = cap;

  const collect = (rows: Hit[], at: (h: Hit) => void) => {
    for (const h of rows) { at(h); h.at = { ...pos }; hits.push(h); }
    matchedAll += rows.length;
  };

  if (pos.p === "S") {
    // Few keyword matches: all of them, tested and sorted by pay, the board's order within a pay.
    const where = [...q.where, "(j.pay, j.id) < (?, ?)"];
    const got = await run<Hit>(
      `SELECT ${COLS} FROM (SELECT rowid AS id FROM jobs_fts WHERE jobs_fts MATCH ?) c
       CROSS JOIN jobs j ON j.id = c.id ${EXTRAS}${and(where)}
       ORDER BY j.pay DESC, j.id DESC LIMIT ?`,
      [match, ...q.binds, pos.k, pos.b, want()]);
    collect(got.rows, (h) => { pos.k = h.pay; pos.b = h.id; });
    if (want() > 0) ended = true;
  }

  const floor = Math.max(1, f.pay);           // the pay index holds pay > 0; a minimum cuts it
  while (pos.p === "P" && want() > 0 && budget > 0 && !ended) {
    // The pay index, highest first, down to a pay about `budget` rows below where it stands.
    // Term order matters: with `pay >= ?` first SQLite bounds the index range by it (measured:
    // 48,965 rows read for a pay floor of 150,000, against 172,150 with `pay > 0` first). The
    // `pay > 0` term is what lets SQLite use the partial index at all.
    const lo = Math.min(pos.k, Math.max(floor, payBound(info, pos.k, budget)));
    if (pos.k <= lo && pos.b === 0) { pos = { p: "Z", s: 0, b: ID_END, k: 0 }; if (f.pay > 0) ended = true; continue; }
    const got = await run<Hit>(
      `SELECT ${COLS} FROM jobs j INDEXED BY jobs_pay ${EXTRAS}
       WHERE j.pay >= ? AND j.pay > 0 AND (j.pay, j.id) < (?, ?)${q.where.length ? " AND " + q.where.join(" AND ") : ""}
       ORDER BY j.pay DESC, j.id DESC LIMIT ?`,
      [lo, pos.k, pos.b, ...q.binds, want()]);
    collect(got.rows, (h) => { pos.k = h.pay; pos.b = h.id; });
    budget -= Math.max(1, got.read);
    scanned += Math.max(0, got.read - 2 * got.rows.length);
    if (want() > 0) {
      if (lo <= floor) {                        // the pay index is done
        if (f.pay > 0) ended = true;            // rows with no pay cannot clear a minimum
        else pos = { p: "Z", s: 0, b: ID_END, k: 0 };
      } else {
        pos = { ...pos, k: lo, b: 0 };          // every row at pay `lo` was read: below it next
      }
    }
  }

  let span = budget;                            // index rows the next W / Z range should cover
  while ((pos.p === "W" || pos.p === "Z") && want() > 0 && budget > 0 && !ended) {
    const seg = segs[pos.s];
    if (!seg) { ended = true; break; }
    const [low, high] = seg;
    const below = Math.min(pos.b, high + 1);
    const nextSeg = () => {
      pos = { ...pos, s: pos.s + 1, b: ID_END };
      if (!segs[pos.s]) ended = true;
    };
    if (below <= low) { nextSeg(); continue; }
    const fts = Boolean(match) && pos.p === "W";
    // "Ad offers sponsorship" walks its own small index (under 1% of rows) to the end.
    const offered = f.sponsored && pos.p === "W" && !fts;
    let bound = offered ? low : Math.max(low, idBound(info, below, span));
    if (bound >= below) bound = Math.max(low, below - 1);
    const conds = [...(pos.p === "Z" ? ["j.pay = 0"] : []), ...q.where];
    const tail = conds.length ? " AND " + conds.join(" AND ") : "";
    // D1 binds a JavaScript number as REAL, and FTS5 then ignores a rowid bound (measured: every
    // match read, 36,892 rows, instead of 306): the casts keep the bounds INTEGER.
    const got = fts
      ? await run<Hit>(
        `SELECT ${COLS} FROM jobs_fts c CROSS JOIN jobs j ON j.id = c.rowid ${EXTRAS}
         WHERE jobs_fts MATCH ? AND c.rowid < CAST(? AS INTEGER) AND c.rowid >= CAST(? AS INTEGER)${tail}
         ORDER BY c.rowid DESC LIMIT ?`,
        [match, below, bound, ...q.binds, want()])
      : await run<Hit>(
        `SELECT ${COLS} FROM jobs j ${offered ? "INDEXED BY jobs_offered " : ""}${EXTRAS}
         WHERE j.id < ? AND j.id >= ?${tail} ORDER BY j.id DESC LIMIT ?`,
        [below, bound, ...q.binds, want()]);
    collect(got.rows, (h) => { pos.b = h.id; });
    budget -= Math.max(1, got.read);
    if (!fts) scanned += Math.max(0, got.read - 2 * got.rows.length);
    if (want() > 0) {
      if (bound <= low) { nextSeg(); continue; }
      pos = { ...pos, b: bound };
      // A keyword reads only its matches: widen the next range by how sparse they were.
      // At most 4 times wider each step, so a stretch where matches crowd together cannot make
      // one query read far past the budget.
      span = fts ? Math.min(span * 4, Math.max(info.rows, 1),
                            Math.round(Math.max(1, budget) * span / Math.max(1, got.read)))
                 : budget;
    }
  }

  // ---- the page, the count, the cursor ---------------------------------------------------- //
  const page = hits.slice(0, take);
  const overflow = hits.length > take;
  const lastHit = page[page.length - 1];
  // Carry on right after the last row served. The row past the page may come from a later
  // stage of the walk (the rows with no pay after the pay index), so the resume point is the one
  // recorded with the last row served, not where the scan itself stopped.
  if (overflow && lastHit?.at) pos = { ...lastHit.at };
  const depthLeft = served + page.length < DEPTH_MAX;
  const more = (overflow || !ended) && depthLeft && take > 0;

  let count: number, exact: boolean, estimate: number | null;
  if (after) {
    count = after.c; exact = after.x === 1; estimate = after.e;
    if (!exact && !overflow && ended) { count = served + page.length; exact = true; estimate = null; }
    else if (!exact) count = Math.max(count, served + page.length + (overflow ? 1 : 0));
  } else if (!overflow && ended) {
    count = page.length; exact = true; estimate = null;
  } else {
    count = page.length + (overflow ? 1 : 0);
    exact = false;
    estimate = null;
    const onlyText = q.where.length === (f.q ? 1 : 0) + (f.loc ? 1 : 0) + (f.titles.length ? 1 : 0);
    const scopeOnly = q.where.length === 1 && f.scope === "sponsors";
    if (q.where.length === 0 || scopeOnly) {
      // No filter, or only the sponsor board: the loader counted those rows.
      const total = scopeOnly ? info.sponsorRows : info.rows;
      if (total > 0) estimate = Math.max(count, total);
    } else if (probe !== null && onlyText) {
      // The keyword index's own count: the rows holding the text's trigrams, a close upper bound.
      if (probe <= FTS_COUNT_CAP) estimate = Math.max(probe, count);
      else count = Math.max(count, FTS_COUNT_CAP);   // "250+": the probe found at least that many
    } else if (probe === null && !match && scanned >= 50 && !(f.days > 0)) {
      // The share of the rows walked that matched, applied to the whole list: "about N".
      const total = f.sponsored ? 0 : info.rows;
      if (total > 0) estimate = Math.max(count, Math.round(total * Math.min(1, matchedAll / scanned)));
    }
  }

  return {
    rows: page.map((h) => toRow(h, info)),
    count, count_exact: exact, count_estimate: estimate,
    next: more ? { v: version, f: await fingerprint(f), n: served + page.length,
                   c: count, e: estimate, x: exact ? 1 : 0, ...pos } : null,
    stats: { rows_read: read, plan: pos.p + (match && pos.p === "W" ? "+fts" : ""), queries },
  };
}

// ---- the route ---------------------------------------------------------------------------- //

const reply = (data: unknown, status: number, extra: Record<string, string> = {}) =>
  new Response(typeof data === "string" ? data : JSON.stringify(data),
               { status, headers: { "content-type": "application/json", ...extra } });

export async function handleSearch(req: Request, env: Env, mode: Mode, nowMs: number,
                                   ctx?: { waitUntil(p: Promise<unknown>): void }): Promise<Response> {
  const noIndex = () => reply({ error: "the search index is not loaded yet", reason: "no_index" }, 503);
  const tooMany = () => reply({ error: "too many searches; wait a minute and try again", reason: "rate_limited" },
                              429, { "retry-after": "60" });
  // The network first, so a flood of made-up tokens never reaches the account database; then the
  // account. A deployment (or test) without a limiter binding has no limit there; the daily budget
  // below is the backstop either way.
  if (await overLimit(env.SEARCH_IP_LIMITER, `ip:${clientIp(req)}`)) return tooMany();
  const account = await accountFor(req, env, nowMs);
  if (!account) return reply({ error: "search is for SponsorJobs accounts", reason: "no_account" }, 401);
  if (await overLimit(env.SEARCH_ACCOUNT_LIMITER, `a:${account}`)) return tooMany();
  if (!env.SEARCH_DB) return noIndex();          // the search database is not bound in this deployment
  let p: Parsed;
  try {
    p = parseParams(new URL(req.url).searchParams, mode);
  } catch (e) {
    if (e instanceof BadParam) return reply({ error: e.message, param: e.param }, 400);
    throw e;
  }
  const info = await indexInfo(env.SEARCH_DB, nowMs);
  if (!info.v) return noIndex();
  const version = info.v;
  if (overBudget(env)) {
    // Search has used its share of the day's free allowance: the app shows its own recent list
    // until tomorrow (UTC). Say when to come back, an hour at the most.
    const untilMidnight = 86_400 - Math.floor((nowMs / 1000) % 86_400);
    await noteBudgetHit(env.SEARCH_DB, nowMs, ctx);
    return reply({ error: "search has reached today's limit; try again later", reason: "daily_budget" }, 503,
                 { "retry-after": String(Math.min(3600, untilMidnight)) });
  }

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

  // The edge cache: normalized facets + page + index_version. Never the token or the account: the
  // answer is the same for every account, and it is looked up only after the token check above.
  const cache = (globalThis as { caches?: { default?: Cache } }).caches?.default;
  const key = new Request(`https://search-cache.sponsorjobs.internal/v1/jobs/${mode}?` + new URLSearchParams({
    f: facetKey(p.facets), limit: String(p.limit), cursor: p.cursor, iv: version }).toString());
  if (cache) {
    const hit = await cache.match(key);
    if (hit) { noteUsage(env.SEARCH_DB, nowMs, 0, ctx); return forClient(hit); }
  }
  const page = await searchPage(env, p.facets, p.limit, after, info, nowMs,
                                { cap: mode === "candidates" ? CANDIDATES_SCAN_CAP : SCAN_CAP });
  noteUsage(env.SEARCH_DB, nowMs, page.stats.rows_read, ctx);
  const res = reply({
    jobs: page.rows,
    count: page.count, count_capped: !page.count_exact, count_estimate: page.count_estimate,
    next_cursor: page.next ? await encodeCursor(env, page.next) : null,
    index_version: version, generated_at: info.g,
  }, 200, { "cache-control": `public, max-age=${CACHE_SECONDS}` });
  if (cache) {
    const put = cache.put(key, res.clone());
    if (ctx) ctx.waitUntil(put); else await put;
  }
  return forClient(res);
}

/** The copy the app gets: `private`, since it answered a request that carried a token (the edge
 * cache's own copy above stays `public`, which the Cache API needs to store it). */
function forClient(r: Response): Response {
  const out = new Response(r.body, r);
  out.headers.set("cache-control", `private, max-age=${CACHE_SECONDS}`);
  return out;
}
