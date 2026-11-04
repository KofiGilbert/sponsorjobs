// The founder's private admin dashboard at https://admin.sponsorjobs.ai, built to ByeWind's
// Dashboard UI Kit (see src/admin_ui.ts).
//
// Pages (GET): / overview (range and chart tabs in the query), /revenue, /usage, /users (filters,
// search, sort), /users/<id>, /health, /feed (job feed and GitHub jobs), /audit, /alerts, and
// /admin/api/stats (the overview numbers as JSON). /prefs sets the dashboard's own cookie (theme,
// sidebar and right sidebar shown or hidden, Favorites / Recently tab, starred pages) and returns.
// The pages are in src/admin_pages.ts, the extra reads in src/admin_data.ts.
//
// Actions (POST, src/admin_actions.ts): give a pass, add interviews, suspend or restore an account,
// reveal a person's full e-mail, change or pause the free AI limit, send a test alert. Each needs
// this host's own origin (Origin, or Sec-Fetch-Site: same-origin), a typed reason, and writes an
// admin_actions row; nothing else on this host writes data. Every request is checked by
// src/access.ts first. Output never contains a token, a token hash, a code hash, a salt, or a
// forwarded mail's code or link: the queries select only the columns they show.

import { verifyAccess } from "./access";
import { type Env, intVar } from "./env";
import { type AiState, aiStatus, describeAi } from "./outage";
import { dayOf, FREE_MODEL, INTERVIEW_SECONDS, PASS_MODEL, PASSES, periodOf } from "./metering";
import { FREE_POOL_CAP_KEY, FREE_POOL_USER, poolCapOverride } from "./store";
import { ActionError, accountAction, formOf, logReveal, sameOrigin, setFreePool, testAlert } from "./admin_actions";
import {
  aiCostSeries, aiStatusRow, auditLog, emailDomains, expiredPasses, feed, githubRuns, listUsers, monthly, poolDays, poolSetting,
  RANGES, type RangeKey, rangeBounds, rangeCounts, recentActivity, revenue, revenueSeries, signupNetworks, spend, totalUsersSeries,
  userDetail,
} from "./admin_data";
import {
  type Attention, alertsPage, attention, auditPage, type ChartKey, feedPage, healthPage, notFound, overviewPage, rail, revenuePage,
  usagePage, userPage, usersPage,
} from "./admin_pages";
import { DEFAULT_FAVS, esc, KEY_SCRIPT, maskEmail, type PageId, type Prefs, type Shell, shell } from "./admin_ui";
import { blendedPerToken, INPUT_SHARE, RATES, round2, TAVUS_PER_MINUTE } from "./admin_data";

export const ADMIN_HOST_DEFAULT = "admin.sponsorjobs.ai";

// Cost assumptions (estimates; the real bills are in the Anthropic and Tavus consoles) live in
// src/admin_data.ts: Anthropic list prices blended at 80% input, Tavus at $0.35 a minute.
export { INPUT_SHARE, RATES, TAVUS_PER_MINUTE } from "./admin_data";

// Resend free plan (resend.com/pricing): 100 e-mails a day, 3,000 a month. Pro is $20/month with no
// daily cap. The dashboard turns amber at 70 a day and red at 90.
export const RESEND_DAILY_CAP = 100;
export const RESEND_MONTHLY_CAP = 3000;
export const RESEND_AMBER = 70;
export const RESEND_RED = 90;
export const RESEND_ADVICE = "Resend free plan: 100 a day. Upgrade to Pro ($20/month, no daily cap) before this fills.";


export interface RecentSignIn {
  email: string;
  account_id: string;
  created_at: number | null;   // unix seconds; null = before created_at was recorded
  email_at: number | null;     // unix seconds the e-mail was attached; null = before it was recorded
  plan: string;                // "free" or the active pass name
}

