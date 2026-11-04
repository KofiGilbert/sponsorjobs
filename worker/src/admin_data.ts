// What the admin dashboard's pages read, beyond the overview numbers in admin.ts collectStats.
//
// Everything here is a read: D1 queries that select only the columns a page shows (never a token,
// a token hash, a code, a salt, a forwarded mail's code or link), plus three outside sources that
// are fetched at most every few minutes per isolate and never block a page when they fail:
//   Stripe   the account's charges (amounts, refunds, disputes), with the Stripe key the broker has
//   feed     https://feed.sponsorjobs.ai/feed/manifest.json (public): when the job feed was built
//   GitHub   the public repo's Actions runs (feed, grow-boards, tests), optionally with GITHUB_TOKEN

import type { Env } from "./env";
import { intVar } from "./env";
import { DAY_SECONDS, FREE_MODEL, INTERVIEW_SECONDS, PACK_PRICE_LABEL, PASS_MODEL, PASSES, PLANS } from "./metering";
import { FREE_POOL_CAP_KEY, FREE_POOL_USER, poolCapOverride } from "./store";

// Anthropic list prices, $ per 1M tokens (checked 2026-10-10), blended at 80% input.
export const RATES = {
  free: { model: FREE_MODEL, inPerM: 1, outPerM: 5 },
  pass: { model: PASS_MODEL, inPerM: 2, outPerM: 10 },
} as const;
export const INPUT_SHARE = 0.8;
export const TAVUS_PER_MINUTE = 0.35;
export const blendedPerToken = (r: { inPerM: number; outPerM: number }) =>
  (INPUT_SHARE * r.inPerM + (1 - INPUT_SHARE) * r.outPerM) / 1_000_000;
export const round2 = (n: number) => Math.round(n * 100) / 100;

/** Plan in force for a users row at `now` ("free" once a pass has lapsed). */
export const planNow = (plan: string | null | undefined, passUntil: number | null | undefined, now: number) =>
  plan && PASSES.includes(plan) && (passUntil ?? 0) > now ? plan : "free";

// ---- settings ----------------------------------------------------------------------------------- //

export interface PoolSetting { cap: number; override: number | null; deployCap: number; updated_at: number | null; updated_by: string | null }

export async function poolSetting(env: Env): Promise<PoolSetting> {
  const row = await env.DB.prepare("SELECT value, updated_at, updated_by FROM admin_settings WHERE key = ?")
    .bind(FREE_POOL_CAP_KEY).first<{ value: string; updated_at: number; updated_by: string }>();
  const deployCap = intVar(env.FREE_POOL_DAILY_TOKENS, 5_000_000);
  const override = poolCapOverride(row?.value);
  return { cap: override ?? deployCap, override, deployCap, updated_at: row?.updated_at ?? null, updated_by: row?.updated_by ?? null };
}

// ---- users -------------------------------------------------------------------------------------- //

export type UserFilter = "all" | "free" | "pass" | "email" | "suspended";
export type UserSort = "newest" | "tokens" | "oldest";
export const USERS_PER_PAGE = 50;

export interface UserRow {
  account_id: string;
  email: string | null;
  created_at: number | null;
  plan: string;                // in force now
  pass_until: number;
  tokens_month: number;
  packages_month: number;
  credit_seconds: number;
  suspended_at: number | null;
}

/** One page of accounts. `q` matches an e-mail (substring, case-insensitive) or an account id
 * (prefix). The free-pool bookkeeping row is never an account. */
