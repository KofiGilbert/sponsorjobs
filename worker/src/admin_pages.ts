// The admin dashboard's pages. Each takes already-loaded data and returns the page body (and, on
// the overview, the right rail); admin.ts loads the data and wraps the body in the shell.

import type { Stats } from "./admin";
import {
  ACTION_LABEL, type Activity, ACTIVITY_ICON, type AuditRow, type Feed, FEED_STALE_HOURS, type PoolSetting, type Revenue, type Runs, type Spender,
  type UserDetail, type UserFilter, type UserRow, type UserSort, USERS_PER_PAGE, netSince, TAVUS_PER_MINUTE,
} from "./admin_data";
import { MAX_INTERVIEWS } from "./admin_actions";
import type { Env } from "./env";
import { INTERVIEW_SECONDS, PLANS } from "./metering";
import {
  ago, badge, barChart, card, date, day, donut, esc, facts, hbars, icon, kpi, lineChart, maskEmail, meter, num,
  PASS_LABEL, shortNum, table, usd,
} from "./admin_ui";

const userHref = (id: string) => `/users/${encodeURIComponent(id)}`;
const planBadge = (plan: string) => badge(PASS_LABEL[plan] ?? plan, plan === "free" ? "plain" : "ok");
const daysLeft = (until: number, now: number) => {
  const d = Math.ceil((until - now) / 86_400);
  return d <= 1 ? "ends within a day" : `${d} days left`;
};
/** "pass30" -> "Job Hunt Pass", "pack_3" -> "3-interview pack". */
const productLabel = (k: string) => PASS_LABEL[k] ?? (k.startsWith("pack_") ? `${k.slice(5)}-interview pack` : "Other amount");
const title = (h: string, sub = "") => `<div class="ptitle"><div><h1>${esc(h)}</h1>${sub ? `<p>${esc(sub)}</p>` : ""}</div></div>`;

// ---- what needs attention ----------------------------------------------------------------------- //

export interface Attention { level: "bad" | "warn" | "ok"; text: string; sub?: string; href?: string }

export function attention(o: { stats: Stats; pool: PoolSetting; revenue: Revenue; feed: Feed; runs: Runs; env: Env;
                               now: number; suspended: number }): Attention[] {
  const out: Attention[] = [];
  const { stats: s, now } = o;
  if (s.ai.state === "company_outage") out.push({ level: "bad", text: "The company AI key is failing", sub: s.ai.note || s.ai.summary, href: "/health" });
  else if (s.ai.state === "transient") out.push({ level: "warn", text: "The last AI call was rate limited", sub: date(s.ai.since), href: "/health" });
  if (!o.feed.ok) out.push({ level: "bad", text: "The job feed could not be read", sub: o.feed.why, href: "/health" });
  else if (o.feed.generated_at && now - o.feed.generated_at > FEED_STALE_HOURS * 3600) {
    out.push({ level: "warn", text: "The job feed is out of date", sub: `Built ${ago(o.feed.generated_at, now)}`, href: "/health" });
  }
  for (const r of o.runs.latest) {
    if (r.conclusion === "failure" && (r.workflow === "feed" || r.workflow === "grow-boards" || r.workflow === "tests")) {
      out.push({ level: "bad", text: `GitHub "${r.workflow}" failed`, sub: ago(r.at, now), href: "/health" });
    }
  }
  if (o.pool.cap === 0) out.push({ level: "warn", text: "The free AI is paused", sub: "Free users see \"busy, try tomorrow\"", href: "/usage" });
  else if (s.free_pool_today.tokens >= o.pool.cap) out.push({ level: "bad", text: "The free AI limit is used up today", href: "/usage" });
  else if (s.free_pool_today.tokens >= 0.75 * o.pool.cap) out.push({ level: "warn", text: "The free AI limit is 75% used", href: "/usage" });
  if (s.sign_in_emails.level !== "ok") {
    out.push({ level: s.sign_in_emails.level === "red" ? "bad" : "warn", text: "Sign-in e-mails are near Resend's free limit",
               sub: `${s.sign_in_emails.today} of ${s.sign_in_emails.daily_cap} today`, href: "/health" });
  }
  const disputes = o.revenue.payments.filter((p) => p.disputed).length;
  if (disputes) out.push({ level: "bad", text: `${disputes} payment${disputes === 1 ? " is" : "s are"} disputed`, sub: "Answer it in Stripe", href: "/revenue" });
  if (!o.revenue.ok && o.revenue.mode !== "off") out.push({ level: "warn", text: "Stripe could not be read", sub: o.revenue.why, href: "/revenue" });
  if (o.revenue.mode === "test") out.push({ level: "warn", text: "Stripe is in test mode", sub: "No real payments are taken yet", href: "/revenue" });
  if (!o.env.OWNER_TELEGRAM_CHAT_ID) out.push({ level: "warn", text: "Outage alerts have no Telegram chat", sub: "Set OWNER_TELEGRAM_CHAT_ID", href: "/health" });
  if (o.suspended) out.push({ level: "ok", text: `${o.suspended} account${o.suspended === 1 ? " is" : "s are"} suspended`, href: "/users?filter=suspended" });
  return out;
}