export interface Stats {
  generated_at: number;
  // Whether model calls on the company key are going through (src/outage.ts): the same line
  // /health shows, plus the cause and when the owner was last messaged.
  ai: {
    state: AiState;
    summary: string;                       // "ok" | "company_outage since <time>" | "transient"
    since: number | null;                  // unix seconds the state began; null when ok
    last_alert_at: number | null;          // unix seconds the owner was last messaged
    note: string;                          // the short upstream cause (empty when ok)
  };
  accounts: {
    total: number;
    with_email: number;
    created_unknown: number;               // accounts from before created_at existed
    new_this_week: number;                 // last 7 days, accounts with a known created_at only
    new_per_day: Array<{ day: string; count: number }>;   // last 30 UTC days, oldest first
  };
  plans: {
    free: number;
    active_pass30: number;
    active_pass90: number;
    accounts_ever_on_pass: Record<string, number>;
    checkout_purchases: number;            // Stripe checkouts granted: passes + interview packs together
    extra_interview_credits: { seconds: number; interviews: number };
  };
  usage_month: {
    period: string;
    active_accounts: number;
    packages: { free: number; pass: number; total: number };
    ai_tokens: { free: number; pass: number; total: number };
    ai_cost_usd: { free: number; pass: number; total: number };
    interviews: { count: number; minutes: number; cost_usd: number };
  };
  free_pool_today: { day: string; tokens: number; cap: number; percent: number };
  sign_in_emails: {
    day: string; today: number; daily_cap: number;
    month: number; monthly_cap: number;
    level: "ok" | "amber" | "red";         // amber from 70 a day, red from 90 (or the month at 90%)
    advice: string;
  };
  recent_sign_ins: RecentSignIn[];
  assumptions: {
    rates_per_million: typeof RATES;
    input_share: number;
    tavus_per_minute: number;
    note: string;
  };
}

type N = { n: number | null };

