// How close BlackOrigin's Cloudflare account is to the FREE plan's limits, and when it is time to
// pay for Workers Paid ($5 a month). Shown on the admin Health page ("Cloudflare Free Plan") and,
// once it is time, as a banner on top of every admin page.
//
// The numbers come from the search database (SEARCH_DB), never from a request counter here:
//   cf_usage            one row a UTC day, the whole account's totals for that day, written by the
//                       feed workflow's loader from Cloudflare's GraphQL Analytics API
//                       (scripts/cf_usage_snapshot.py; migrations-search/0002_cf_usage.sql)
//   search_usage        search's own counters for the day (src/search.ts)
//   search_budget_days  the days search turned requests away with 503 daily_budget
//
// Free plan limits (checked 2026-11-26):
//   Workers  100,000 requests a day        https://developers.cloudflare.com/workers/platform/limits/
//   D1       5 million rows read a day, 100,000 rows written a day, 5 GB stored in all, 500 MB a
//            database                       https://developers.cloudflare.com/d1/platform/limits/
//                                           https://developers.cloudflare.com/d1/platform/pricing/
//   Workers Paid is $5 a month             https://developers.cloudflare.com/workers/platform/pricing/
// Storage is counted in decimal units (500 MB = 500,000,000 bytes): the smaller reading, so the
// warning comes a little early rather than late. Daily limits reset at 00:00 UTC.

import type { Env } from "./env";
import { DAILY_REQUESTS, DAILY_ROWS_READ } from "./search";
import { badge, card, esc, facts, num, table } from "./admin_ui";

export const CF_PLANS_URL = "https://www.cloudflare.com/plans/developer-platform/";
export const CF_PAY_AT = 70;        // percent of a limit (7-day peak): "Time to switch to Workers Paid"
export const CF_CLOSE_AT = 50;      // percent: "Getting close"
export const CF_WINDOW_DAYS = 7;

export const CF_LIMITS = [
  { key: "worker_requests", label: "Workers requests", limit: 100_000, unit: "a day", bytes: false },
  { key: "d1_rows_read", label: "D1 rows read", limit: 5_000_000, unit: "a day", bytes: false },
  { key: "d1_rows_written", label: "D1 rows written", limit: 100_000, unit: "a day", bytes: false },
  { key: "d1_storage_bytes", label: "D1 storage, all databases", limit: 5_000_000_000, unit: "in all", bytes: true },
  { key: "d1_largest_bytes", label: "Largest D1 database", limit: 500_000_000, unit: "a database", bytes: true },
] as const;
export type CfKey = (typeof CF_LIMITS)[number]["key"];

/** One cf_usage row as the loader wrote it (NULL = could not be read that day). */
export interface CfRow {
  day: string; taken_at: string;
  worker_requests: number | null; d1_rows_read: number | null; d1_rows_written: number | null;
  d1_storage_bytes: number | null; d1_largest_bytes: number | null; d1_largest_name: string | null;
  load_rows: number | null; load_total: number | null; load_per_day: number | null; load_ready: number | null;
  load_written: number | null; load_budget: number | null;   // the loader's own writes that day, and its budget
  warning: string | null;
}

export interface CfUsage {
  ok: boolean;                 // the search database answered
  why?: string;                // why not, in plain words
  today: string;               // the UTC day the page was built
  rows: CfRow[];               // newest first, at most CF_WINDOW_DAYS + 1
  budgetDays: string[];        // days in the last 7 (today included) search said daily_budget
}

const dayMinus = (day: string, n: number) => new Date(Date.parse(`${day}T00:00:00Z`) - n * 86_400_000).toISOString().slice(0, 10);
const limitOf = (raw: string | undefined, fallback: number) => {
  const n = Number.parseInt(raw ?? "", 10);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
};
const COLUMNS = "day, taken_at, worker_requests, d1_rows_read, d1_rows_written, d1_storage_bytes, d1_largest_bytes, " +
  "d1_largest_name, load_rows, load_total, load_per_day, load_ready, load_written, load_budget, warning";

