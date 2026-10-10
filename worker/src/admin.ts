// The founder's private, READ-ONLY admin dashboard at https://admin.sponsorjobs.ai.
//
// Two routes on the admin host only: GET / (one server-rendered HTML page) and GET /admin/api/stats
// (the same numbers as JSON). Every request is checked by src/access.ts first; nothing here writes
// to the database. Output never contains a token, a token hash, a code hash or a salt: the queries
// below select only the columns they show.

import { verifyAccess } from "./access";
import { type Env, intVar } from "./env";
import { dayOf, FREE_MODEL, INTERVIEW_SECONDS, PASS_MODEL, PASSES, periodOf } from "./metering";
import { FREE_POOL_USER } from "./store";

export const ADMIN_HOST_DEFAULT = "admin.sponsorjobs.ai";

// ---- cost assumptions (estimates; the real bills are in the Anthropic and Tavus consoles) ------ //
//
// Anthropic list prices, $ per 1M tokens (platform.claude.com/docs/en/about-claude/pricing,
// checked 2026-10-10): claude-haiku-4-5 $1 in / $5 out, claude-sonnet-5-5 $2 in / $10 out.
// The broker stores only input+output together, so a blend is assumed: tailoring prompts are
// long and replies short, so 80% of tokens are taken as input. Prompt caching would lower this.
export const RATES = {
  free: { model: FREE_MODEL, inPerM: 1, outPerM: 5 },
  pass: { model: PASS_MODEL, inPerM: 2, outPerM: 10 },
} as const;
export const INPUT_SHARE = 0.8;
// Tavus conversational video, $ per minute of live interview (tavus.io/pricing, overage rate).
export const TAVUS_PER_MINUTE = 0.35;
// Resend free plan (resend.com/pricing): 100 e-mails a day, 3,000 a month. Pro is $20/month with no
// daily cap. The dashboard turns amber at 70 a day and red at 90.
export const RESEND_DAILY_CAP = 100;
export const RESEND_MONTHLY_CAP = 3000;
export const RESEND_AMBER = 70;
export const RESEND_RED = 90;
export const RESEND_ADVICE = "Resend free plan: 100 a day. Upgrade to Pro ($20/month, no daily cap) before this fills.";

const blendedPerToken = (r: { inPerM: number; outPerM: number }) =>
  (INPUT_SHARE * r.inPerM + (1 - INPUT_SHARE) * r.outPerM) / 1_000_000;

const round2 = (n: number) => Math.round(n * 100) / 100;

export interface RecentSignIn {
  email: string;
  account_id: string;
  created_at: number | null;   // unix seconds; null = before created_at was recorded
  email_at: number | null;     // unix seconds the e-mail was attached; null = before it was recorded
  plan: string;                // "free" or the active pass name
}

export interface Stats {
  generated_at: number;
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

  const cap = intVar(env.FREE_POOL_DAILY_TOKENS, 5_000_000);
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