export async function collectStats(env: Env, nowMs: number): Promise<Stats> {
  const db = env.DB;
  const now = Math.floor(nowMs / 1000);
  const period = periodOf(nowMs);
  const today = dayOf(nowMs);
  const monthStart = Math.floor(Date.UTC(new Date(nowMs).getUTCFullYear(), new Date(nowMs).getUTCMonth(), 1) / 1000);
  const todayStart = Math.floor(Date.parse(`${today}T00:00:00Z`) / 1000);
  const chartStart = todayStart - 29 * 86_400;
  const passList = PASSES.map(() => "?").join(", ");

  const res = await db.batch([
    /* 0 */ db.prepare("SELECT COUNT(*) AS n FROM accounts"),
    /* 1 */ db.prepare("SELECT COUNT(*) AS n FROM accounts WHERE email IS NOT NULL AND email != ''"),
    /* 2 */ db.prepare("SELECT COUNT(*) AS n FROM accounts WHERE created_at IS NULL"),
    /* 3 */ db.prepare("SELECT COUNT(*) AS n FROM accounts WHERE created_at >= ?").bind(now - 7 * 86_400),
    /* 4 */ db.prepare("SELECT date(created_at, 'unixepoch') AS day, COUNT(*) AS n FROM accounts " +
                       "WHERE created_at >= ? GROUP BY day").bind(chartStart),
    /* 5 */ db.prepare(`SELECT plan, COUNT(*) AS n FROM users WHERE user != ? AND plan IN (${passList}) ` +
                       "AND pass_until > ? GROUP BY plan").bind(FREE_POOL_USER, ...PASSES, now),
    /* 6 */ db.prepare(`SELECT plan, COUNT(*) AS n FROM users WHERE user != ? AND plan IN (${passList}) ` +
                       "AND pass_until > 0 GROUP BY plan").bind(FREE_POOL_USER, ...PASSES),
    /* 7 */ db.prepare("SELECT COUNT(*) AS n FROM processed_events WHERE key LIKE 'checkout:%'"),
    /* 8 */ db.prepare("SELECT COALESCE(SUM(MAX(credit_seconds, 0)), 0) AS n FROM users WHERE user != ?")
                .bind(FREE_POOL_USER),
    /* 9 */ db.prepare("SELECT COUNT(*) AS n FROM (SELECT user FROM usage WHERE period = ? AND user != ? " +
                       "UNION SELECT user FROM avatar_sessions WHERE started >= ?)")
                .bind(period, FREE_POOL_USER, monthStart),
    /* 10 */ db.prepare("SELECT COALESCE(SUM(packages), 0) AS free, COALESCE(SUM(pass_packages), 0) AS pass, " +
                        "COALESCE(SUM(llm_tokens), 0) AS tokens FROM usage WHERE period = ? AND user != ?")
                 .bind(period, FREE_POOL_USER),
    /* 11 */ db.prepare("SELECT COALESCE(SUM(llm_tokens), 0) AS n FROM usage WHERE user = ? AND period LIKE ?")
                 .bind(FREE_POOL_USER, `${period}-%`),
    /* 12 */ db.prepare("SELECT COUNT(*) AS count, COALESCE(SUM(used), 0) AS used FROM avatar_sessions " +
                        "WHERE started >= ?").bind(monthStart),
    /* 13 */ db.prepare("SELECT COALESCE(SUM(llm_tokens), 0) AS n FROM usage WHERE user = ? AND period = ?")
                 .bind(FREE_POOL_USER, today),
    /* 14 */ db.prepare(
      "SELECT a.account_id, a.email, a.created_at, a.email_at, u.plan, u.pass_until " +
      "FROM accounts a LEFT JOIN users u ON u.user = a.account_id " +
      "WHERE a.email IS NOT NULL AND a.email != '' " +
      "ORDER BY COALESCE(a.email_at, a.created_at, 0) DESC, a.rowid DESC LIMIT 20"),
    /* 15 */ db.prepare("SELECT COALESCE(SUM(n), 0) AS n FROM email_sends WHERE day = ?").bind(today),
    /* 16 */ db.prepare("SELECT COALESCE(SUM(n), 0) AS n FROM email_sends WHERE day LIKE ?").bind(`${period}-%`),
    /* 17 */ db.prepare("SELECT value FROM admin_settings WHERE key = ?").bind(FREE_POOL_CAP_KEY),
  ]);
  const one = (i: number) => Number((res[i]!.results[0] as N | undefined)?.n ?? 0);
  const rows = <T>(i: number) => res[i]!.results as T[];

  const total = one(0);
  const perDay = new Map(rows<{ day: string; n: number }>(4).map((r) => [r.day, r.n]));
  const new_per_day = Array.from({ length: 30 }, (_, i) => {
    const day = new Date((chartStart + i * 86_400) * 1000).toISOString().slice(0, 10);
    return { day, count: perDay.get(day) ?? 0 };
  });

  const active = Object.fromEntries(rows<{ plan: string; n: number }>(5).map((r) => [r.plan, r.n]));
  const ever = Object.fromEntries(rows<{ plan: string; n: number }>(6).map((r) => [r.plan, r.n]));
  const activePasses = Object.values(active).reduce((a, b) => a + b, 0);
  const creditSeconds = one(8);

  const u = rows<{ free: number; pass: number; tokens: number }>(10)[0] ?? { free: 0, pass: 0, tokens: 0 };
  // Every free-plan completion is also charged to the shared pool, so the pool's days this month
  // ARE the free tokens; whatever else was spent was on a pass.
  const freeTokens = Math.min(one(11), u.tokens);
  const passTokens = Math.max(0, u.tokens - freeTokens);
  const freeCost = freeTokens * blendedPerToken(RATES.free);
  const passCost = passTokens * blendedPerToken(RATES.pass);
  const av = rows<{ count: number; used: number }>(12)[0] ?? { count: 0, used: 0 };
  const minutes = av.used / 60;

  const cap = poolCapOverride((res[17]!.results[0] as { value: string } | undefined)?.value)
    ?? intVar(env.FREE_POOL_DAILY_TOKENS, 5_000_000);
  const poolToday = one(13);

  const mailsToday = one(15);
  const mailsMonth = one(16);
  const mailLevel = mailsToday >= RESEND_RED || mailsMonth >= 0.9 * RESEND_MONTHLY_CAP ? "red"
    : mailsToday >= RESEND_AMBER || mailsMonth >= 0.7 * RESEND_MONTHLY_CAP ? "amber" : "ok";

  const recent = rows<{ account_id: string; email: string; created_at: number | null; email_at: number | null;
                        plan: string | null; pass_until: number | null }>(14)
    .map((r): RecentSignIn => ({
      email: r.email, account_id: r.account_id, created_at: r.created_at ?? null, email_at: r.email_at ?? null,
      plan: r.plan && PASSES.includes(r.plan) && (r.pass_until ?? 0) > now ? r.plan : "free",
    }));

  const ai = await aiStatus(db);

  return {
    generated_at: now,
    ai: { state: ai.state, summary: describeAi(ai), since: ai.state === "ok" ? null : ai.since,
          last_alert_at: ai.last_alert_at, note: ai.note },
    accounts: { total, with_email: one(1), created_unknown: one(2), new_this_week: one(3), new_per_day },
    plans: {
      free: Math.max(0, total - activePasses),
      active_pass30: active.pass30 ?? 0,
      active_pass90: active.pass90 ?? 0,
      accounts_ever_on_pass: ever,
      checkout_purchases: one(7),
      extra_interview_credits: { seconds: creditSeconds, interviews: round2(creditSeconds / INTERVIEW_SECONDS) },
    },
    usage_month: {
      period,
      active_accounts: one(9),
      packages: { free: u.free, pass: u.pass, total: u.free + u.pass },
      ai_tokens: { free: freeTokens, pass: passTokens, total: u.tokens },
      ai_cost_usd: { free: round2(freeCost), pass: round2(passCost), total: round2(freeCost + passCost) },
      interviews: { count: av.count, minutes: round2(minutes), cost_usd: round2(minutes * TAVUS_PER_MINUTE) },
    },
    free_pool_today: { day: today, tokens: poolToday, cap,
                       percent: cap > 0 ? round2((100 * poolToday) / cap) : 0 },
    sign_in_emails: { day: today, today: mailsToday, daily_cap: RESEND_DAILY_CAP, month: mailsMonth,
                      monthly_cap: RESEND_MONTHLY_CAP, level: mailLevel, advice: RESEND_ADVICE },
    recent_sign_ins: recent,
    assumptions: {
      rates_per_million: RATES, input_share: INPUT_SHARE, tavus_per_minute: TAVUS_PER_MINUTE,
      note: "Costs are estimates from token counts at list prices. Pass packages are counted only " +
            "and sign-in e-mails are counted from the dashboard's release on; accounts and sign-ins " +
            "created before it have no date.",
    },
  };
}