function attentionList(items: Attention[]): string {
  if (!items.some((i) => i.level !== "ok")) items = [{ level: "ok", text: "All clear", sub: "Nothing needs you right now." }, ...items];
  return `<ul class="attn">${items.map((i) => `<li><span class="ib ${i.level}">${icon(i.level === "ok" ? "ok" : "alert")}</span>` +
    `<div><div class="t">${i.href ? `<a href="${i.href}">${esc(i.text)}</a>` : esc(i.text)}</div>` +
    (i.sub ? `<div class="s">${esc(i.sub)}</div>` : "") + `</div></li>`).join("")}</ul>`;
}

// ---- Overview ----------------------------------------------------------------------------------- //

export function overviewPage(o: { stats: Stats; pool: PoolSetting; revenue: Revenue; attention: Attention[];
                                   activity: Activity[]; now: number }): { body: string; rail: string } {
  const { stats: s, now } = o;
  const net30 = netSince(o.revenue, now, 30);
  const prev30 = o.revenue.payments.filter((p) => p.created >= now - 60 * 86_400 && p.created < now - 30 * 86_400)
    .reduce((a, p) => a + p.amount - p.refunded, 0) / 100;
  const activePasses = s.plans.active_pass30 + s.plans.active_pass90;
  const everPaid = Object.values(s.plans.accounts_ever_on_pass).reduce((a, b) => a + b, 0);
  const conversion = s.accounts.total ? (100 * everPaid) / s.accounts.total : 0;
  const m = s.usage_month;
  const revMonth = o.revenue.payments.filter((p) => new Date(p.created * 1000).toISOString().slice(0, 7) === m.period)
    .reduce((a, p) => a + p.amount - p.refunded, 0) / 100;
  const costMonth = m.ai_cost_usd.total + m.interviews.cost_usd;
  const poolPct = o.pool.cap > 0 ? (100 * s.free_pool_today.tokens) / o.pool.cap : 100;

  const body = title("Overview", "How SponsorJobs is doing today.") +
    `<div class="kpis">` +
    kpi("Revenue, 30 days", o.revenue.ok ? usd(net30) : "n/a", { tone: "a", delta: o.revenue.ok && prev30 > 0 ? (100 * (net30 - prev30)) / prev30 : null,
        sub: o.revenue.ok ? `after refunds${o.revenue.mode === "test" ? ", Stripe test mode" : ""}` : "Stripe unavailable" }) +
    kpi("Active passes", num(activePasses), { tone: "b", sub: `${num(s.plans.active_pass30)} Job Hunt, ${num(s.plans.active_pass90)} Season` }) +
    kpi("Active this month", num(m.active_accounts), { tone: "a", sub: `of ${num(s.accounts.total)} accounts` }) +
    kpi("New this week", num(s.accounts.new_this_week), { tone: "b", sub: `${conversion.toFixed(1)}% ever bought a pass` }) +
    `</div>` +
    `<div class="grid2">` +
    card("New accounts per day", barChart(s.accounts.new_per_day.map((d) => ({ label: d.day, value: d.count })),
      { aria: "New accounts per day, last 30 days", unit: "new" }) +
      `<details><summary>Show as a table</summary>${table(["Day", "New"], s.accounts.new_per_day.slice().reverse().map((d) => [esc(d.day), num(d.count)]), "")}</details>`,
      { aside: `<span class="muted small">last 30 days, UTC</span>` }) +
    card("Plans", donut([
      { label: "Free", value: s.plans.free }, { label: "Job Hunt Pass", value: s.plans.active_pass30 },
      { label: "Season Pass", value: s.plans.active_pass90 }], { aria: "Accounts by plan", center: num(s.accounts.total) })) +
    `</div>` +
    `<div class="grid2e">` +
    card(`This month (${m.period})`, facts([
      ["Revenue", o.revenue.ok ? usd(revMonth) : "n/a"],
      ["AI cost (estimate)", `${usd(m.ai_cost_usd.total)} <span class="muted small">free ${usd(m.ai_cost_usd.free)}, pass ${usd(m.ai_cost_usd.pass)}</span>`],
      ["Interview cost (estimate)", `${usd(m.interviews.cost_usd)} <span class="muted small">${num(m.interviews.minutes)} min</span>`],
      ["Margin (estimate)", o.revenue.ok ? `<strong>${usd(revMonth - costMonth)}</strong>` : "n/a"],
      ["Tailored packages", `${num(m.packages.total)} <span class="muted small">free ${num(m.packages.free)}, pass ${num(m.packages.pass)}</span>`],
    ]), { aside: `<a class="muted small" href="/usage">Usage &amp; costs</a>` }) +
    card("Free AI today", `<div class="kvalue">${shortNum(s.free_pool_today.tokens)} <span class="muted small">of ${o.pool.cap ? shortNum(o.pool.cap) : "0 (paused)"} tokens</span></div>` +
      meter(poolPct, poolPct >= 100 ? "bad" : poolPct >= 75 ? "warn" : "ok") +
      `<p class="muted small">Shared by every free account. Past the limit, free requests get "busy, try tomorrow"; pass holders are not affected.</p>` +
      `<p class="small"><a href="/usage#free-ai">Change the limit or pause it</a></p>`) +
    `</div>` +
    card("Recent e-mail sign-ins", table(["E-mail", "Plan", "Signed in", "Account created"],
      s.recent_sign_ins.map((r) => [`<a href="${userHref(r.account_id)}">${esc(maskEmail(r.email))}</a>`, planBadge(r.plan),
        esc(date(r.email_at)), esc(date(r.created_at))]), "No one has signed in with an e-mail yet."),
      { aside: `<a class="muted small" href="/users?filter=email">All</a>` });

  const rail = `<section><h2>Needs attention</h2>${attentionList(o.attention)}</section>` +
    `<section><h2>Recent activity</h2>${o.activity.length ? `<ul class="attn">${o.activity.map((a) =>
      `<li><span class="ib plain">${icon(ACTIVITY_ICON[a.kind])}</span><div><div class="t">${a.href ? `<a href="${a.href}">${esc(a.text)}</a>` : esc(a.text)}</div>` +
      `<div class="s">${esc(ago(a.at, now))}</div></div></li>`).join("")}</ul>` : `<p class="muted">Nothing yet.</p>`}</section>`;
  return { body, rail };
}