export async function listUsers(db: D1Database, o: { q: string; filter: UserFilter; sort: UserSort; page: number;
                                 period: string; now: number }): Promise<{ rows: UserRow[]; total: number }> {
  const where: string[] = ["a.account_id != ?"];
  const args: unknown[] = [FREE_POOL_USER];
  const q = o.q.trim().toLowerCase();
  if (q) { where.push("(lower(COALESCE(a.email, '')) LIKE ? OR a.account_id LIKE ?)"); args.push(`%${q}%`, `${q}%`); }
  const passList = PASSES.map(() => "?").join(", ");
  if (o.filter === "pass") { where.push(`u.plan IN (${passList}) AND u.pass_until > ?`); args.push(...PASSES, o.now); }
  if (o.filter === "free") { where.push(`NOT (COALESCE(u.plan, 'free') IN (${passList}) AND COALESCE(u.pass_until, 0) > ?)`); args.push(...PASSES, o.now); }
  if (o.filter === "email") where.push("a.email IS NOT NULL AND a.email != ''");
  if (o.filter === "suspended") where.push("a.suspended_at IS NOT NULL");
  const order = o.sort === "tokens" ? "tokens_month DESC, a.rowid DESC"
    : o.sort === "oldest" ? "COALESCE(a.created_at, 0) ASC, a.rowid ASC" : "COALESCE(a.created_at, 0) DESC, a.rowid DESC";
  const from = "FROM accounts a LEFT JOIN users u ON u.user = a.account_id " +
               "LEFT JOIN usage g ON g.user = a.account_id AND g.period = ? WHERE " + where.join(" AND ");
  const [rows, count] = await db.batch([
    db.prepare(`SELECT a.account_id, a.email, a.created_at, a.suspended_at, u.plan, COALESCE(u.pass_until, 0) AS pass_until, ` +
               `COALESCE(u.credit_seconds, 0) AS credit_seconds, COALESCE(g.llm_tokens, 0) AS tokens_month, ` +
               `COALESCE(g.packages, 0) + COALESCE(g.pass_packages, 0) AS packages_month ${from} ORDER BY ${order} LIMIT ? OFFSET ?`)
      .bind(o.period, ...args, USERS_PER_PAGE, Math.max(0, o.page - 1) * USERS_PER_PAGE),
    db.prepare(`SELECT COUNT(*) AS n ${from}`).bind(o.period, ...args),
  ]);
  const total = Number((count!.results[0] as { n: number } | undefined)?.n ?? 0);
  return {
    total,
    rows: (rows!.results as Array<Omit<UserRow, "plan"> & { plan: string | null }>).map((r) => ({
      ...r, plan: planNow(r.plan, r.pass_until, o.now) })),
  };
}

export interface UserDetail {
  account_id: string;
  email: string | null;
  created_at: number | null;
  email_at: number | null;
  suspended_at: number | null;
  plan: string;                         // in force now
  stored_plan: string | null;           // as stored (a lapsed pass keeps its name)
  pass_until: number;
  pass_seconds: number;                 // live-interview seconds left on the pass
  pass_packages: number;                // tailored packages left on the pass
  credit_seconds: number;               // bought interview seconds (packs), never expire
  sessions: number;                     // signed-in devices (tokens), a count only
  months: Array<{ period: string; tokens: number; packages: number; pass_packages: number }>;
  interviews: Array<{ started: number; used: number; ended: number }>;
  telegram: { linked: boolean; paused: boolean; linked_at: string | null };
  alias: { address: string; created_at: number; mails_30d: number } | null;
}

export async function userDetail(db: D1Database, id: string, now: number): Promise<UserDetail | null> {
  const [acct, user, toks, months, iv, tg, alias] = await db.batch([
    db.prepare("SELECT account_id, email, created_at, email_at, suspended_at FROM accounts WHERE account_id = ?").bind(id),
    db.prepare("SELECT plan, pass_until, pass_seconds, pass_packages, credit_seconds FROM users WHERE user = ?").bind(id),
    db.prepare("SELECT COUNT(*) AS n FROM tokens WHERE account_id = ?").bind(id),
    db.prepare("SELECT period, llm_tokens AS tokens, packages, pass_packages FROM usage WHERE user = ? " +
               "AND length(period) = 7 ORDER BY period DESC LIMIT 6").bind(id),
    db.prepare("SELECT started, used, ended FROM avatar_sessions WHERE user = ? ORDER BY started DESC LIMIT 10").bind(id),
    db.prepare("SELECT paused, linked_at FROM telegram_chats WHERE account = ?").bind(id),
    db.prepare("SELECT m.local, m.created_at, (SELECT COUNT(*) FROM mail_events e WHERE e.user = m.user AND e.at >= ?) AS n " +
               "FROM mail_aliases m WHERE m.user = ?").bind(now - 30 * DAY_SECONDS, id),
  ]);
  const a = acct!.results[0] as { account_id: string; email: string | null; created_at: number | null; email_at: number | null;
                                  suspended_at: number | null } | undefined;
  if (!a) return null;
  const u = user!.results[0] as { plan: string; pass_until: number; pass_seconds: number; pass_packages: number;
                                  credit_seconds: number } | undefined;
  const t = tg!.results[0] as { paused: number; linked_at: string } | undefined;
  const al = alias!.results[0] as { local: string; created_at: number; n: number } | undefined;
  return {
    ...a,
    plan: planNow(u?.plan, u?.pass_until, now),
    stored_plan: u?.plan ?? null,
    pass_until: u?.pass_until ?? 0,
    pass_seconds: u?.pass_seconds ?? 0,
    pass_packages: u?.pass_packages ?? 0,
    credit_seconds: u?.credit_seconds ?? 0,
    sessions: Number((toks!.results[0] as { n: number } | undefined)?.n ?? 0),
    months: months!.results as UserDetail["months"],
    interviews: iv!.results as UserDetail["interviews"],
    telegram: { linked: Boolean(t), paused: Boolean(t?.paused), linked_at: t?.linked_at ?? null },
    alias: al ? { address: `${al.local}@mail.sponsorjobs.ai`, created_at: al.created_at, mails_30d: Number(al.n) } : null,
  };
}