// ---- routing ----------------------------------------------------------------------------------- //

const security = {
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "referrer-policy": "no-referrer",
  "x-robots-tag": "noindex, nofollow",
};

// Inter comes from Google Fonts (the kit's typeface); the one script is the kit's ⌘K shortcut,
// allowed by its hash. Nothing else loads from anywhere.
let scriptHash = "";
async function pageCsp(): Promise<string> {
  if (!scriptHash) {
    const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(KEY_SCRIPT));
    scriptHash = btoa(String.fromCharCode(...new Uint8Array(d)));
  }
  return "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; " +
    `script-src 'sha256-${scriptHash}'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`;
}

const text = (body: string, status: number) =>
  new Response(body, { status, headers: { "content-type": "text/plain", ...security } });
async function html(body: string, status = 200, cookie?: string): Promise<Response> {
  const h = new Headers({ "content-type": "text/html; charset=utf-8", ...security, "content-security-policy": await pageCsp() });
  if (cookie) h.append("set-cookie", cookie);
  return new Response(body, { status, headers: h });
}
const back = (to: string, cookie?: string) => {
  const h = new Headers({ location: to, ...security });
  if (cookie) h.append("set-cookie", cookie);
  return new Response(null, { status: 303, headers: h });
};

/** A flash line carried on a redirect: our own messages only, so a crafted link cannot put
 * arbitrary words on the page (it is escaped either way). */