// ---- Users -------------------------------------------------------------------------------------- //

export function usersPage(o: { rows: UserRow[]; total: number; q: string; filter: UserFilter; sort: UserSort; page: number; now: number }): string {
  const link = (p: Partial<{ q: string; filter: string; sort: string; page: number }>) => {
    const u = new URLSearchParams();
    const q = p.q ?? o.q, f = p.filter ?? o.filter, s = p.sort ?? o.sort, pg = p.page ?? 1;
    if (q) u.set("q", q);
    if (f !== "all") u.set("filter", f);
    if (s !== "newest") u.set("sort", s);
    if (pg > 1) u.set("page", String(pg));
    const qs = u.toString();
    return `/users${qs ? `?${qs}` : ""}`;
  };
  const chip = (label: string, on: boolean, href: string) => `<a class="chip${on ? " on" : ""}" href="${esc(href)}"${on ? ' aria-current="true"' : ""}>${esc(label)}</a>`;
  const filters = `<div class="filters">` +
    ([["all", "All"], ["free", "Free"], ["pass", "On a pass"], ["email", "E-mail sign-in"], ["suspended", "Suspended"]] as const)
      .map(([k, l]) => chip(l, o.filter === k, link({ filter: k }))).join("") +
    `<span class="muted small" style="margin-left:8px">Sort</span>` +
    ([["newest", "Newest"], ["oldest", "Oldest"], ["tokens", "AI use this month"]] as const)
      .map(([k, l]) => chip(l, o.sort === k, link({ sort: k }))).join("") + `</div>`;
  const rows = o.rows.map((r) => [
    `<a href="${userHref(r.account_id)}">${r.email ? esc(maskEmail(r.email)) : `<span class="mono">${esc(r.account_id.slice(0, 14))}</span>`}</a>`,
    planBadge(r.plan) + (r.suspended_at ? " " + badge("Suspended", "bad") : ""),
    r.plan === "free" ? `<span class="muted">none</span>` : esc(day(r.pass_until)),
    num(r.tokens_month), num(r.packages_month),
    r.credit_seconds > 0 ? num(r.credit_seconds / INTERVIEW_SECONDS) : `<span class="muted">0</span>`,
    esc(r.created_at ? day(r.created_at) : "unknown"),
  ]);
  const pages = Math.max(1, Math.ceil(o.total / USERS_PER_PAGE));
  const pager = pages <= 1 ? "" : `<div class="pager"><span class="muted small">Page ${o.page} of ${pages}</span>` +
    (o.page > 1 ? `<a class="btn" href="${esc(link({ page: o.page - 1 }))}">Previous</a>` : "") +
    (o.page < pages ? `<a class="btn" href="${esc(link({ page: o.page + 1 }))}">Next</a>` : "") + `</div>`;
  return title("Users", `${num(o.total)} account${o.total === 1 ? "" : "s"}${o.q ? ` matching "${o.q}"` : ""}. E-mails are masked here; open a person to see more.`) +
    card("Accounts", filters + `<div style="height:12px"></div>` +
      table(["Person", "Plan", "Pass ends", "AI tokens (month)", "Packages (month)", "Extra interviews", "Created"], rows,
        o.q ? "No account matches that search." : "No accounts here.") + pager);
}