// ---- spend ------------------------------------------------------------------------------------- //

export interface Spender { account_id: string; email: string | null; plan: string; tokens: number; cost: number; cap_percent: number | null }

/** This month's AI spend per account, highest first, with the 95th percentile and the mean, so a
 * runaway account shows up even when the average looks calm. */
export async function spend(db: D1Database, period: string, now: number, limit = 10):
    Promise<{ top: Spender[]; accounts: number; mean: number; p95: number; total: number }> {
  const rows = (await db.prepare(
    "SELECT g.user AS account_id, g.llm_tokens AS tokens, a.email, u.plan, u.pass_until FROM usage g " +
    "LEFT JOIN accounts a ON a.account_id = g.user LEFT JOIN users u ON u.user = g.user " +
    "WHERE g.period = ? AND g.user != ? AND g.llm_tokens > 0 ORDER BY g.llm_tokens DESC")
    .bind(period, FREE_POOL_USER).all<{ account_id: string; tokens: number; email: string | null; plan: string | null;
                                        pass_until: number | null }>()).results;
  const priced = rows.map((r): Spender => {
    const plan = planNow(r.plan, r.pass_until, now);
    const free = plan === "free";
    const cap = PLANS[plan]?.llmTokenCap ?? null;
    return { account_id: r.account_id, email: r.email, plan, tokens: r.tokens,
             cost: r.tokens * blendedPerToken(free ? RATES.free : RATES.pass),
             cap_percent: cap ? round2((100 * r.tokens) / cap) : null };
  });
  const costs = priced.map((p) => p.cost).sort((x, y) => x - y);
  const total = costs.reduce((x, y) => x + y, 0);
  const p95 = costs.length ? costs[Math.min(costs.length - 1, Math.ceil(0.95 * costs.length) - 1)]! : 0;
  priced.sort((x, y) => y.cost - x.cost);
  return { top: priced.slice(0, limit), accounts: priced.length, mean: costs.length ? total / costs.length : 0, p95, total };
}

/** The free pool's tokens for each of the last `days` UTC days, oldest first. */
export async function poolDays(db: D1Database, todayStart: number, days = 14): Promise<Array<{ label: string; value: number }>> {
  const keys = Array.from({ length: days }, (_, i) => new Date((todayStart - (days - 1 - i) * DAY_SECONDS) * 1000).toISOString().slice(0, 10));
  const rows = (await db.prepare(`SELECT period, llm_tokens FROM usage WHERE user = ? AND period IN (${keys.map(() => "?").join(", ")})`)
    .bind(FREE_POOL_USER, ...keys).all<{ period: string; llm_tokens: number }>()).results;
  const by = new Map(rows.map((r) => [r.period, r.llm_tokens]));
  return keys.map((k) => ({ label: k, value: by.get(k) ?? 0 }));
}