/** Read the snapshots and search's own counters (a few rows; the admin pages only). */
export async function cfUsage(env: Env, nowMs: number): Promise<CfUsage> {
  const today = new Date(nowMs).toISOString().slice(0, 10);
  const db = env.SEARCH_DB;
  if (!db) return { ok: false, why: "The search database is not connected to this Worker.", today, rows: [], budgetDays: [] };
  const since = dayMinus(today, CF_WINDOW_DAYS - 1);
  try {
    const [snap, used, hits] = await db.batch([
      db.prepare(`SELECT ${COLUMNS} FROM cf_usage ORDER BY day DESC LIMIT ?`).bind(CF_WINDOW_DAYS + 1),
      db.prepare("SELECT day, rows_read, requests FROM search_usage WHERE day >= ?").bind(since),
      db.prepare("SELECT day FROM search_budget_days WHERE day >= ? AND hits > 0").bind(since),
    ]);
    const readCap = limitOf(env.SEARCH_DAILY_ROWS_READ, DAILY_ROWS_READ);
    const reqCap = limitOf(env.SEARCH_DAILY_REQUESTS, DAILY_REQUESTS);
    const days = new Set((hits!.results as Array<{ day: string }>).map((r) => r.day));
    // The counters say so too: search stops at its cap, so a day that reached it said no.
    for (const r of used!.results as Array<{ day: string; rows_read: number; requests: number }>) {
      if ((readCap > 0 && Number(r.rows_read) >= readCap) || (reqCap > 0 && Number(r.requests) >= reqCap)) days.add(r.day);
    }
    return { ok: true, today, rows: snap!.results as unknown as CfRow[], budgetDays: [...days].sort().reverse() };
  } catch {
    return { ok: false, why: "The search database could not be read (is migration 0002 applied?).", today, rows: [], budgetDays: [] };
  }
}

export interface CfLine {
  key: CfKey; label: string; limit: number; unit: string; bytes: boolean;   // as on the latest day
  others: boolean;                                     // rows written by everything but the first load
  latest: number | null; peak: number | null;          // values; null = not read
  latestPct: number | null; peakPct: number | null;    // percent of the limit
}

export interface CfVerdict {
  level: "pay" | "close" | "ok" | "none";               // none = no snapshot yet
  reasons: string[];                                   // what triggered pay (or close), plain words
  lines: CfLine[];
  latest: CfRow | null;
  windowDays: number;                                  // snapshot days the peak was taken over
}

/** The thresholds: pay when any limit's 7-day peak is at or above 70% (storage per database and in
 * all included) or search said daily_budget on any of the last 7 days; close from 50%. */
export function cfVerdict(u: CfUsage): CfVerdict {
  const latest = u.rows[0] ?? null;
  const since = dayMinus(u.today, CF_WINDOW_DAYS);
  const inWindow = u.rows.filter((r) => r.day >= since && r.day < u.today);
  const win = inWindow.length ? inWindow : latest ? [latest] : [];
  const lines: CfLine[] = CF_LIMITS.map((l) => {
    const m = (r: CfRow | null) => (r ? measure(l, r) : null);
    const points = win.map(m).filter((x): x is Measure => x !== null);
    const top = points.length ? points.reduce((a, b) => (b.value / b.limit > a.value / a.limit ? b : a)) : null;
    const lat = m(latest);
    const shape = lat ?? top;
    const others = shape?.others ?? false;
    return { key: l.key, label: others ? "D1 rows written, not counting the search first load" : l.label,
             limit: shape?.limit ?? l.limit, unit: others ? "a day beside the load" : l.unit, bytes: l.bytes, others,
             latest: lat?.value ?? null, peak: top?.value ?? null,
             latestPct: lat ? (100 * lat.value) / lat.limit : null, peakPct: top ? (100 * top.value) / top.limit : null };
  });
  const reasons: string[] = [];
  for (const l of lines) {
    if (l.peakPct !== null && l.peakPct >= CF_PAY_AT) reasons.push(reasonOf(l, l.peakPct));
  }
  if (u.budgetDays.length) {
    reasons.push(`Search hit its daily limit on ${u.budgetDays.length === 1 ? u.budgetDays[0] : `${u.budgetDays.length} days (last ${u.budgetDays[0]})`}`);
  }
  if (reasons.length) return { level: "pay", reasons, lines, latest, windowDays: win.length };
  if (!latest) return { level: "none", reasons, lines, latest, windowDays: 0 };
  const close = lines.filter((l) => l.peakPct !== null && l.peakPct >= CF_CLOSE_AT)
    .map((l) => reasonOf(l, l.peakPct!));
  return { level: close.length ? "close" : "ok", reasons: close, lines, latest, windowDays: win.length };
}

