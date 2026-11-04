// The founder's private admin dashboard at https://admin.sponsorjobs.ai.
//
// Pages (GET): / overview, /users (search, filters), /users/<id> (one person), /revenue (Stripe),
// /usage (AI and interview use, costs, the free AI limit), /health (outside services), /audit
// (every admin action), and /admin/api/stats (the overview numbers as JSON). The look is in
// src/admin_ui.ts, the pages in src/admin_pages.ts, the extra reads in src/admin_data.ts.
//
// Actions (POST, src/admin_actions.ts): give a pass, add interviews, suspend or restore an account,
// reveal a person's full e-mail, change or pause the free AI limit, send a test alert. Each needs
// this host's own origin (Origin, or Sec-Fetch-Site: same-origin), a typed reason, and writes an
// admin_actions row; nothing else on this host writes. Every request is checked by src/access.ts
// first. Output never contains a token, a token hash, a code hash, a salt, or a forwarded mail's
// code or link: the queries select only the columns they show.

import { verifyAccess } from "./access";
import { type Env, intVar } from "./env";
import { type AiState, aiStatus, describeAi } from "./outage";
import { dayOf, FREE_MODEL, INTERVIEW_SECONDS, PASS_MODEL, PASSES, periodOf } from "./metering";
import { FREE_POOL_CAP_KEY, FREE_POOL_USER, poolCapOverride } from "./store";
import { ActionError, accountAction, formOf, logReveal, sameOrigin, setFreePool, testAlert } from "./admin_actions";
import {
  accountsPerDay, auditLog, emailDomains, feed, githubRuns, listUsers, monthly, poolDays, poolSetting, recentActivity, revenue, signupNetworks,
  spend, userDetail,
} from "./admin_data";
import {
  type Attention, attention, auditPage, healthPage, notFound, overviewPage, revenuePage, usagePage, userPage, usersPage,
} from "./admin_pages";
import { esc, maskEmail, type Shell, shell } from "./admin_ui";
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

// Inter comes from Google Fonts (the kit's typeface); nothing else is loaded from outside.
const PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; " +
  "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";

const text = (body: string, status: number) =>
  new Response(body, { status, headers: { "content-type": "text/plain", ...security } });
const html = (body: string, status = 200) =>
  new Response(body, { status, headers: { "content-type": "text/html; charset=utf-8", ...security, "content-security-policy": PAGE_CSP } });
const back = (to: string) => new Response(null, { status: 303, headers: { location: to, ...security } });

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