/** Monthly totals for the last `n` months (newest last): tokens split free/pass, packages, interview minutes. */
export async function monthly(db: D1Database, nowMs: number, n = 6): Promise<Array<{ period: string; tokens: number; packages: number; minutes: number }>> {
  const d = new Date(nowMs);
  const periods = Array.from({ length: n }, (_, i) => {
    const m = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - (n - 1 - i), 1));
    return m.toISOString().slice(0, 7);
  });
  const start = Math.floor(Date.parse(`${periods[0]}-01T00:00:00Z`) / 1000);
  const [use, iv] = await db.batch([
    db.prepare(`SELECT period, COALESCE(SUM(llm_tokens), 0) AS tokens, COALESCE(SUM(packages), 0) + COALESCE(SUM(pass_packages), 0) AS packages ` +
               `FROM usage WHERE user != ? AND period IN (${periods.map(() => "?").join(", ")}) GROUP BY period`).bind(FREE_POOL_USER, ...periods),
    db.prepare("SELECT strftime('%Y-%m', started, 'unixepoch') AS period, COALESCE(SUM(used), 0) AS used FROM avatar_sessions " +
               "WHERE started >= ? GROUP BY period").bind(start),
  ]);
  const u = new Map((use!.results as Array<{ period: string; tokens: number; packages: number }>).map((r) => [r.period, r]));
  const v = new Map((iv!.results as Array<{ period: string; used: number }>).map((r) => [r.period, r.used]));
  return periods.map((p) => ({ period: p, tokens: u.get(p)?.tokens ?? 0, packages: u.get(p)?.packages ?? 0, minutes: (v.get(p) ?? 0) / 60 }));
}

/** New accounts per UTC day for `days` days ending the day before `endStart` (exclusive), oldest
 * first: the overview's comparison line (the 30 days before the chart's 30). */
export async function accountsPerDay(db: D1Database, endStart: number, days: number): Promise<number[]> {
  const start = endStart - days * DAY_SECONDS;
  const rows = (await db.prepare("SELECT date(created_at, 'unixepoch') AS day, COUNT(*) AS n FROM accounts " +
    "WHERE created_at >= ? AND created_at < ? GROUP BY day").bind(start, endStart).all<{ day: string; n: number }>()).results;
  const by = new Map(rows.map((r) => [r.day, r.n]));
  return Array.from({ length: days }, (_, i) => by.get(new Date((start + i * DAY_SECONDS) * 1000).toISOString().slice(0, 10)) ?? 0);
}

// ---- the overview's ranges and monthly series ---------------------------------------------------- //

export type RangeKey = "today" | "7d" | "30d" | "year";
export const RANGES: Array<{ key: RangeKey; label: string }> = [
  { key: "today", label: "Today" }, { key: "7d", label: "Last 7 days" }, { key: "30d", label: "Last 30 days" }, { key: "year", label: "This year" },
];
/** [start, end) of a range and of the same-length range just before it (unix seconds). */
export function rangeBounds(key: RangeKey, nowMs: number): { start: number; end: number; prevStart: number } {
  const now = Math.floor(nowMs / 1000);
  const d = new Date(nowMs);
  const today = Math.floor(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) / 1000);
  const start = key === "today" ? today : key === "7d" ? now - 7 * DAY_SECONDS : key === "30d" ? now - 30 * DAY_SECONDS
    : Math.floor(Date.UTC(d.getUTCFullYear(), 0, 1) / 1000);
  return { start, end: now, prevStart: start - (now - start) };
}

/** New accounts, e-mail sign-ins and paid checkouts in [start, end) and in [prevStart, start). */
export async function rangeCounts(db: D1Database, r: { start: number; end: number; prevStart: number }) {
  const q = (sql: string) => db.prepare(sql);
  const res = await db.batch([
    q("SELECT COUNT(*) AS n FROM accounts WHERE created_at >= ? AND created_at < ?").bind(r.start, r.end),
    q("SELECT COUNT(*) AS n FROM accounts WHERE created_at >= ? AND created_at < ?").bind(r.prevStart, r.start),
    q("SELECT COUNT(*) AS n FROM accounts WHERE email_at >= ? AND email_at < ?").bind(r.start, r.end),
    q("SELECT COUNT(*) AS n FROM accounts WHERE email_at >= ? AND email_at < ?").bind(r.prevStart, r.start),
    q("SELECT COUNT(*) AS n FROM processed_events WHERE key LIKE 'checkout:%' AND at >= ? AND at < ?").bind(r.start, r.end),
    q("SELECT COUNT(*) AS n FROM processed_events WHERE key LIKE 'checkout:%' AND at >= ? AND at < ?").bind(r.prevStart, r.start),
  ]);
  const n = (i: number) => Number((res[i]!.results[0] as { n: number } | undefined)?.n ?? 0);
  return { signups: [n(0), n(1)] as const, signins: [n(2), n(3)] as const, checkouts: [n(4), n(5)] as const };
}

