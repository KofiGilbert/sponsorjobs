// Where Autofill and the apply agent stop (2026-11-26, the app's autoapply/stoplog.py): the app sends
// small batches of events to POST /v1/autofill/events, the admin's "Autofill stops" page groups them
// so a stop seen many times is fixed in the brain in general, not one site at a time.
//
// Where and why, never who. The app keeps only public words; this side keeps only the listed
// fields, cuts every text, and takes out anything that looks like an email address, a link or a
// long digit run, whatever arrives. Capped: 50 events a request, 300 rows an account a UTC day (one
// count read, so the free plan's 100,000 rows written a day stay safe), kept 90 days.

export const EVENT_TYPES = ["asked_person", "stuck_loop", "cost_cap", "captcha", "sign_in_wall", "required_left_empty",
  "site_error", "closed_posting", "other"] as const;
export const PAGE_KINDS = ["job_page", "form", "review", "sign_in", "other"] as const;
export const MAX_BATCH = 50;
export const DAILY_CAP = 300;
export const KEEP_DAYS = 90;
export const PRUNE_EVERY = 50;        // about one request in this many prunes rows past KEEP_DAYS

const EMAIL = /[\w.%+-]+@[\w-]+(\.[\w-]+)+/g;
const URL_RE = /https?:\/\/\S+|www\.\S+/gi;
const DIGITS = /\+?\(?\d[\d\s().-]{5,}\d/g;

/** A text as it may be stored: whitespace folded, no email, link or long digit run, cut to `max`. */
export function clean(v: unknown, max: number): string {
  return String(v ?? "").replace(/\s+/g, " ").trim().replace(EMAIL, "[x]").replace(URL_RE, "[link]").replace(DIGITS, "[x]")
    .slice(0, max).trim();
}

/** The question's grouping key: lowercased, digits and punctuation folded, at most 120 chars. */
export function qnorm(q: string): string {
  return String(q || "").toLowerCase().replace(/\[x\]|\d+/g, " ").replace(/[^\p{L}\s]+/gu, " ").replace(/\s+/g, " ").trim().slice(0, 120);
}

export interface StopEvent {
  type: string; ats: string; host: string; page_kind: string; question: string; qnorm: string; options: string[];
  reason: string; steps: number; cost: number; version: string;
}

/** One event as stored, or null when it is not one (an unknown type). Every other field is dropped. */
export function sanitize(raw: unknown): StopEvent | null {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const type = String(r.type ?? "");
  if (!(EVENT_TYPES as readonly string[]).includes(type)) return null;
  const pk = String(r.page_kind ?? "other");
  const question = clean(r.question, 200);
  const steps = Math.max(0, Math.min(999, Math.trunc(Number(r.steps) || 0)));
  const cost = Math.max(0, Math.min(99, Number(r.cost) || 0));
  return {
    type, ats: clean(r.ats, 32).toLowerCase(), host: clean(r.host, 120).toLowerCase(),
    page_kind: (PAGE_KINDS as readonly string[]).includes(pk) ? pk : "other",
    question, qnorm: qnorm(question),
    options: (Array.isArray(r.options) ? r.options : []).slice(0, 8).map((o) => clean(o, 60)).filter(Boolean),
    reason: clean(r.reason, 160), steps, cost: Math.round(cost * 10_000) / 10_000, version: clean(r.version, 24),
  };
}

/** Store a batch for one account. Returns how many were kept and how many the daily cap turned away. */
export async function storeEvents(db: D1Database, account: string, raw: unknown[], nowS: number, day: string,
                                  rand: () => number = Math.random): Promise<{ kept: number; capped: number; dropped: number }> {
  const events = raw.slice(0, MAX_BATCH).map(sanitize).filter((e): e is StopEvent => e !== null);
  const dropped = Math.min(raw.length, MAX_BATCH) - events.length;
  const row = await db.prepare("SELECT COUNT(*) AS n FROM autofill_events WHERE account = ? AND day = ?").bind(account, day).first<{ n: number }>();
  const room = Math.max(0, DAILY_CAP - (row?.n ?? 0));
  const take = events.slice(0, room);
  const stmts = take.map((e) => db.prepare(
    "INSERT INTO autofill_events(account, at, day, type, ats, host, page_kind, question, qnorm, options, reason, steps, cost, version) " +
    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    .bind(account, nowS, day, e.type, e.ats, e.host, e.page_kind, e.question, e.qnorm, JSON.stringify(e.options), e.reason, e.steps, e.cost, e.version));
  if (rand() < 1 / PRUNE_EVERY) stmts.push(db.prepare("DELETE FROM autofill_events WHERE at < ?").bind(nowS - KEEP_DAYS * 86_400));
  if (stmts.length) await db.batch(stmts);
  return { kept: take.length, capped: events.length - take.length, dropped };
}

// ---- the admin page's numbers ----------------------------------------------------------------- //

export interface StopGroup { type: string; qnorm: string; question: string; ats: string; host: string; n7: number; n30: number;
  last: number; reason: string; options: string }

/** Counts by type over 7 and 30 days. */
export async function stopTypes(db: D1Database, nowS: number): Promise<Array<{ type: string; n7: number; n30: number }>> {
  const r = await db.prepare(
    "SELECT type, SUM(CASE WHEN at >= ?1 THEN 1 ELSE 0 END) AS n7, COUNT(*) AS n30 FROM autofill_events WHERE at >= ?2 " +
    "GROUP BY type ORDER BY n30 DESC").bind(nowS - 7 * 86_400, nowS - 30 * 86_400).all<{ type: string; n7: number; n30: number }>();
  return r.results;
}

/** The stops grouped by type and question (else by host when there is no question), seen most
 * first, each with its newest example (question, choices, reason, host). */
export async function stopGroups(db: D1Database, nowS: number, o: { type?: string; limit?: number } = {}): Promise<StopGroup[]> {
  const lim = Math.max(1, Math.min(200, o.limit ?? 60));
  const where = o.type ? " AND type = ?3" : "";
  const st = db.prepare(
    "SELECT type, CASE WHEN qnorm != '' THEN qnorm ELSE 'host:' || host END AS gkey, MAX(id) AS last_id, MAX(at) AS last, " +
    "SUM(CASE WHEN at >= ?1 THEN 1 ELSE 0 END) AS n7, COUNT(*) AS n30 " +
    `FROM autofill_events WHERE at >= ?2${where} GROUP BY type, gkey ORDER BY n30 DESC, last DESC LIMIT ${lim}`);
  const g = await (o.type ? st.bind(nowS - 7 * 86_400, nowS - 30 * 86_400, o.type) : st.bind(nowS - 7 * 86_400, nowS - 30 * 86_400))
    .all<{ type: string; gkey: string; last_id: number; last: number; n7: number; n30: number }>();
  if (!g.results.length) return [];
  const ids = g.results.map((x) => x.last_id);
  const ex = await db.prepare(`SELECT id, question, qnorm, options, reason, ats, host FROM autofill_events WHERE id IN (${ids.map(() => "?").join(",")})`)
    .bind(...ids).all<{ id: number; question: string; qnorm: string; options: string; reason: string; ats: string; host: string }>();
  const by = new Map(ex.results.map((r) => [r.id, r]));
  return g.results.map((x) => {
    const r = by.get(x.last_id);
    return { type: x.type, qnorm: r?.qnorm ?? "", question: r?.question ?? "", ats: r?.ats ?? "", host: r?.host ?? "",
             n7: x.n7, n30: x.n30, last: x.last, reason: r?.reason ?? "", options: r?.options ?? "[]" };
  });
}

/** Counts by hiring system over 30 days. */
export async function stopHosts(db: D1Database, nowS: number): Promise<Array<{ ats: string; host: string; n30: number }>> {
  const r = await db.prepare("SELECT ats, host, COUNT(*) AS n30 FROM autofill_events WHERE at >= ? GROUP BY ats, host ORDER BY n30 DESC LIMIT 20")
    .bind(nowS - 30 * 86_400).all<{ ats: string; host: string; n30: number }>();
  return r.results;
}

/** Rows written today and the most on one day of the last 7 (the free plan card's own line). */
export async function stopWrites(db: D1Database, today: string, since: string): Promise<{ today: number; peak: number }> {
  const r = await db.prepare("SELECT day, COUNT(*) AS n FROM autofill_events WHERE day >= ? GROUP BY day").bind(since).all<{ day: string; n: number }>();
  const t = r.results.find((x) => x.day === today)?.n ?? 0;
  return { today: t, peak: Math.max(0, ...r.results.map((x) => x.n)) };
}