// ---- one person --------------------------------------------------------------------------------- //

export function userPage(o: { u: UserDetail; history: AuditRow[]; revealed: boolean; now: number }): string {
  const { u, now } = o;
  const plan = PLANS[u.plan];
  const head = `<div class="ptitle"><div><h1>${u.email ? esc(o.revealed ? u.email : maskEmail(u.email)) : "Anonymous account"}</h1>` +
    `<p>${planBadge(u.plan)} ${u.suspended_at ? badge(`Suspended ${day(u.suspended_at)}`, "bad") : badge("Active", "ok")}</p></div>` +
    (u.email && !o.revealed ? `<form method="post" action="${userHref(u.account_id)}/reveal"><button class="btn" type="submit">Show full e-mail</button></form>` : "") + `</div>`;
  const account = card("Account", facts([
    ["Account id", `<span class="mono">${esc(u.account_id)}</span>`],
    ["E-mail", u.email ? esc(o.revealed ? u.email : maskEmail(u.email)) + (o.revealed ? ` <span class="muted small">(this view is in the audit log)</span>` : "") : `<span class="muted">none (anonymous)</span>`],
    ["Created", esc(date(u.created_at))],
    ["E-mail added", esc(date(u.email_at))],
    ["Plan", `${esc(PASS_LABEL[u.plan] ?? u.plan)}${u.plan !== "free" ? ` until ${esc(date(u.pass_until))} (${esc(daysLeft(u.pass_until, now))})` : u.stored_plan && u.stored_plan !== "free" ? ` <span class="muted small">(${esc(PASS_LABEL[u.stored_plan] ?? u.stored_plan)} ended ${esc(day(u.pass_until))})</span>` : ""}`],
    ["Left on the pass", u.plan !== "free" ? `${num(u.pass_packages)} packages, ${num(u.pass_seconds / INTERVIEW_SECONDS)} live interviews` : `<span class="muted">n/a</span>`],
    ["Extra interviews", `${num(u.credit_seconds / INTERVIEW_SECONDS)} <span class="muted small">(${num(u.credit_seconds / 60)} min, never expire)</span>`],
    ["Signed-in devices", num(u.sessions)],
    ["Telegram", u.telegram.linked ? `linked${u.telegram.paused ? ", paused" : ""}${u.telegram.linked_at ? ` <span class="muted small">since ${esc(u.telegram.linked_at.slice(0, 10))}</span>` : ""}` : `<span class="muted">not linked</span>`],
    ["Application e-mail", u.alias ? `<span class="mono">${esc(o.revealed ? u.alias.address : maskEmail(u.alias.address))}</span> <span class="muted small">${num(u.alias.mails_30d)} mail${u.alias.mails_30d === 1 ? "" : "s"} in 30 days</span>` : `<span class="muted">none</span>`],
    ["Monthly AI limit", plan?.llmTokenCap ? `${num(plan.llmTokenCap)} tokens` : "n/a"],
  ]));
  const reasonField = `<input type="text" name="reason" required minlength="3" maxlength="300" placeholder="Reason (kept in the audit log)" aria-label="Reason">`;
  const id = userHref(u.account_id);
  const actions = card("Support actions",
    `<form class="act" method="post" action="${id}/give-pass"><h3>Give a pass</h3><div class="row">` +
      `<select name="pass" aria-label="Pass"><option value="pass30">Job Hunt Pass (30 days)</option><option value="pass90">Season Pass (90 days)</option></select>` +
      `${reasonField}<button class="btn primary" type="submit">Give pass</button></div>` +
      `<span class="muted small">Starts now, or adds to a pass they already have. No charge.</span></form>` +
    `<form class="act" method="post" action="${id}/add-interviews"><h3>Add live interviews</h3><div class="row">` +
      `<select name="interviews" aria-label="Interviews">${Array.from({ length: MAX_INTERVIEWS }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join("")}</select>` +
      `${reasonField}<button class="btn" type="submit">Add</button></div>` +
      `<span class="muted small">20 minutes each; they never expire. Each costs us about ${usd(20 * TAVUS_PER_MINUTE)} on Tavus.</span></form>` +
    (u.suspended_at
      ? `<form class="act" method="post" action="${id}/unsuspend"><h3>Restore this account</h3><div class="row">${reasonField}` +
        `<button class="btn" type="submit">Restore</button></div><span class="muted small">Their devices sign straight back in.</span></form>`
      : `<form class="act" method="post" action="${id}/suspend"><h3>Suspend this account</h3><div class="row">${reasonField}` +
        `<input type="text" name="confirm" required placeholder="Type SUSPEND" aria-label="Type SUSPEND to confirm" style="flex:0 1 140px">` +
        `<button class="btn danger" type="submit">Suspend</button></div>` +
        `<span class="muted small">Signs them out everywhere and stops their AI use until you restore it. Nothing is deleted.</span></form>`) +
    `<p class="muted small">Refunds happen in Stripe, never here.</p>`);
  const months = card("Usage by month", table(["Month", "AI tokens", "Free packages", "Pass packages"],
    u.months.map((r) => [esc(r.period), num(r.tokens), num(r.packages), num(r.pass_packages)]), "No usage recorded."));
  const interviews = card("Live interviews", table(["Started", "Used", "Ended"],
    u.interviews.map((r) => [esc(date(r.started)), `${num(r.used / 60)} min`, r.ended ? esc(date(r.ended)) : `<span class="muted">open</span>`]),
    "No live interviews."));
  const history = card("Admin history", auditTable(o.history, false), { aside: `<a class="muted small" href="/audit">Audit log</a>` });
  return head + `<div class="grid2e">${account}${actions}</div><div class="grid2e">${months}${interviews}</div>${history}`;
}