/** The last `n` calendar months (UTC), oldest first, as "YYYY-MM", with the kit's short labels. */
export function lastMonths(nowMs: number, n = 7, yearsBack = 0): Array<{ period: string; label: string; end: number }> {
  const d = new Date(nowMs);
  return Array.from({ length: n }, (_, i) => {
    const m = new Date(Date.UTC(d.getUTCFullYear() - yearsBack, d.getUTCMonth() - (n - 1 - i), 1));
    const next = Date.UTC(m.getUTCFullYear(), m.getUTCMonth() + 1, 1) / 1000;
    return { period: m.toISOString().slice(0, 7), label: m.toLocaleString("en-US", { month: "short", timeZone: "UTC" }), end: next };
  });
}

/** Accounts that existed at the end of each month (the kit's "Total Users"), for the last 7
 * months and the same months a year earlier. */
export async function totalUsersSeries(db: D1Database, nowMs: number): Promise<{ labels: string[]; series: number[]; compare: number[] }> {
  const a = lastMonths(nowMs), b = lastMonths(nowMs, 7, 1);
  const stmts = [...a, ...b].map((m) => db.prepare("SELECT COUNT(*) AS n FROM accounts WHERE account_id != ? AND COALESCE(created_at, 0) < ?").bind(FREE_POOL_USER, m.end));
  const res = await db.batch(stmts);
  const n = res.map((r) => Number((r.results[0] as { n: number } | undefined)?.n ?? 0));
  // Accounts from before dates were recorded have created_at NULL: they count as always there,
  // which is the truth for every month this dashboard can show.
  return { labels: a.map((m) => m.label), series: n.slice(0, 7), compare: n.slice(7) };
}

/** Estimated AI cost per month (the kit's second tab), this year and a year earlier. */
export async function aiCostSeries(db: D1Database, nowMs: number): Promise<{ labels: string[]; series: number[]; compare: number[] }> {
  const a = lastMonths(nowMs), b = lastMonths(nowMs, 7, 1);
  const periods = [...a, ...b].map((m) => m.period);
  const [use, pool] = await db.batch([
    db.prepare(`SELECT period, COALESCE(SUM(llm_tokens), 0) AS t FROM usage WHERE user != ? AND period IN (${periods.map(() => "?").join(", ")}) GROUP BY period`).bind(FREE_POOL_USER, ...periods),
    db.prepare("SELECT substr(period, 1, 7) AS period, COALESCE(SUM(llm_tokens), 0) AS t FROM usage WHERE user = ? AND length(period) = 10 GROUP BY substr(period, 1, 7)").bind(FREE_POOL_USER),
  ]);
  const all = new Map((use!.results as Array<{ period: string; t: number }>).map((r) => [r.period, r.t]));
  const free = new Map((pool!.results as Array<{ period: string; t: number }>).map((r) => [r.period, r.t]));
  const cost = (p: string) => { const t = all.get(p) ?? 0, f = Math.min(t, free.get(p) ?? 0);
    return round2(f * blendedPerToken(RATES.free) + (t - f) * blendedPerToken(RATES.pass)); };
  return { labels: a.map((m) => m.label), series: a.map((m) => cost(m.period)), compare: b.map((m) => cost(m.period)) };
}

/** Net revenue per month from Stripe (the kit's third tab), this year and a year earlier. */
export function revenueSeries(r: Revenue, nowMs: number): { labels: string[]; series: number[]; compare: number[] } {
  const a = lastMonths(nowMs), b = lastMonths(nowMs, 7, 1);
  const by = new Map<string, number>();
  for (const p of r.payments) { const k = new Date(p.created * 1000).toISOString().slice(0, 7); by.set(k, (by.get(k) ?? 0) + (p.amount - p.refunded) / 100); }
  return { labels: a.map((m) => m.label), series: a.map((m) => round2(by.get(m.period) ?? 0)), compare: b.map((m) => round2(by.get(m.period) ?? 0)) };
}

/** Accounts whose pass has ended (the donut's fourth slice). */
export async function expiredPasses(db: D1Database, now: number): Promise<number> {
  const list = PASSES.map(() => "?").join(", ");
  return Number((await db.prepare(`SELECT COUNT(*) AS n FROM users WHERE user != ? AND plan IN (${list}) AND pass_until > 0 AND pass_until <= ?`)
    .bind(FREE_POOL_USER, ...PASSES, now).first<{ n: number }>())?.n ?? 0);
}

/** The company AI key's stored state, for the Alerts page. */
export async function aiStatusRow(db: D1Database): Promise<{ state: string; since: number; last_seen: number; last_alert_at: number | null; note: string } | null> {
  return db.prepare("SELECT state, since, last_seen, last_alert_at, note FROM ai_status WHERE key = 'ai'")
    .first<{ state: string; since: number; last_seen: number; last_alert_at: number | null; note: string }>();
}