interface Measure { value: number; limit: number; others: boolean }

/** One day's value of a limit, and the limit it is judged against. While search's first load is
 * unfinished, the loader spends its daily budget (80,000 rows by default) on purpose, so rows
 * written are judged by everyone else's writes (the day's total less the loader's own) against
 * what the budget leaves of the 100,000 (20,000 by default): the banner then only says to pay
 * when the broker's own writes squeeze the load. Once the load is done, the plain total again. */
function measure(l: (typeof CF_LIMITS)[number], r: CfRow): Measure | null {
  const raw = r[l.key];
  if (raw === null || raw === undefined || !Number.isFinite(Number(raw))) return null;
  const value = Number(raw);
  const budget = Number(r.load_budget ?? 0), own = r.load_written;
  if (l.key === "d1_rows_written" && !r.load_ready && own !== null && own !== undefined && budget > 0 && budget < l.limit) {
    return { value: Math.max(0, value - Number(own)), limit: l.limit - budget, others: true };
  }
  return { value, limit: l.limit, others: false };
}

const reasonOf = (l: CfLine, p: number) => l.others
  ? `D1 rows written by everything but the search first load reached ${pctText(p)} of the ${num(l.limit)} a day the load leaves free`
  : `${l.label} reached ${pctText(p)} of the free ${limitText(l)}`;

// ---- words and numbers --------------------------------------------------------------------- //

/** Floor, so a page never says 70% while the banner (which compares the exact number) is off. */
export const pctText = (p: number) => (p > 0 && p < 1 ? "under 1%" : `${Math.floor(p)}%`);
export function bytesText(b: number): string {
  if (b >= 1e9) return `${(b / 1e9).toFixed(2).replace(/\.?0+$/, "")} GB`;
  if (b >= 1e6) return `${(b / 1e6).toFixed(1).replace(/\.0$/, "")} MB`;
  if (b >= 1e3) return `${Math.round(b / 1e3)} KB`;
  return `${Math.round(b)} bytes`;
}
const valueText = (l: { bytes: boolean }, v: number) => (l.bytes ? bytesText(v) : num(v));
const limitText = (l: { bytes: boolean; limit: number; unit: string }) => `${valueText(l, l.limit)} ${l.unit}`;

/** "X of Y jobs loaded, about N days left", or the finished load, or null when unknown. */
export function loadText(r: CfRow | null): string | null {
  if (!r || r.load_total === null || r.load_total === undefined) return null;
  const done = Number(r.load_rows ?? 0), total = Number(r.load_total);
  if (r.load_ready) return `First load done: ${num(done)} jobs in search`;
  const left = Math.max(0, total - done), per = Number(r.load_per_day ?? 0);
  const days = per > 0 ? Math.max(1, Math.ceil(left / per)) : null;
  return `${num(done)} of ${num(total)} jobs loaded, ` + (days === null ? "days left unknown" : `about ${days} day${days === 1 ? "" : "s"} left`);
}

// ---- the banner (every page) and the card (Health) ----------------------------------------- //

/** The banner on top of every admin page: "Time to switch" at 70%, a softer note at 50%, else "". */
export function cfBanner(v: CfVerdict): string {
  if (v.level !== "pay" && v.level !== "close") return "";
  const pay = v.level === "pay";
  const head = pay ? "Time to switch to Workers Paid ($5 a month)" : "Getting close to the Cloudflare free plan";
  return `<div class="flash notice ${pay ? "bad" : "warn"}" role="${pay ? "alert" : "status"}"><span class="dot ${pay ? "bad" : "warn"}"></span>` +
    `<span class="nt">${esc(head)}</span><span class="muted">${esc(v.reasons.join(". "))}.</span>` +
    `<span class="nlinks"><a class="btn" href="/health#cloudflare">Details</a>` +
    (pay ? `<a class="btn primary" href="${CF_PLANS_URL}" rel="noopener noreferrer">Cloudflare plans</a>` : "") + `</span></div>`;
}