const FLASH_OK = /^[A-Za-z0-9 .,'"()%-]{1,200}$/;
function flashOf(url: URL): Shell["flash"] {
  const ok = url.searchParams.get("ok"), err = url.searchParams.get("err");
  if (ok && FLASH_OK.test(ok)) return { kind: "ok", text: ok };
  if (err && FLASH_OK.test(err)) return { kind: "bad", text: err };
  return null;
}
const withFlash = (path: string, kind: "ok" | "err", msg: string) => `${path}?${kind}=${encodeURIComponent(msg)}`;

// ---- the dashboard's own preferences (one cookie, nothing in the database) ----------------------- //

const PREFS_COOKIE = "sj_admin";
const LOCAL = /^\/(?!\/)[^\s]{0,300}$/;          // a path on this host, never "//other.host"
function readPrefs(req: Request): Prefs {
  const base: Prefs = { theme: "", sideHidden: false, railHidden: false, tab: "favorites", favs: DEFAULT_FAVS, recent: [] };
  const raw = (req.headers.get("cookie") ?? "").split(/;\s*/).find((c) => c.startsWith(`${PREFS_COOKIE}=`))?.slice(PREFS_COOKIE.length + 1);
  if (!raw) return base;
  try {
    const j = JSON.parse(decodeURIComponent(atob(raw.replace(/-/g, "+").replace(/_/g, "/"))));
    const links = (v: unknown) => Array.isArray(v) ? v.filter((x): x is { href: string; label: string } =>
      x && typeof x.href === "string" && LOCAL.test(x.href) && typeof x.label === "string").map((x) => ({ href: x.href, label: x.label.slice(0, 40) })) : [];
    return { theme: j.theme === "dark" || j.theme === "light" ? j.theme : "", sideHidden: j.side === 1, railHidden: j.rail === 1,
             tab: j.tab === "recently" ? "recently" : "favorites", favs: Array.isArray(j.favs) ? links(j.favs).slice(0, 8) : DEFAULT_FAVS,
             recent: links(j.recent).slice(0, 6) };
  } catch { return base; }
}
function prefsCookie(p: Prefs): string {
  const j = JSON.stringify({ theme: p.theme, side: p.sideHidden ? 1 : 0, rail: p.railHidden ? 1 : 0, tab: p.tab, favs: p.favs, recent: p.recent });
  const v = btoa(encodeURIComponent(j)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${PREFS_COOKIE}=${v}; Path=/; Max-Age=31536000; Secure; HttpOnly; SameSite=Lax`;
}
/** Remember a page in "Recently" (newest first, six at most). Labels are the masked titles. */
function visited(p: Prefs, href: string, label: string): Prefs {
  return { ...p, recent: [{ href, label: label.slice(0, 40) }, ...p.recent.filter((r) => r.href !== href)].slice(0, 6) };
}
/** GET /prefs?...&back=/path : change one preference and go back. Only the cookie changes. */
function prefsResponse(url: URL, p: Prefs): Response {
  const q = url.searchParams;
  const to = LOCAL.test(q.get("back") ?? "") ? q.get("back")! : "/";
  let n: Prefs = { ...p };
  if (q.get("toggle") === "side") n.sideHidden = !p.sideHidden;
  if (q.get("toggle") === "rail") n.railHidden = !p.railHidden;
  const t = q.get("theme");
  if (t === "dark" || t === "light") n.theme = t;
  if (t === "system") n.theme = "";
  const tab = q.get("tab");
  if (tab === "favorites" || tab === "recently") n.tab = tab;
  if (q.get("fav") === "1") n = { ...n, tab: "favorites", favs: [{ href: to, label: (q.get("label") ?? to).slice(0, 40) }, ...p.favs.filter((f) => f.href !== to)].slice(0, 8) };
  if (q.get("unfav") === "1") n.favs = p.favs.filter((f) => f.href !== to);
  return back(to, prefsCookie(n));
}

/** The page after "Send test alert": what happened, and the way back. */
function renderTestResult(result: string): string {
  const ok = result === "sent";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test alert</title><style>body{font:15px/1.5 system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px}</style></head><body>
<h1>${ok ? "Test alert sent" : "Test alert not sent"}</h1>
<p>${ok ? "Check Telegram: the message should be there now." : `Reason: ${esc(result)}`}</p>
<p><a href="/alerts">Back to the dashboard</a></p></body></html>`;
}

/** Is this request for the admin host? (Only routes it here; the JWT is what lets it in.) */
export function isAdminHost(req: Request, env: Env): boolean {
  const want = (env.ADMIN_HOST || ADMIN_HOST_DEFAULT).toLowerCase();
  return new URL(req.url).hostname.toLowerCase() === want;
}

const USER_PATH = /^\/users\/([^/]{1,200})(?:\/(give-pass|add-interviews|suspend|unsuspend|reveal))?$/;
/** The account id in a /users/<id> path, or "" when the escape is malformed (then nothing matches). */
const idOf = (raw: string) => { try { return decodeURIComponent(raw); } catch { return ""; } };

/** What every page's right sidebar and badges need. Stripe, the feed and GitHub are cached. */
async function railData(env: Env, nowMs: number, stats?: Stats) {
  const now = Math.floor(nowMs / 1000);
  const [st, pool, rev, fd, runs, activity, suspended] = await Promise.all([
    stats ? Promise.resolve(stats) : collectStats(env, nowMs), poolSetting(env), revenue(env, nowMs), feed(nowMs), githubRuns(env, nowMs),
    recentActivity(env.DB, 6), suspendedCount(env.DB)]);
  const att = attention({ stats: st, pool, revenue: rev, feed: fd, runs, env, now, suspended });
  const contacts = st.recent_sign_ins.map((r) => ({ account_id: r.account_id, email: r.email }));
  return { stats: st, pool, revenue: rev, feed: fd, runs, att, rail: rail({ attention: att, activity, contacts, now }) };
}

export async function handleAdmin(req: Request, env: Env, nowMs: number): Promise<Response> {
  const who = await verifyAccess(req, env, Math.floor(nowMs / 1000));
  if (!who) return text("Forbidden", 403);
  const url = new URL(req.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const now = Math.floor(nowMs / 1000);
  const admin = who.email;
  const prefs = readPrefs(req);

  if (req.method === "POST") {
    if (!sameOrigin(req)) return text("Forbidden", 403);
    if (path === "/admin/test-alert") return html(renderTestResult(await testAlert(env, admin, now)));
    if (path === "/settings/free-pool") {
      try {
        return back(withFlash("/usage", "ok", await setFreePool(env, admin, await formOf(req), now)));
      } catch (e) {
        if (e instanceof ActionError) return back(withFlash("/usage", "err", e.message));
        throw e;
      }
    }
    const m = USER_PATH.exec(path);
    if (m && m[2]) {
      const id = idOf(m[1]!);
      const home = `/users/${encodeURIComponent(id)}`;
      try {
        if (m[2] === "reveal") {
          await logReveal(env, admin, id, now);
          return userResponse(env, id, nowMs, admin, prefs, home, { revealed: true });
        }
        return back(withFlash(home, "ok", await accountAction(env, admin, id, m[2], await formOf(req), now)));
      } catch (e) {
        if (e instanceof ActionError) return back(withFlash(home, "err", e.message));
        throw e;
      }
    }
    return text("Method not allowed", 405);
  }
  if (req.method !== "GET" && req.method !== "HEAD") return text("Method not allowed", 405);

  if (path === "/admin/api/stats") {
    return new Response(JSON.stringify(await collectStats(env, nowMs)), { headers: { "content-type": "application/json", ...security } });
  }
  if (path === "/prefs") return prefsResponse(url, prefs);

  const here = path + url.search.replace(/([?&])(ok|err)=[^&]*/g, "$1").replace(/[?&]+$/, "");
  /** Render a page with the right sidebar, remember it in "Recently", and keep the flash. */
  const page = async (id: PageId, ttl: string, body: string, o: Partial<Shell> & { stats?: Stats; rd?: Awaited<ReturnType<typeof railData>> } = {}) => {
    const rd = o.rd ?? await railData(env, nowMs, o.stats);
    const p2 = visited(prefs, here, ttl);
    return html(shell({ page: id, title: ttl, body, viewer: admin, generatedAt: now, flash: flashOf(url), prefs: p2, here, rail: rd.rail,
      badges: badgesFor(rd.att), ...o }), 200, prefsCookie(p2));
  };

  if (path === "/" || path === "/admin") {
    const range = (RANGES.find((r) => r.key === url.searchParams.get("range"))?.key ?? "30d") as RangeKey;
    const chart = (["users", "revenue", "ai"] as const).find((c) => c === url.searchParams.get("chart")) ?? "users" as ChartKey;
    const today = dayOf(nowMs);
    const todayStart = Math.floor(Date.parse(`${today}T00:00:00Z`) / 1000);
    const bounds = rangeBounds(range, nowMs);
    const rd = await railData(env, nowMs);
    const [counts, sp, pDays, expired, series] = await Promise.all([
      rangeCounts(env.DB, bounds), spend(env.DB, periodOf(nowMs), now, 6), poolDays(env.DB, todayStart, 12), expiredPasses(env.DB, now),
      chart === "users" ? totalUsersSeries(env.DB, nowMs) : chart === "ai" ? aiCostSeries(env.DB, nowMs) : Promise.resolve(revenueSeries(rd.revenue, nowMs))]);
    const body = overviewPage({ stats: rd.stats, pool: rd.pool, revenue: rd.revenue, now, range, counts, bounds, chart, series,
      spend: sp, poolDays: pDays, expired });
    return page("overview", "Overview", body, { rd });
  }
  if (path === "/users") {
    const filter = (["all", "free", "pass", "email", "suspended"] as const).find((f) => f === url.searchParams.get("filter")) ?? "all";
    const sort = (["newest", "oldest", "tokens"] as const).find((s) => s === url.searchParams.get("sort")) ?? "newest";
    const pg = Math.max(1, Math.min(10_000, Number.parseInt(url.searchParams.get("page") ?? "1", 10) || 1));
    const q = (url.searchParams.get("q") ?? "").slice(0, 120);
    const { rows, total } = await listUsers(env.DB, { q, filter, sort, page: pg, period: periodOf(nowMs), now });
    return page("users", "Users", usersPage({ rows, total, q, filter, sort, page: pg, now }), { query: q, sub: filter });
  }
  const um = USER_PATH.exec(path);
  if (um && !um[2]) return userResponse(env, idOf(um[1]!), nowMs, admin, prefs, here, { flash: flashOf(url), act: url.searchParams.get("act") });
  if (path === "/revenue") {
    const [rd, granted30] = await Promise.all([railData(env, nowMs), grantedSince(env.DB, now - 30 * 86_400)]);
    return page("revenue", "Revenue", revenuePage({ revenue: rd.revenue, granted30, now }), { rd });
  }
  if (path === "/usage") {
    const today = dayOf(nowMs);
    const todayStart = Math.floor(Date.parse(`${today}T00:00:00Z`) / 1000);
    const [rd, days, sp, months, networks, domains] = await Promise.all([
      railData(env, nowMs), poolDays(env.DB, todayStart), spend(env.DB, periodOf(nowMs), now),
      monthly(env.DB, nowMs), signupNetworks(env.DB, today, env), emailDomains(env.DB)]);
    return page("usage", "Usage & costs", usagePage({ stats: rd.stats, pool: rd.pool, poolDays: days, spend: sp, months, networks, domains, now }), { rd });
  }
  if (path === "/health") {
    const rd = await railData(env, nowMs);
    return page("health", "Health", healthPage({ stats: rd.stats, revenue: rd.revenue, env, now }), { rd });
  }
  if (path === "/feed") {
    const rd = await railData(env, nowMs);
    return page("feed", "Job feed", feedPage({ feed: rd.feed, runs: rd.runs, now }), { rd });
  }
  if (path === "/alerts") {
    const [rd, ai, tests] = await Promise.all([railData(env, nowMs), aiStatusRow(env.DB),
      env.DB.prepare("SELECT id, at, admin, action, target, detail, reason FROM admin_actions WHERE action = 'test_alert' ORDER BY id DESC LIMIT 5").all<AuditRowT>()]);
    return page("alerts", "Alerts", alertsPage({ stats: rd.stats, env, ai, tests: tests.results, now }), { rd });
  }
  if (path === "/audit") {
    const before = Number.parseInt(url.searchParams.get("before") ?? "", 10);
    const rows = await auditLog(env.DB, { limit: 51, before: Number.isFinite(before) && before > 0 ? before : undefined });
    return page("audit", "Audit log", auditPage({ rows: rows.slice(0, 50), more: rows.length > 50 }));
  }
  const rd = await railData(env, nowMs);
  return html(shell({ page: "overview", title: "Not found", body: notFound(), viewer: admin, generatedAt: now, prefs, here, rail: rd.rail }), 404);
}

type AuditRowT = { id: number; at: number; admin: string; action: string; target: string; detail: string; reason: string };

async function userResponse(env: Env, id: string, nowMs: number, admin: string, prefs: Prefs, here: string,
                            o: { revealed?: boolean; flash?: Shell["flash"]; act?: string | null }): Promise<Response> {
  const now = Math.floor(nowMs / 1000);
  const [u, history, rd] = await Promise.all([userDetail(env.DB, id, now), auditLog(env.DB, { target: id, limit: 20 }), railData(env, nowMs)]);
  const base = { viewer: admin, generatedAt: now, flash: o.flash ?? null, rail: rd.rail, badges: badgesFor(rd.att), here };
  if (!u) return html(shell({ ...base, prefs, page: "users", title: "Not found", crumbs: [{ label: "Users", href: "/users" }], body: notFound() }), 404);
  const masked = u.email ? maskEmail(u.email) : "Anonymous account";
  const p2 = visited(prefs, `/users/${encodeURIComponent(id)}`, masked);          // never the revealed e-mail
  return html(shell({ ...base, prefs: p2, page: "users", title: u.email && o.revealed ? u.email : masked,
    crumbs: [{ label: "Users", href: "/users" }], body: userPage({ u, history, revealed: Boolean(o.revealed), now,
      act: (["pass", "interviews", "suspend"] as const).find((k) => k === o.act) ?? "pass" }) }), 200, prefsCookie(p2));
}

const suspendedCount = async (db: D1Database) =>
  Number((await db.prepare("SELECT COUNT(*) AS n FROM accounts WHERE suspended_at IS NOT NULL").first<{ n: number }>())?.n ?? 0);
const grantedSince = async (db: D1Database, since: number) =>
  Number((await db.prepare("SELECT COUNT(*) AS n FROM processed_events WHERE key LIKE 'checkout:%' AND at >= ?").bind(since)
    .first<{ n: number }>())?.n ?? 0);
const badgesFor = (att: Attention[]): Shell["badges"] => {
  const bad = att.filter((a) => a.level === "bad").length;
  return bad ? { health: String(bad) } : {};
};