// ---- sign-up pressure (abuse signals, counts only) ---------------------------------------------- //

/** Today's new-account counts per network (the registration limiter's salted IP hash, never an
 * address), busiest first, and the limit they run into. */
export async function signupNetworks(db: D1Database, today: string, env: Env): Promise<{ rows: Array<{ net: string; n: number }>; limit: number }> {
  const rows = (await db.prepare("SELECT ip_hash, n FROM register_limits WHERE day = ? ORDER BY n DESC LIMIT 5")
    .bind(today).all<{ ip_hash: string; n: number }>()).results;
  return { rows: rows.map((r) => ({ net: r.ip_hash.slice(0, 8), n: r.n })), limit: intVar(env.REGISTER_PER_IP_PER_DAY, 50) };
}

/** E-mail domains of signed-in accounts, most common first (the domain only). */
export async function emailDomains(db: D1Database, limit = 6): Promise<Array<{ domain: string; n: number }>> {
  return (await db.prepare("SELECT lower(substr(email, instr(email, '@') + 1)) AS domain, COUNT(*) AS n FROM accounts " +
    "WHERE email LIKE '%@%' GROUP BY domain ORDER BY n DESC LIMIT ?").bind(limit).all<{ domain: string; n: number }>()).results;
}

// ---- audit log ---------------------------------------------------------------------------------- //

export interface AuditRow { id: number; at: number; admin: string; action: string; target: string; detail: string; reason: string }

export async function auditLog(db: D1Database, o: { target?: string; limit?: number; before?: number } = {}): Promise<AuditRow[]> {
  const where: string[] = [];
  const args: unknown[] = [];
  if (o.target) { where.push("target = ?"); args.push(o.target); }
  if (o.before) { where.push("id < ?"); args.push(o.before); }
  return (await db.prepare(`SELECT id, at, admin, action, target, detail, reason FROM admin_actions ` +
    (where.length ? `WHERE ${where.join(" AND ")} ` : "") + "ORDER BY id DESC LIMIT ?")
    .bind(...args, o.limit ?? 50).all<AuditRow>()).results;
}

/** The audit log's four numbers for the last `days` days. */
export async function auditCounts(db: D1Database, since: number): Promise<{ total: number; passes: number; suspends: number; last: number | null }> {
  const r = await db.prepare("SELECT COUNT(*) AS total, SUM(action = 'give_pass') AS passes, SUM(action = 'suspend') AS suspends, " +
    "(SELECT MAX(at) FROM admin_actions) AS last FROM admin_actions WHERE at >= ?").bind(since)
    .first<{ total: number; passes: number | null; suspends: number | null; last: number | null }>();
  return { total: Number(r?.total ?? 0), passes: Number(r?.passes ?? 0), suspends: Number(r?.suspends ?? 0), last: r?.last ?? null };
}

/** Sign-in e-mails sent per UTC day for the last `days` days, oldest first (email_sends). */
export async function emailDays(db: D1Database, todayStart: number, days = 14): Promise<Array<{ label: string; value: number }>> {
  const keys = Array.from({ length: days }, (_, i) => new Date((todayStart - (days - 1 - i) * DAY_SECONDS) * 1000).toISOString().slice(0, 10));
  const rows = (await db.prepare(`SELECT day, n FROM email_sends WHERE day IN (${keys.map(() => "?").join(", ")})`).bind(...keys)
    .all<{ day: string; n: number }>()).results;
  const by = new Map(rows.map((r) => [r.day, r.n]));
  return keys.map((k) => ({ label: k, value: by.get(k) ?? 0 }));
}

// ---- recent activity (the overview's right rail) ------------------------------------------------- //

export interface Activity { at: number; kind: "signup" | "signin" | "purchase" | "admin"; text: string; href?: string }
export const ACTIVITY_ICON: Record<Activity["kind"], string> = { signup: "users", signin: "users", purchase: "revenue", admin: "audit" };