  return {
    generated_at: now,
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

// ---- HTML -------------------------------------------------------------------------------------- //

const esc = (v: unknown): string => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
const num = (n: number) => n.toLocaleString("en-US");
const usd = (n: number) => `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const date = (s: number | null) => (s ? new Date(s * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC" : "unknown");
const PASS_LABEL: Record<string, string> = { free: "Free", pass30: "Job Hunt Pass", pass90: "Season Pass" };

function tile(label: string, value: string, sub = ""): string {
  return `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div>` +
         (sub ? `<div class="sub">${esc(sub)}</div>` : "") + `</div>`;
}

/** 30 daily bars, one series, inline SVG. Each bar carries a <title> as its hover tooltip, and a
 * table of the same numbers sits under the chart for screen readers. */
function barChart(days: Array<{ day: string; count: number }>): string {
  const W = 600, H = 160, top = 12, bottom = 22, left = 28;
  const max = Math.max(1, ...days.map((d) => d.count));
  const plotH = H - top - bottom;
  const slot = (W - left) / days.length;
  const bw = Math.max(2, slot - 2);                     // 2px gap between bars
  const y = (v: number) => top + plotH - (v / max) * plotH;
  const bars = days.map((d, i) => {
    const x = left + i * slot + 1;
    const h = Math.max(d.count > 0 ? 2 : 0, (d.count / max) * plotH);
    const r = Math.min(4, bw / 2, h);
    // Rounded top, square baseline.
    const path = h <= 0 ? "" :
      `<path class="bar" d="M${x},${top + plotH} V${top + plotH - h + r} Q${x},${top + plotH - h} ${x + r},${top + plotH - h} ` +
      `H${x + bw - r} Q${x + bw},${top + plotH - h} ${x + bw},${top + plotH - h + r} V${top + plotH} Z"/>`;
    return `<g><title>${esc(d.day)}: ${d.count} new</title>` +
           `<rect class="hit" x="${x - 1}" y="${top}" width="${slot}" height="${plotH}"/>${path}</g>`;
  }).join("");
  const label = (i: number, anchor: "start" | "middle" | "end") => {
    const x = anchor === "start" ? left + i * slot + 1 : anchor === "end" ? left + (i + 1) * slot - 1 : left + i * slot + slot / 2;
    return `<text class="axis" x="${x}" y="${H - 6}" text-anchor="${anchor}">${esc(days[i]!.day.slice(5))}</text>`;
  };
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="New accounts per day, last 30 days">` +
    `<line class="grid" x1="${left}" x2="${W}" y1="${y(max)}" y2="${y(max)}"/>` +
    `<line class="base" x1="${left}" x2="${W}" y1="${top + plotH}" y2="${top + plotH}"/>` +
    `<text class="axis" x="${left - 6}" y="${y(max) + 4}" text-anchor="end">${max}</text>` +
    `<text class="axis" x="${left - 6}" y="${top + plotH + 4}" text-anchor="end">0</text>` +
    bars + label(0, "start") + label(14, "middle") + label(days.length - 1, "end") + `</svg>`;
}

export function renderPage(s: Stats, viewer: string): string {
  const a = s.accounts, p = s.plans, m = s.usage_month, f = s.free_pool_today, e = s.sign_in_emails;
  const ePct = Math.min(100, (100 * e.today) / e.daily_cap);
  const everList = Object.entries(p.accounts_ever_on_pass).map(([k, v]) => `${PASS_LABEL[k] ?? k} ${num(v)}`).join(", ") || "none";
  const rows = s.recent_sign_ins.map((r) => `<tr><td>${esc(r.email)}</td><td>${esc(PASS_LABEL[r.plan] ?? r.plan)}</td>` +
    `<td>${esc(date(r.email_at))}</td><td>${esc(date(r.created_at))}</td></tr>`).join("") ||
    `<tr><td colspan="4" class="muted">No one has signed in with an e-mail yet.</td></tr>`;
  const dayRows = a.new_per_day.map((d) => `<tr><td>${esc(d.day)}</td><td>${d.count}</td></tr>`).join("");
  const r = s.assumptions.rates_per_million;
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>SponsorJobs Admin</title>
<style>
:root{--bg:#f6f6f5;--card:#fff;--ink:#1d1d1f;--ink2:#55555a;--muted:#8a8a8f;--line:#e4e4e2;--bar:#6e6e73;--bar-hover:#1d1d1f;--warn:#a15c00;--bad:#c0362c;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#141415;--card:#1d1d1f;--ink:#f2f2f2;--ink2:#b4b4b8;--muted:#86868b;--line:#2e2e31;--bar:#a1a1a6;--bar-hover:#f2f2f2;--warn:#e0a040;--bad:#ff6b5e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:8px}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:28px 0 10px;color:var(--ink2);font-weight:600}
.muted,.sub{color:var(--muted);font-size:13px}
.grid4{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile,.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.label{font-size:13px;color:var(--ink2)}.value{font-size:24px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.chart{width:100%;height:auto;display:block}.chart .bar{fill:var(--bar)}.chart g:hover .bar{fill:var(--bar-hover)}
.chart .hit{fill:transparent}.chart .grid{stroke:var(--line);stroke-dasharray:3 3}.chart .base{stroke:var(--line)}
.chart .axis{fill:var(--muted);font-size:11px}
.meter{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin-top:8px}.meter span{display:block;height:100%;background:var(--bar)}
.meter.hot span,.amber .meter span{background:var(--warn)}.red .meter span{background:var(--bad)}
.amber .flag{color:var(--warn)}.red .flag{color:var(--bad)}.amber{border-color:var(--warn)}.red{border-color:var(--bad)}
.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--ink2);font-weight:600}
td:first-child{white-space:normal;overflow-wrap:anywhere;min-width:14ch}
details{margin-top:8px}summary{cursor:pointer;color:var(--ink2);font-size:13px}
</style></head><body><main>
<header><h1>SponsorJobs admin</h1><span class="muted">Signed in as ${esc(viewer)} · ${esc(date(s.generated_at))} · read-only</span></header>

<h2>Accounts</h2>
<div class="grid4">
${tile("Total accounts", num(a.total), a.created_unknown ? `${num(a.created_unknown)} from before sign-up dates were recorded` : "")}
${tile("Signed in with e-mail", num(a.with_email), a.total ? `${Math.round((100 * a.with_email) / a.total)}% of accounts` : "")}
${tile("New this week", num(a.new_this_week), "last 7 days")}
</div>
<div class="card" style="margin-top:10px"><div class="label">New accounts per day, last 30 days (UTC)</div>
${barChart(a.new_per_day)}
<details><summary>Show as a table</summary><div class="tablewrap"><table><tr><th>Day</th><th>New</th></tr>${dayRows}</table></div></details>
</div>

<h2>Plans</h2>
<div class="grid4">
${tile("Free", num(p.free))}
${tile("Job Hunt Pass (active)", num(p.active_pass30))}
${tile("Season Pass (active)", num(p.active_pass90))}
${tile("Purchases", num(p.checkout_purchases), `passes and interview packs together; ever on a pass: ${everList}`)}
${tile("Extra-interview credits", num(Math.floor(p.extra_interview_credits.interviews)), `interviews unused (${num(Math.round(p.extra_interview_credits.seconds / 60))} min)`)}
</div>

<h2>This month (${esc(m.period)})</h2>
<div class="grid4">
${tile("Active accounts", num(m.active_accounts))}
${tile("Tailored packages", num(m.packages.total), `free ${num(m.packages.free)} · pass ${num(m.packages.pass)}`)}
${tile("AI tokens", num(m.ai_tokens.total), `free ${num(m.ai_tokens.free)} · pass ${num(m.ai_tokens.pass)}`)}
${tile("AI cost (estimate)", usd(m.ai_cost_usd.total), `free ${usd(m.ai_cost_usd.free)} · pass ${usd(m.ai_cost_usd.pass)}`)}
${tile("Live interviews", num(m.interviews.count), `${num(Math.round(m.interviews.minutes))} min used`)}
${tile("Interview cost (estimate)", usd(m.interviews.cost_usd), `at ${usd(s.assumptions.tavus_per_minute)}/min (Tavus)`)}
</div>

<h2>Free AI pool today (${esc(f.day)})</h2>
<div class="card"><div class="label">${num(f.tokens)} of ${num(f.cap)} tokens (${f.percent}%)</div>
<div class="meter${f.percent >= 80 ? " hot" : ""}"><span style="width:${Math.min(100, f.percent)}%"></span></div>
<div class="sub" style="margin-top:6px">Past the cap, free requests get "busy, try tomorrow"; pass holders are unaffected.</div></div>

<h2>Sign-in code e-mails (${esc(e.day)})</h2>
<div class="card ${e.level}"><div class="label">${num(e.today)} of ${num(e.daily_cap)} today · ${num(e.month)} of ${num(e.monthly_cap)} this month</div>
<div class="meter"><span style="width:${ePct}%"></span></div>
<div class="sub" style="margin-top:6px">${e.level === "ok" ? "" : `<strong class="flag">${e.level === "red" ? "Nearly full." : "Filling up."}</strong> `}${esc(e.advice)}</div></div>

<h2>Recent sign-ins (last 20 with an e-mail)</h2>
<div class="card tablewrap"><table><tr><th>E-mail</th><th>Plan</th><th>Signed in</th><th>Account created</th></tr>${rows}</table></div>

<p class="muted" style="margin-top:24px">Cost estimates: ${esc(r.free.model)} $${r.free.inPerM}/$${r.free.outPerM} per 1M tokens (free plan),
${esc(r.pass.model)} $${r.pass.inPerM}/$${r.pass.outPerM} (passes), ${Math.round(s.assumptions.input_share * 100)}% of tokens assumed input.
${esc(s.assumptions.note)} JSON: <a href="/admin/api/stats" style="color:inherit">/admin/api/stats</a></p>
</main></body></html>`;
}