// ---- Revenue ------------------------------------------------------------------------------------ //

export function revenuePage(o: { revenue: Revenue; granted30: number; now: number }): string {
  const r = o.revenue, now = o.now;
  const mode = r.mode === "live" ? badge("Live", "ok") : r.mode === "test" ? badge("Test mode", "warn") : badge("Not configured", "bad");
  if (!r.ok) {
    return title("Revenue", "One-time passes and interview packs, from Stripe.") +
      card("Stripe", `<p>${mode} ${esc(r.why ?? "Stripe could not be read.")}</p><p><a href="${esc(r.dashboard)}">Open Stripe</a></p>`);
  }
  const p90 = r.payments;
  const refunds90 = p90.reduce((a, p) => a + p.refunded, 0) / 100;
  const paid30 = p90.filter((p) => p.created >= now - 30 * 86_400).length;
  const byProduct = ["pass30", "pass90", "pack_1", "pack_3", "pack_5", "other"].map((k) => ({
    label: productLabel(k), value: p90.filter((p) => p.product === k).length,
  })).filter((x) => x.value > 0);
  const buyers = new Map<string, number>();
  for (const p of p90) if (p.email) buyers.set(p.email.toLowerCase(), (buyers.get(p.email.toLowerCase()) ?? 0) + 1);
  const repeat = [...buyers.values()].filter((n) => n > 1).length;
  const link = (id: string) => `${r.dashboard}/${encodeURIComponent(id)}`;
  return title("Revenue", "One-time passes and interview packs, from Stripe. Passes do not renew, so this shows sales, not MRR.") +
    `<div class="kpis">` +
    kpi("Net, 7 days", usd(netSince(r, now, 7)), { tone: "a" }) +
    kpi("Net, 30 days", usd(netSince(r, now, 30)), { tone: "b", sub: `${paid30} payment${paid30 === 1 ? "" : "s"}` }) +
    kpi("Net, 90 days", usd(netSince(r, now, 90)), { tone: "a" }) +
    kpi("Refunded, 90 days", usd(refunds90), { tone: "b", sub: `${repeat} repeat buyer${repeat === 1 ? "" : "s"}` }) +
    `</div>` +
    `<div class="grid2">` +
    card("Payments, last 90 days", table(["Date", "Product", "Amount", "Refunded", "Buyer", ""],
      p90.map((p) => [esc(date(p.created)), esc(productLabel(p.product)),
        usd(p.amount / 100), p.refunded ? usd(p.refunded / 100) : `<span class="muted">0</span>`, esc(maskEmail(p.email)),
        (p.disputed ? badge("Disputed", "bad") + " " : "") + `<a href="${esc(link(p.id))}">Stripe</a>`]),
      "No payments in the last 90 days."), { aside: mode }) +
    card("By product", donut(byProduct, { aria: "Payments by product, last 90 days", center: num(p90.length) }) +
      `<p class="muted small">Check: ${paid30} paid in Stripe and ${o.granted30} granted by the webhook in 30 days.` +
      (paid30 !== o.granted30 ? ` <strong>These differ</strong>: a webhook may have been missed; compare in Stripe.` : " They match.") + `</p>` +
      `<p class="small"><a href="${esc(r.dashboard)}">Open payments in Stripe</a></p>`) +
    `</div>`;
}