export async function recentActivity(db: D1Database, limit = 8): Promise<Activity[]> {
  const [signups, signins, buys, admin] = await db.batch([
    db.prepare("SELECT account_id, created_at AS at FROM accounts WHERE created_at IS NOT NULL ORDER BY created_at DESC LIMIT ?").bind(limit),
    db.prepare("SELECT account_id, email, email_at AS at FROM accounts WHERE email_at IS NOT NULL ORDER BY email_at DESC LIMIT ?").bind(limit),
    db.prepare("SELECT at FROM processed_events WHERE key LIKE 'checkout:%' ORDER BY at DESC LIMIT ?").bind(limit),
    db.prepare("SELECT at, action, target FROM admin_actions ORDER BY id DESC LIMIT ?").bind(limit),
  ]);
  const out: Activity[] = [
    ...(signups!.results as Array<{ account_id: string; at: number }>).map((r): Activity =>
      ({ at: r.at, kind: "signup", text: "New account", href: `/users/${encodeURIComponent(r.account_id)}` })),
    ...(signins!.results as Array<{ account_id: string; email: string; at: number }>).map((r): Activity =>
      ({ at: r.at, kind: "signin", text: "Added an e-mail sign-in", href: `/users/${encodeURIComponent(r.account_id)}` })),
    ...(buys!.results as Array<{ at: number }>).map((r): Activity => ({ at: r.at, kind: "purchase", text: "Stripe checkout paid", href: "/revenue" })),
    ...(admin!.results as Array<{ at: number; action: string; target: string }>).map((r): Activity =>
      ({ at: r.at, kind: "admin", text: ACTION_LABEL[r.action] ?? r.action, href: r.target ? `/users/${encodeURIComponent(r.target)}` : "/audit" })),
  ];
  return out.sort((a, b) => b.at - a.at).slice(0, limit);
}

export const ACTION_LABEL: Record<string, string> = {
  give_pass: "Give pass", add_interviews: "Add interviews", suspend: "Suspend", unsuspend: "Restore", test_alert: "Test alert",
  set_free_pool: "Free AI limit", reveal_email: "Reveal e-mail",
};

// ---- outside sources (cached, never fatal) ------------------------------------------------------ //

interface Cached<T> { at: number; value: T }
const cache = new Map<string, Cached<unknown>>();
async function cached<T>(key: string, ttlMs: number, nowMs: number, load: () => Promise<T>): Promise<T> {
  const hit = cache.get(key) as Cached<T> | undefined;
  if (hit && nowMs - hit.at < ttlMs) return hit.value;
  const value = await load();
  cache.set(key, { at: nowMs, value });
  return value;
}
/** For tests: forget what was fetched. */
export function resetAdminCache() { cache.clear(); }