// ---- routing ----------------------------------------------------------------------------------- //

const security = {
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "referrer-policy": "no-referrer",
  "x-robots-tag": "noindex, nofollow",
};

/** Is this request for the admin host? (Only routes it here; the JWT is what lets it in.) */
export function isAdminHost(req: Request, env: Env): boolean {
  const want = (env.ADMIN_HOST || ADMIN_HOST_DEFAULT).toLowerCase();
  return new URL(req.url).hostname.toLowerCase() === want;
}

export async function handleAdmin(req: Request, env: Env, nowMs: number): Promise<Response> {
  const who = await verifyAccess(req, env, Math.floor(nowMs / 1000));
  if (!who) {
    return new Response("Forbidden", { status: 403, headers: { "content-type": "text/plain", ...security } });
  }
  if (req.method !== "GET" && req.method !== "HEAD") {
    return new Response("Method not allowed", { status: 405, headers: { "content-type": "text/plain", ...security } });
  }
  const path = new URL(req.url).pathname;
  if (path === "/admin/api/stats") {
    return new Response(JSON.stringify(await collectStats(env, nowMs)),
      { headers: { "content-type": "application/json", ...security } });
  }
  if (path === "/" || path === "/admin" || path === "/admin/") {
    return new Response(renderPage(await collectStats(env, nowMs), who.email), {
      headers: {
        "content-type": "text/html; charset=utf-8", ...security,
        "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
      },
    });
  }
  return new Response("Not found", { status: 404, headers: { "content-type": "text/plain", ...security } });
}