// ---- Usage & costs ------------------------------------------------------------------------------ //

export function usagePage(o: { stats: Stats; pool: PoolSetting; poolDays: Array<{ label: string; value: number }>;
                               spend: { top: Spender[]; accounts: number; mean: number; p95: number; total: number };
                               months: Array<{ period: string; tokens: number; packages: number; minutes: number }>;
                               networks: { rows: Array<{ net: string; n: number }>; limit: number };
                               domains: Array<{ domain: string; n: number }>; now: number }): string {
  const s = o.stats, m = s.usage_month;
  const avgPerDay = o.poolDays.slice(-7).reduce((a, d) => a + d.value, 0) / 7;
  const hoursIntoDay = ((o.now % 86_400) / 3600) || 0.01;
  const burnPerHour = s.free_pool_today.tokens / hoursIntoDay;
  const left = o.pool.cap - s.free_pool_today.tokens;
  const empties = o.pool.cap === 0 ? "paused" : left <= 0 ? "used up" : burnPerHour > 0 && left / burnPerHour < 24 - hoursIntoDay
    ? `on pace to run out in about ${Math.max(1, Math.round(left / burnPerHour))} h` : "on pace to last the day";
  const poolForm = `<form class="act sep" method="post" action="/settings/free-pool"><h3>Change the free AI limit</h3><div class="row">` +
    `<select name="mode" aria-label="Setting"><option value="custom">Set a daily limit</option><option value="default">Use the deploy's value (${shortNum(o.pool.deployCap)})</option><option value="paused">Pause the free AI</option></select>` +
    `<input type="text" name="tokens" inputmode="numeric" placeholder="Tokens a day, e.g. 3000000" aria-label="Tokens a day" style="flex:0 1 200px">` +
    `<input type="text" name="reason" required minlength="3" maxlength="300" placeholder="Reason" aria-label="Reason">` +
    `<input type="text" name="confirm" placeholder="Type PAUSE to pause" aria-label="Type PAUSE to pause" style="flex:0 1 160px">` +
    `<button class="btn primary" type="submit">Apply</button></div>` +
    `<span class="muted small">Takes effect on the next request; no deploy. ${o.pool.override !== null ? `Set to ${o.pool.override === 0 ? "paused" : num(o.pool.override)} by ${esc(o.pool.updated_by ?? "")} ${esc(ago(o.pool.updated_at, o.now))}.` : "Now using the deploy's value."}</span></form>`;
  return title("Usage & costs", "AI and live-interview use, what it costs us, and the free tier's shared limit. Costs are estimates at list prices.") +
    `<div class="kpis">` +
    kpi("AI cost this month", usd(m.ai_cost_usd.total), { tone: "a", sub: `${shortNum(m.ai_tokens.total)} tokens` }) +
    kpi("Interview cost", usd(m.interviews.cost_usd), { tone: "b", sub: `${num(m.interviews.minutes)} min, ${num(m.interviews.count)} calls` }) +
    kpi("Cost per active user", usd(o.spend.accounts ? o.spend.total / o.spend.accounts : 0), { tone: "a", sub: `p95 ${usd(o.spend.p95)}, ${num(o.spend.accounts)} users` }) +
    kpi("Free AI today", `${shortNum(s.free_pool_today.tokens)}`, { tone: "b", sub: o.pool.cap === 0 ? "paused" : `of ${shortNum(o.pool.cap)}, ${empties}` }) +
    `</div>` +
    `<section class="card" id="free-ai"><div class="chead"><h2>Free AI, last 14 days</h2><span class="muted small">7-day average ${shortNum(avgPerDay)} a day</span></div>` +
    lineChart(o.poolDays, { aria: "Free AI tokens per day, last 14 days", cap: o.pool.cap || undefined, unit: "tokens" }) + poolForm + `</section>` +
    `<div class="grid2e">` +
    card("Top AI users this month", hbars(o.spend.top.map((t) => ({
      label: t.email ? maskEmail(t.email) : t.account_id.slice(0, 14), value: t.cost, href: userHref(t.account_id),
      display: `${usd(t.cost)}${t.cap_percent !== null && t.plan === "free" ? `, ${t.cap_percent.toFixed(0)}% of cap` : ""}`,
    })), "No AI use this month."), { aside: `<span class="muted small">mean ${usd(o.spend.mean)}, p95 ${usd(o.spend.p95)}</span>` }) +
    card("Monthly totals", table(["Month", "AI tokens", "Packages", "Interview min"],
      o.months.slice().reverse().map((x) => [esc(x.period), num(x.tokens), num(x.packages), num(x.minutes)]), "")) +
    `</div>` +
    `<div class="grid2e">` +
    card("Sign-ups today by network", table(["Network (hashed)", "New accounts", ""],
      o.networks.rows.map((r) => [`<span class="mono">${esc(r.net)}</span>`, num(r.n),
        r.n >= o.networks.limit ? badge("At the limit", "bad") : r.n >= 0.5 * o.networks.limit ? badge("Busy", "warn") : ""]),
      "No sign-ups today."), { aside: `<span class="muted small">limit ${num(o.networks.limit)} a day each</span>` }) +
    card("E-mail domains", hbars(o.domains.map((d) => ({ label: d.domain, value: d.n })), "No e-mail sign-ins yet."),
      { aside: `<span class="muted small">a sudden new domain can mean a script</span>` }) +
    `</div>`;
}