const tone = (p: number | null) => (p === null ? "plain" : p >= CF_PAY_AT ? "bad" : p >= CF_CLOSE_AT ? "warn" : "ok");
const cell = (l: CfLine, v: number | null, p: number | null) =>
  v === null || p === null ? `<span class="muted">Not read</span>` : badge(`${valueText(l, v)} (${pctText(p)})`, tone(p));

/** The Health page's "Cloudflare Free Plan" card. */
export function cfCard(u: CfUsage, v: CfVerdict, nowS: number): string {
  const status = v.level === "pay" ? badge("Time to pay", "bad") : v.level === "close" ? badge("Getting close", "warn")
    : v.level === "ok" ? badge("Within limits", "ok") : badge("No data yet", "plain");
  const o = { span: 4 as const, id: "cloudflare", aside: status };
  if (!u.ok) return card("Cloudflare Free Plan", `<p class="muted">${esc(u.why ?? "No data.")}</p>`, o);
  const r = v.latest;
  if (!r) {
    return card("Cloudflare Free Plan", `<p class="muted">No snapshot yet. The feed workflow writes one a day once its ` +
      `Cloudflare token has the Account Analytics Read permission.</p>` +
      (u.budgetDays.length ? `<p>${esc(v.reasons.join(". "))}.</p>` : ""), o);
  }
  const rows = v.lines.map((l) => [esc(l.label), cell(l, l.latest, l.latestPct), cell(l, l.peak, l.peakPct),
    esc(l.others ? `${num(l.limit)} a day (100,000 less the load's budget)` : limitText(l))]);
  // The first load's own writes, on a line of their own: planned, inside its budget, never a warning.
  if (!r.load_ready && r.load_written !== null && r.load_written !== undefined) {
    const since = dayMinus(u.today, CF_WINDOW_DAYS);
    const own = [r, ...u.rows.filter((x) => x.day >= since && x.day < u.today)]
      .filter((x) => !x.load_ready && x.load_written !== null && x.load_written !== undefined).map((x) => Number(x.load_written));
    const i = v.lines.findIndex((l) => l.key === "d1_rows_written") + 1;
    rows.splice(i, 0, ["Search first load", `${num(Number(r.load_written))} rows, planned`, `${num(Math.max(...own))} rows, planned`,
      esc(r.load_budget ? `up to ${num(Number(r.load_budget))} a day (its budget)` : "no daily budget")]);
  }
  const taken = Date.parse(r.taken_at);
  const age = Number.isFinite(taken) ? Math.max(0, nowS - Math.floor(taken / 1000)) : null;
  const staleDays = Math.round((Date.parse(`${u.today}T00:00:00Z`) - Date.parse(`${r.day}T00:00:00Z`)) / 86_400_000);
  const info: Array<[string, string]> = [
    ["Last snapshot", esc(`${r.day}` + (age !== null ? `, written ${age < 3600 ? `${Math.max(1, Math.round(age / 60))} min` : age < 172_800 ? `${Math.round(age / 3600)} h` : `${Math.round(age / 86_400)} days`} ago` : ""))],
    ["Search first load", esc(loadText(r) ?? "Not known yet")],
    ["Search daily limit", u.budgetDays.length ? badge(`Hit on ${u.budgetDays.join(", ")}`, "bad") : badge("Not hit in 7 days", "ok")],
  ];
  if (r.d1_largest_name) info.push(["Largest database", esc(r.d1_largest_name)]);
  if (staleDays > 2) info.push(["Note", badge(`The last snapshot is ${staleDays} days old: check the feed workflow`, "warn")]);
  if (r.warning) info.push(["Not read", esc(r.warning)]);
  return card("Cloudflare Free Plan",
    table(["Limit", "Latest day", `Peak, ${v.windowDays} day${v.windowDays === 1 ? "" : "s"}`, "Free plan"], rows, "") + facts(info), o);
}