/** The page after "Send test alert": what happened, and the way back. */
function renderTestResult(result: string): string {
  const ok = result === "sent";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test alert</title><style>body{font:15px/1.5 system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px}</style></head><body>
<h1>${ok ? "Test alert sent" : "Test alert not sent"}</h1>
<p>${ok ? "Check Telegram: the message should be there now." : `Reason: ${esc(result)}`}</p>
<p><a href="/health">Back to the dashboard</a></p></body></html>`;
}

/** Is this request for the admin host? (Only routes it here; the JWT is what lets it in.) */
export function isAdminHost(req: Request, env: Env): boolean {
  const want = (env.ADMIN_HOST || ADMIN_HOST_DEFAULT).toLowerCase();
  return new URL(req.url).hostname.toLowerCase() === want;
}

const USER_PATH = /^\/users\/([^/]{1,200})(?:\/(give-pass|add-interviews|suspend|unsuspend|reveal))?$/;
/** The account id in a /users/<id> path, or "" when the escape is malformed (then nothing matches). */
const idOf = (raw: string) => { try { return decodeURIComponent(raw); } catch { return ""; } };

export async function handleAdmin(req: Request, env: Env, nowMs: number): Promise<Response> {
  const who = await verifyAccess(req, env, Math.floor(nowMs / 1000));
  if (!who) return text("Forbidden", 403);
  const url = new URL(req.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const now = Math.floor(nowMs / 1000);
  const admin = who.email;

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
          return userResponse(env, id, now, admin, { revealed: true });
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
  const base = { viewer: admin, generatedAt: now, flash: flashOf(url) };
  if (path === "/" || path === "/admin") {
    const today = dayOf(nowMs);
    const todayStart = Math.floor(Date.parse(`${today}T00:00:00Z`) / 1000);
    const [stats, pool, rev, fd, runs, activity, suspended, prevDays, sp, pDays] = await Promise.all([
      collectStats(env, nowMs), poolSetting(env), revenue(env, nowMs), feed(nowMs), githubRuns(env, nowMs),
      recentActivity(env.DB, 6), suspendedCount(env.DB), accountsPerDay(env.DB, todayStart - 29 * 86_400, 30),
      spend(env.DB, periodOf(nowMs), now, 6), poolDays(env.DB, todayStart)]);
    const att = attention({ stats, pool, revenue: rev, feed: fd, runs, env, now, suspended });
    const { body, rail } = overviewPage({ stats, pool, revenue: rev, attention: att, activity, prevDays, spend: sp, poolDays: pDays, now });
    return html(shell({ ...base, page: "overview", title: "Overview", body, rail, badges: badgesFor(att) }));
  }
  if (path === "/users") {
    const filter = (["all", "free", "pass", "email", "suspended"] as const).find((f) => f === url.searchParams.get("filter")) ?? "all";
    const sort = (["newest", "oldest", "tokens"] as const).find((s) => s === url.searchParams.get("sort")) ?? "newest";
    const page = Math.max(1, Math.min(10_000, Number.parseInt(url.searchParams.get("page") ?? "1", 10) || 1));
    const q = (url.searchParams.get("q") ?? "").slice(0, 120);
    const { rows, total } = await listUsers(env.DB, { q, filter, sort, page, period: periodOf(nowMs), now });
    return html(shell({ ...base, page: "users", title: "Users", query: q, body: usersPage({ rows, total, q, filter, sort, page, now }) }));
  }
  const um = USER_PATH.exec(path);
  if (um && !um[2]) return userResponse(env, idOf(um[1]!), now, admin, { flash: base.flash });
  if (path === "/revenue") {
    const [rev, granted30] = await Promise.all([revenue(env, nowMs), grantedSince(env.DB, now - 30 * 86_400)]);
    return html(shell({ ...base, page: "revenue", title: "Revenue", body: revenuePage({ revenue: rev, granted30, now }) }));
  }
  if (path === "/usage") {
    const today = dayOf(nowMs);
    const todayStart = Math.floor(Date.parse(`${today}T00:00:00Z`) / 1000);
    const [stats, pool, days, sp, months, networks, domains] = await Promise.all([
      collectStats(env, nowMs), poolSetting(env), poolDays(env.DB, todayStart), spend(env.DB, periodOf(nowMs), now),
      monthly(env.DB, nowMs), signupNetworks(env.DB, today, env), emailDomains(env.DB)]);
    return html(shell({ ...base, page: "usage", title: "Usage & costs",
      body: usagePage({ stats, pool, poolDays: days, spend: sp, months, networks, domains, now }) }));
  }
  if (path === "/health") {
    const [stats, fd, runs, rev] = await Promise.all([collectStats(env, nowMs), feed(nowMs), githubRuns(env, nowMs), revenue(env, nowMs)]);
    return html(shell({ ...base, page: "health", title: "Health", body: healthPage({ stats, feed: fd, runs, revenue: rev, env, now }) }));
  }
  if (path === "/audit") {
    const before = Number.parseInt(url.searchParams.get("before") ?? "", 10);
    const rows = await auditLog(env.DB, { limit: 51, before: Number.isFinite(before) && before > 0 ? before : undefined });
    return html(shell({ ...base, page: "audit", title: "Audit log", body: auditPage({ rows: rows.slice(0, 50), more: rows.length > 50 }) }));
  }
  return html(shell({ ...base, page: "overview", title: "Not found", body: notFound() }), 404);
}

async function userResponse(env: Env, id: string, now: number, admin: string,
                            o: { revealed?: boolean; flash?: Shell["flash"] }): Promise<Response> {
  const [u, history] = await Promise.all([userDetail(env.DB, id, now), auditLog(env.DB, { target: id, limit: 20 })]);
  const base = { viewer: admin, generatedAt: now, flash: o.flash ?? null };
  if (!u) return html(shell({ ...base, page: "users", title: "Not found", crumbs: [{ label: "Users", href: "/users" }], body: notFound() }), 404);
  return html(shell({ ...base, page: "users", title: u.email ? (o.revealed ? u.email : maskEmail(u.email)) : "Anonymous account",
    crumbs: [{ label: "Users", href: "/users" }], body: userPage({ u, history, revealed: Boolean(o.revealed), now }) }));
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