async function getJson(url: string, headers: Record<string, string> = {}, timeoutMs = 6000): Promise<unknown> {
  const res = await fetch(url, { headers: { accept: "application/json", "user-agent": "sponsorjobs-admin", ...headers },
                                 signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
const why = (e: unknown) => (e instanceof Error ? e.message : String(e)).slice(0, 160);

// Stripe -------------------------------------------------------------------------------------------

export interface Payment {
  id: string; created: number; amount: number; refunded: number; currency: string;
  disputed: boolean; email: string | null; product: string; status: string;
}
export interface Revenue {
  ok: boolean; why?: string; mode: "live" | "test" | "off";
  payments: Payment[];                          // newest first, last 400 days, up to 100
  dashboard: string;                            // the Stripe dashboard's payments page for this mode
}

/** Which product a charge was, from its amount (the checkout's metadata stays on the session, not
 * the charge; the amounts are our own price labels). */
export function productFor(amountCents: number): string {
  const label = `$${(amountCents / 100).toFixed(amountCents % 100 ? 2 : 0)}`;
  for (const p of PASSES) if (PLANS[p]!.priceLabel === label) return p;
  for (const [id, l] of Object.entries(PACK_PRICE_LABEL)) if (l === label) return id;
  return "other";
}

export async function revenue(env: Env, nowMs: number): Promise<Revenue> {
  const key = env.STRIPE_SECRET_KEY || env.STRIPE_TEST_SECRET_KEY || "";
  const mode = env.STRIPE_SECRET_KEY ? "live" : env.STRIPE_TEST_SECRET_KEY ? "test" : "off";
  const dashboard = `https://dashboard.stripe.com/${mode === "test" ? "test/" : ""}payments`;
  if (!key) return { ok: false, why: "no Stripe key is set", mode, payments: [], dashboard };
  return cached(`stripe:${mode}`, 5 * 60_000, nowMs, async () => {
    try {
      const since = Math.floor(nowMs / 1000) - 400 * DAY_SECONDS;   // this year and last, for the monthly chart
      const data = await getJson(`https://api.stripe.com/v1/charges?limit=100&created[gte]=${since}`,
        { Authorization: `Bearer ${key}` }) as { data?: Array<Record<string, any>> };
      const payments = (data.data ?? []).filter((c) => c.paid || c.status === "succeeded").map((c): Payment => ({
        id: String(c.id), created: Number(c.created), amount: Number(c.amount ?? 0), refunded: Number(c.amount_refunded ?? 0),
        currency: String(c.currency ?? "usd"), disputed: Boolean(c.disputed), status: String(c.status ?? ""),
        email: c.billing_details?.email ?? c.receipt_email ?? null, product: productFor(Number(c.amount ?? 0)),
      }));
      return { ok: true, mode, payments, dashboard };
    } catch (e) {
      return { ok: false, why: why(e), mode, payments: [], dashboard };
    }
  });
}

/** Net revenue (after refunds) in dollars over the last `days` days. */
export const netSince = (r: Revenue, nowSec: number, days: number) =>
  r.payments.filter((p) => p.created >= nowSec - days * DAY_SECONDS).reduce((a, p) => a + (p.amount - p.refunded), 0) / 100;

// The job feed ------------------------------------------------------------------------------------

export interface Feed { ok: boolean; why?: string; generated_at: number | null; jobs: number; employers: number | null; sponsors_at: number | null }
export const FEED_MANIFEST = "https://feed.sponsorjobs.ai/feed/manifest.json";
export const SPONSORS_MANIFEST = "https://feed.sponsorjobs.ai/feed/sponsors/manifest.json";
/** The feed workflow runs every 3 hours; older than this is "stale". */
export const FEED_STALE_HOURS = 8;

export async function feed(nowMs: number): Promise<Feed> {
  return cached("feed", 5 * 60_000, nowMs, async () => {
    try {
      const m = await getJson(FEED_MANIFEST) as { generated_at?: string; count?: number };
      let employers: number | null = null, sponsors_at: number | null = null;
      try {
        const s = await getJson(SPONSORS_MANIFEST) as { employers?: number; generated_at?: string };
        employers = typeof s.employers === "number" ? s.employers : null;
        sponsors_at = s.generated_at ? Math.floor(Date.parse(s.generated_at) / 1000) : null;
      } catch { /* the sponsor index is optional */ }
      const at = m.generated_at ? Math.floor(Date.parse(m.generated_at) / 1000) : null;
      return { ok: true, generated_at: Number.isFinite(at) ? at : null, jobs: Number(m.count ?? 0), employers, sponsors_at };
    } catch (e) {
      return { ok: false, why: why(e), generated_at: null, jobs: 0, employers: null, sponsors_at: null };
    }
  });
}

// GitHub Actions ----------------------------------------------------------------------------------

export interface Run { workflow: string; conclusion: string; status: string; at: number; url: string; branch: string }
export interface Runs { ok: boolean; why?: string; latest: Run[] }
export const GITHUB_REPO = "KofiGilbert/sponsorjobs";
const WATCHED = ["feed", "grow-boards", "tests", "release"];

export async function githubRuns(env: Env & { GITHUB_TOKEN?: string }, nowMs: number): Promise<Runs> {
  return cached("github", 10 * 60_000, nowMs, async () => {
    try {
      const headers: Record<string, string> = { accept: "application/vnd.github+json" };
      if (env.GITHUB_TOKEN) headers.Authorization = `Bearer ${env.GITHUB_TOKEN}`;
      const data = await getJson(`https://api.github.com/repos/${GITHUB_REPO}/actions/runs?per_page=60`, headers) as
        { workflow_runs?: Array<Record<string, any>> };
      const latest: Run[] = [];
      for (const name of WATCHED) {
        const r = (data.workflow_runs ?? []).find((w) => String(w.name ?? "").toLowerCase() === name &&
          (name !== "tests" || w.head_branch === "main"));
        if (r) latest.push({ workflow: name, conclusion: String(r.conclusion ?? ""), status: String(r.status ?? ""),
          at: Math.floor(Date.parse(String(r.updated_at ?? r.created_at)) / 1000), url: String(r.html_url ?? ""),
          branch: String(r.head_branch ?? "") });
      }
      return { ok: true, latest };
    } catch (e) {
      return { ok: false, why: why(e), latest: [] };
    }
  });
}

export { INTERVIEW_SECONDS };