// ---- Health ------------------------------------------------------------------------------------- //

export function healthPage(o: { stats: Stats; feed: Feed; runs: Runs; revenue: Revenue; env: Env; now: number }): string {
  const { stats: s, env, now } = o;
  const row = (name: string, status: string, detail: string) => [`<strong>${esc(name)}</strong>`, status, detail];
  const e = s.sign_in_emails;
  const feedAge = o.feed.generated_at ? now - o.feed.generated_at : null;
  const rows: string[][] = [
    row("Company AI key (Anthropic)", s.ai.state === "ok" ? badge("Working", "ok") : s.ai.state === "transient" ? badge("Busy", "warn") : badge("Down", "bad"),
      s.ai.state === "ok" ? "Model calls are going through." : `${esc(s.ai.note || s.ai.summary)}. Owner alerted ${esc(date(s.ai.last_alert_at))}. <a href="https://platform.claude.com/settings/billing">Billing</a>`),
    row("Job feed", !o.feed.ok ? badge("Unreadable", "bad") : feedAge !== null && feedAge > FEED_STALE_HOURS * 3600 ? badge("Stale", "warn") : badge("Fresh", "ok"),
      o.feed.ok ? `${num(o.feed.jobs)} jobs, built ${esc(ago(o.feed.generated_at, now))}. Rebuilt every 3 hours.` : esc(o.feed.why ?? "")),
    row("Sponsor index", o.feed.employers !== null ? badge("Published", "ok") : badge("Unknown", "warn"),
      o.feed.employers !== null ? `${num(o.feed.employers)} employers, built ${esc(ago(o.feed.sponsors_at, now))}.` : "Not readable."),
    ...o.runs.latest.map((r) => row(`GitHub: ${r.workflow}`,
      r.status !== "completed" ? badge("Running", "plain") : r.conclusion === "success" ? badge("Passed", "ok") : badge(r.conclusion || "unknown", "bad"),
      `${esc(ago(r.at, now))}${r.branch ? ` on ${esc(r.branch)}` : ""}. <a href="${esc(r.url)}">Open</a>`)),
    ...(o.runs.ok ? [] : [row("GitHub Actions", badge("Unreadable", "warn"), esc(o.runs.why ?? ""))]),
    row("Stripe", o.revenue.mode === "live" ? badge("Live", "ok") : o.revenue.mode === "test" ? badge("Test mode", "warn") : badge("Off", "bad"),
      o.revenue.ok ? `Readable. ${num(o.revenue.payments.length)} payments in 90 days.` : esc(o.revenue.why ?? "")),
    row("Sign-in e-mails (Resend)", e.level === "ok" ? badge("OK", "ok") : badge(e.level === "red" ? "Nearly full" : "Filling up", e.level === "red" ? "bad" : "warn"),
      `${num(e.today)} of ${num(e.daily_cap)} today, ${num(e.month)} of ${num(e.monthly_cap)} this month.${meter((100 * e.today) / e.daily_cap, e.level === "red" ? "bad" : e.level === "amber" ? "warn" : "ok")}` +
      (e.level === "ok" ? "" : `<span class="small">${esc(e.advice)}</span>`)),
    row("Telegram bot", env.TELEGRAM_BOT_TOKEN ? badge("Configured", "ok") : badge("Off", "warn"),
      env.OWNER_TELEGRAM_CHAT_ID ? "Outage alerts go to your chat." : "No OWNER_TELEGRAM_CHAT_ID: alerts go to the log only."),
    row("Live interviews (Tavus)", env.TAVUS_API_KEY ? badge("Configured", "ok") : badge("Off", "bad"), `${num(s.usage_month.interviews.minutes)} min used this month.`),
    row("Google sign-in", env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET ? badge("Configured", "ok") : badge("Off", "warn"), ""),
    row("Apply agent signing key", env.AGENT_SIGNING_KEY ? badge("Configured", "ok") : badge("Off", "bad"),
      `Published at <a href="https://sponsorjobs.ai/.well-known/http-message-signatures-directory">sponsorjobs.ai/.well-known/...</a>`),
  ];
  return title("Health", "Every outside service SponsorJobs depends on, in one place.") +
    card("Services", table(["Service", "Status", "Detail"], rows, "")) +
    card("Alerts", `<form method="post" action="/admin/test-alert" class="act"><h3>Send a test alert</h3>` +
      `<div class="row"><button class="btn" type="submit">Send test alert</button><span class="muted small">One Telegram message to you, the way an outage alert is sent.</span></div></form>`);
}

// ---- Audit log ---------------------------------------------------------------------------------- //

function auditTable(rows: AuditRow[], withTarget: boolean): string {
  return table(["When", "Who", "Action", ...(withTarget ? ["Account"] : []), "Detail", "Reason"], rows.map((r) => [
    esc(date(r.at)), esc(r.admin), esc(ACTION_LABEL[r.action] ?? r.action),
    ...(withTarget ? [r.target ? `<a class="mono" href="${userHref(r.target)}">${esc(r.target.slice(0, 14))}</a>` : `<span class="muted">none</span>`] : []),
    esc(r.detail), esc(r.reason)]), "No admin actions yet.");
}

export function auditPage(o: { rows: AuditRow[]; more: boolean }): string {
  const last = o.rows[o.rows.length - 1];
  return title("Audit log", "Every change made from this dashboard, newest first. Nothing here can be edited or deleted from the dashboard.") +
    card("Actions", auditTable(o.rows, true) +
      (o.more && last ? `<div class="pager"><a class="btn" href="/audit?before=${last.id}">Older</a></div>` : ""));
}

export const notFound = () => title("Not found", "There is nothing at this address.") + `<p><a href="/">Back to the overview</a></p>`;
