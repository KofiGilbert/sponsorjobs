// The admin dashboard's pages. Each takes already-loaded data and returns the page body; admin.ts
// loads the data, adds the right sidebar and wraps it all in the shell. The overview is the kit's
// "Dashboard Overview" block for block; the other pages reuse its blocks.

import type { Stats } from "./admin";
import {
  ACTION_LABEL, type Activity, type AuditRow, type Feed, FEED_STALE_HOURS, type PoolSetting, RANGES, type RangeKey, type Revenue,
  type Runs, type Spender, type UserDetail, type UserFilter, type UserRow, type UserSort, USERS_PER_PAGE, netSince,
} from "./admin_data";
import { MAX_INTERVIEWS } from "./admin_actions";
import type { Env } from "./env";
import { INTERVIEW_SECONDS, PLANS } from "./metering";
import {
  ago, badge, barChart, card, date, day, donut, esc, facts, kpi, lineBlock, maskEmail, menu, meter, num,
  PASS_LABEL, pageTitle, railList, shortNum, siteBars, table, usd,
} from "./admin_ui";

const userHref = (id: string) => `/users/${encodeURIComponent(id)}`;
const planBadge = (plan: string) => badge(PASS_LABEL[plan] ?? plan, plan === "free" ? "plain" : "ok");
const daysLeft = (until: number, now: number) => {
  const d = Math.ceil((until - now) / 86_400);
  return d <= 1 ? "ends within a day" : `${d} days left`;
};
/** "pass30" -> "Job Hunt Pass", "pack_3" -> "3-interview pack". */
const productLabel = (k: string) => PASS_LABEL[k] ?? (k.startsWith("pack_") ? `${k.slice(5)}-interview pack` : "Other amount");
const title = pageTitle;
const pct = (a: number, b: number) => (b > 0 ? (100 * (a - b)) / b : null);
const shortName = (email: string | null, id: string) => (email ? maskEmail(email).split("@")[0]! : id.slice(5, 11));

// ---- the right sidebar (every page) ------------------------------------------------------------- //

export interface Attention { level: "bad" | "warn" | "ok"; text: string; sub?: string; href?: string; icon?: string }

export function attention(o: { stats: Stats; pool: PoolSetting; revenue: Revenue; feed: Feed; runs: Runs; env: Env;
                               now: number; suspended: number }): Attention[] {
  const out: Attention[] = [];
  const { stats: s, now } = o;
  if (s.ai.state === "company_outage") out.push({ level: "bad", icon: "bug", text: "The company AI key is failing", sub: ago(s.ai.since, now), href: "/alerts" });
  else if (s.ai.state === "transient") out.push({ level: "warn", icon: "bug", text: "The last AI call was rate limited", sub: ago(s.ai.since, now), href: "/alerts" });
  if (!o.feed.ok) out.push({ level: "bad", icon: "broadcast", text: "The job feed could not be read", href: "/feed" });
  else if (o.feed.generated_at && now - o.feed.generated_at > FEED_STALE_HOURS * 3600) {
    out.push({ level: "warn", icon: "broadcast", text: "The job feed is out of date", sub: `Built ${ago(o.feed.generated_at, now)}`, href: "/feed" });
  }
  for (const r of o.runs.latest) {
    if (r.conclusion === "failure" && (r.workflow === "feed" || r.workflow === "grow-boards" || r.workflow === "tests")) {
      out.push({ level: "bad", icon: "bug", text: `GitHub "${r.workflow}" failed`, sub: ago(r.at, now), href: "/feed" });
    }
  }
  if (o.pool.cap === 0) out.push({ level: "warn", icon: "broadcast", text: "The free AI is paused", href: "/usage#free-ai" });
  else if (s.free_pool_today.tokens >= o.pool.cap) out.push({ level: "bad", icon: "broadcast", text: "The free AI limit is used up today", href: "/usage#free-ai" });
  else if (s.free_pool_today.tokens >= 0.75 * o.pool.cap) out.push({ level: "warn", icon: "broadcast", text: "The free AI limit is 75% used", href: "/usage#free-ai" });
  if (s.sign_in_emails.level !== "ok") {
    out.push({ level: s.sign_in_emails.level === "red" ? "bad" : "warn", icon: "broadcast", text: "Sign-in e-mails are near Resend's free limit",
               sub: `${s.sign_in_emails.today} of ${s.sign_in_emails.daily_cap} today`, href: "/alerts" });
  }
  const disputes = o.revenue.payments.filter((p) => p.disputed).length;
  if (disputes) out.push({ level: "bad", icon: "card", text: `${disputes} payment${disputes === 1 ? " is" : "s are"} disputed`, href: "/revenue#payments" });
  if (!o.revenue.ok && o.revenue.mode !== "off") out.push({ level: "warn", icon: "card", text: "Stripe could not be read", href: "/revenue" });
  if (o.revenue.mode === "test") out.push({ level: "warn", icon: "card", text: "Stripe is in test mode", href: "/revenue" });
  if (!o.env.OWNER_TELEGRAM_CHAT_ID) out.push({ level: "warn", icon: "broadcast", text: "Outage alerts have no Telegram chat", href: "/alerts" });
  if (o.suspended) out.push({ level: "ok", icon: "user", text: `${o.suspended} account${o.suspended === 1 ? " is" : "s are"} suspended`, href: "/users?filter=suspended" });
  return out;
}

const ACTIVITY_ICON: Record<Activity["kind"], string> = { signup: "signup", signin: "user", purchase: "card", admin: "admin" };

/** The kit's right sidebar: Notifications, Activities, Contacts. */
export function rail(o: { attention: Attention[]; activity: Activity[]; contacts: Array<{ account_id: string; email: string }>; now: number }): string {
  const notes = o.attention.length ? o.attention : [{ level: "ok" as const, icon: "ok", text: "All clear" }];
  return railList("Notifications", notes.map((n, i) => ({ icon: n.icon ?? "bug", text: n.text, sub: n.sub, href: n.href,
      tone: i % 2 ? "b" as const : "a" as const })),
    { kind: "notifications", id: "notifications" }) +
    railList("Activities", o.activity.map((a) => ({ icon: ACTIVITY_ICON[a.kind], text: a.text, sub: ago(a.at, o.now), href: a.href })),
      { kind: "activities", empty: "Nothing yet" }) +
    railList("Contacts", o.contacts.slice(0, 6).map((c) => ({ initial: c.email.slice(0, 1).toUpperCase(), text: maskEmail(c.email), href: userHref(c.account_id) })),
      { kind: "contacts", empty: "No e-mail sign-ins yet" });
}

// ---- Overview (the kit's frame, block for block) ------------------------------------------------ //

export type ChartKey = "users" | "revenue" | "ai";

export function overviewPage(o: {
  stats: Stats; pool: PoolSetting; revenue: Revenue; now: number; range: RangeKey;
  counts: { signups: readonly [number, number]; signins: readonly [number, number]; checkouts: readonly [number, number] };
  bounds: { start: number; prevStart: number };
  chart: ChartKey; series: { labels: string[]; series: number[]; compare: number[] };
  spend: { top: Spender[] }; poolDays: Array<{ label: string; value: number }>; expired: number;
}): string {
  const { stats: s, now } = o;
  const m = s.usage_month;
  const inRange = (a: number, b: number) => o.revenue.payments.filter((p) => p.created >= a && p.created < b).reduce((x, p) => x + p.amount - p.refunded, 0) / 100;
  const rev = inRange(o.bounds.start, now + 1), revPrev = inRange(o.bounds.prevStart, o.bounds.start);
  const thisMonth = o.revenue.payments.filter((p) => new Date(p.created * 1000).toISOString().slice(0, 7) === m.period);
  const revMonth = thisMonth.reduce((a, p) => a + p.amount - p.refunded, 0) / 100;
  const refundsMonth = thisMonth.reduce((a, p) => a + p.refunded, 0) / 100;
  const margin = revMonth - m.ai_cost_usd.total - m.interviews.cost_usd;
  const rangeLabel = RANGES.find((r) => r.key === o.range)?.label ?? "Last 30 days";
  const link = (q: Record<string, string>) => {
    const u = new URLSearchParams({ range: o.range, chart: o.chart, ...q });
    if (u.get("range") === "30d") u.delete("range");
    if (u.get("chart") === "users") u.delete("chart");
    const qs = u.toString();
    return `/${qs ? `?${qs}` : ""}`;
  };
  const C = o.counts;
  const tabs: Array<{ key: ChartKey; label: string }> = [{ key: "users", label: "Total Users" }, { key: "revenue", label: "Revenue" }, { key: "ai", label: "AI Cost" }];
  const free = s.plans.free, p30 = s.plans.active_pass30, p90 = s.plans.active_pass90;

  return title("Overview", menu(rangeLabel, RANGES.map((r) => ({ label: r.label, href: link({ range: r.key }) })))) +
    kpi("Revenue", o.revenue.ok ? usd(rev) : "n/a", { tone: "a", delta: o.revenue.ok ? pct(rev, revPrev) : null }) +
    kpi("New Users", num(C.signups[0]), { tone: "b", delta: pct(C.signups[0], C.signups[1]) }) +
    kpi("Sign-ins", num(C.signins[0]), { tone: "a", delta: pct(C.signins[0], C.signins[1]) }) +
    kpi("Purchases", num(C.checkouts[0]), { tone: "b", delta: pct(C.checkouts[0], C.checkouts[1]) }) +
    lineBlock({ tabs: tabs.map((t) => ({ label: t.label, href: link({ chart: t.key }), on: t.key === o.chart })), legend: ["This year", "Last year"],
      labels: o.series.labels, series: o.series.series, compare: o.series.compare, unit: o.chart === "users" ? "" : "$",
      aria: `${tabs.find((t) => t.key === o.chart)!.label} by month, this year and last year`, span: 3 }) +
    card("Top AI Users", siteBars(o.spend.top.slice(0, 6).map((t) => ({ label: shortName(t.email, t.account_id), value: t.cost, display: usd(t.cost), href: userHref(t.account_id) })), "None yet"),
      { span: 1 }) +
    card("Costs This Month", barChart([
      { label: "Revenue", value: revMonth, display: usd(revMonth) },
      { label: "Free AI", value: m.ai_cost_usd.free, display: usd(m.ai_cost_usd.free) },
      { label: "Pass AI", value: m.ai_cost_usd.pass, display: usd(m.ai_cost_usd.pass) },
      { label: "Tavus", value: m.interviews.cost_usd, display: usd(m.interviews.cost_usd) },
      { label: "Refunds", value: refundsMonth, display: usd(refundsMonth) },
      { label: "Margin", value: Math.max(0, margin), display: usd(margin) },
    ], { aria: "Revenue, costs and margin this month, in dollars", unit: "$" }), { span: 2 }) +
    card("Users by Plan", donut([
      { label: "Free", value: Math.max(0, free - o.expired) }, { label: "Job Hunt Pass", value: p30 },
      { label: "Season Pass", value: p90 }, { label: "Pass ended", value: o.expired }], { aria: "Accounts by plan" }), { span: 2 }) +
    card("Free AI", barChart(o.poolDays.map((d) => ({ label: d.label.slice(5), value: d.value, display: shortNum(d.value) })),
      { aria: "Free AI tokens per day, last 14 days", single: true }), { span: 4 });
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
  const sortMenu = menu(({ newest: "Newest", oldest: "Oldest", tokens: "AI use this month" } as const)[o.sort],
    ([["newest", "Newest"], ["oldest", "Oldest"], ["tokens", "AI use this month"]] as const).map(([k, l]) => ({ label: l, href: link({ sort: k }) })));
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
  const name = ({ all: "All users", pass: "On a pass", free: "Free", email: "E-mail sign-in", suspended: "Suspended" } as const)[o.filter];
  return title("Users", sortMenu) +
    card(name, table(["Person", "Plan", "Pass ends", "AI tokens", "Packages", "Extra interviews", "Created"], rows,
        o.q ? "No account matches that search." : "No accounts here.") + pager,
      { aside: `<span class="muted small">${num(o.total)}${o.q ? ` matching "${esc(o.q)}"` : ""}</span>` });
}

// ---- one person --------------------------------------------------------------------------------- //

export function userPage(o: { u: UserDetail; history: AuditRow[]; revealed: boolean; now: number }): string {
  const { u, now } = o;
  const plan = PLANS[u.plan];
  const head = title(u.email ? (o.revealed ? u.email : maskEmail(u.email)) : "Anonymous account",
    `<span class="filters">${planBadge(u.plan)}<span class="fsep"></span>${u.suspended_at ? badge(`Suspended ${day(u.suspended_at)}`, "bad") : badge("Active", "ok")}` +
    (u.email && !o.revealed ? `<span class="fsep"></span><form method="post" action="${userHref(u.account_id)}/reveal"><button class="btn" type="submit">Show full e-mail</button></form>` : "") + `</span>`);
  const account = card("Account", facts([
    ["Account id", `<span class="mono">${esc(u.account_id)}</span>`],
    ["E-mail", u.email ? esc(o.revealed ? u.email : maskEmail(u.email)) : `<span class="muted">none (anonymous)</span>`],
    ["Created", esc(date(u.created_at))],
    ["E-mail added", esc(date(u.email_at))],
    ["Plan", `${esc(PASS_LABEL[u.plan] ?? u.plan)}${u.plan !== "free" ? ` until ${esc(date(u.pass_until))} (${esc(daysLeft(u.pass_until, now))})` : u.stored_plan && u.stored_plan !== "free" ? ` <span class="muted small">(${esc(PASS_LABEL[u.stored_plan] ?? u.stored_plan)} ended ${esc(day(u.pass_until))})</span>` : ""}`],
    ["Left on the pass", u.plan !== "free" ? `${num(u.pass_packages)} packages, ${num(u.pass_seconds / INTERVIEW_SECONDS)} live interviews` : `<span class="muted">n/a</span>`],
    ["Extra interviews", num(u.credit_seconds / INTERVIEW_SECONDS)],
    ["Signed-in devices", num(u.sessions)],
    ["Telegram", u.telegram.linked ? `linked${u.telegram.paused ? ", paused" : ""}${u.telegram.linked_at ? ` <span class="muted small">since ${esc(u.telegram.linked_at.slice(0, 10))}</span>` : ""}` : `<span class="muted">not linked</span>`],
    ["Application e-mail", u.alias ? `<span class="mono">${esc(o.revealed ? u.alias.address : maskEmail(u.alias.address))}</span> <span class="muted small">${num(u.alias.mails_30d)} mail${u.alias.mails_30d === 1 ? "" : "s"} in 30 days</span>` : `<span class="muted">none</span>`],
    ["Monthly AI limit", plan?.llmTokenCap ? `${num(plan.llmTokenCap)} tokens` : "n/a"],
  ]), { span: 2 });
  const reasonField = `<input type="text" name="reason" required minlength="3" maxlength="300" placeholder="Reason" aria-label="Reason">`;
  const id = userHref(u.account_id);
  const actions = card("Actions",
    `<form class="act" method="post" action="${id}/give-pass"><h3>Give a pass</h3><div class="row">` +
      `<select name="pass" aria-label="Pass"><option value="pass30">Job Hunt Pass (30 days)</option><option value="pass90">Season Pass (90 days)</option></select>` +
      `${reasonField}<button class="btn primary" type="submit">Give pass</button></div></form>` +
    `<form class="act" method="post" action="${id}/add-interviews"><h3>Add live interviews</h3><div class="row">` +
      `<select name="interviews" aria-label="Interviews">${Array.from({ length: MAX_INTERVIEWS }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join("")}</select>` +
      `${reasonField}<button class="btn" type="submit">Add</button></div></form>` +
    (u.suspended_at
      ? `<form class="act" method="post" action="${id}/unsuspend"><h3>Restore</h3><div class="row">${reasonField}` +
        `<button class="btn" type="submit">Restore</button></div></form>`
      : `<form class="act" method="post" action="${id}/suspend"><h3>Suspend</h3><div class="row">${reasonField}` +
        `<input type="text" name="confirm" required placeholder="Type SUSPEND" aria-label="Type SUSPEND to confirm" style="flex:0 1 140px">` +
        `<button class="btn danger" type="submit">Suspend</button></div></form>`), { span: 2 });
  const months = card("Usage by Month", table(["Month", "AI tokens", "Free packages", "Pass packages"],
    u.months.map((r) => [esc(r.period), num(r.tokens), num(r.packages), num(r.pass_packages)]), "None."), { span: 2 });
  const interviews = card("Live Interviews", table(["Started", "Used", "Ended"],
    u.interviews.map((r) => [esc(date(r.started)), `${num(r.used / 60)} min`, r.ended ? esc(date(r.ended)) : `<span class="muted">open</span>`]),
    "None."), { span: 2 });
  const history = card("History", auditTable(o.history, false), { aside: `<a class="muted small" href="/audit">Audit log</a>` });
  return head + account + actions + months + interviews + history;
}

// ---- Revenue ------------------------------------------------------------------------------------ //

export function revenuePage(o: { revenue: Revenue; granted30: number; now: number }): string {
  const r = o.revenue, now = o.now;
  const mode = r.mode === "live" ? badge("Live", "ok") : r.mode === "test" ? badge("Test mode", "warn") : badge("Not configured", "bad");
  if (!r.ok) return title("Revenue", mode) + card("Stripe", `<p class="small">${esc(r.why ?? "Stripe could not be read.")} <a href="${esc(r.dashboard)}">Open Stripe</a></p>`);
  const p90 = r.payments.filter((p) => p.created >= now - 90 * 86_400);
  const refunds90 = p90.reduce((a, p) => a + p.refunded, 0) / 100;
  const paid30 = p90.filter((p) => p.created >= now - 30 * 86_400).length;
  const byProduct = ["pass30", "pass90", "pack_1", "pack_3", "pack_5", "other"].map((k) => ({
    label: productLabel(k), value: p90.filter((p) => p.product === k).length,
  })).filter((x) => x.value > 0);
  const buyers = new Map<string, number>();
  for (const p of p90) if (p.email) buyers.set(p.email.toLowerCase(), (buyers.get(p.email.toLowerCase()) ?? 0) + 1);
  const repeat = [...buyers.values()].filter((n) => n > 1).length;
  const link = (id: string) => `${r.dashboard}/${encodeURIComponent(id)}`;
  const n7 = netSince(r, now, 7), n30 = netSince(r, now, 30), n90 = netSince(r, now, 90);
  const prev30 = r.payments.filter((p) => p.created >= now - 60 * 86_400 && p.created < now - 30 * 86_400).reduce((a, p) => a + p.amount - p.refunded, 0) / 100;
  return title("Revenue", mode) +
    kpi("Net, 7 Days", usd(n7), { tone: "a" }) +
    kpi("Net, 30 Days", usd(n30), { tone: "b", delta: pct(n30, prev30) }) +
    kpi("Net, 90 Days", usd(n90), { tone: "a" }) +
    kpi("Refunded, 90 Days", usd(refunds90), { tone: "b" }) +
    card("Payments", table(["Date", "Product", "Amount", "Refunded", "Buyer", ""],
      p90.map((p) => [esc(date(p.created)), esc(productLabel(p.product)),
        usd(p.amount / 100), p.refunded ? usd(p.refunded / 100) : `<span class="muted">0</span>`, esc(maskEmail(p.email)),
        (p.disputed ? badge("Disputed", "bad") + " " : "") + `<a href="${esc(link(p.id))}">Stripe</a>`]),
      "None in 90 days."), { span: 4, id: "payments", aside: `<a class="muted small" href="${esc(r.dashboard)}">Open Stripe</a>` }) +
    card("Products", donut(byProduct, { aria: "Payments by product, last 90 days" }), { span: 2, id: "products" }) +
    card("Checks", facts([["Paid in Stripe, 30 days", num(paid30)], ["Granted by webhook", `${num(o.granted30)} ${paid30 === o.granted30 ? badge("Match", "ok") : badge("Differs", "bad")}`],
             ["Repeat buyers", num(repeat)]]), { span: 2, id: "checks" });
}

// ---- Usage & costs ------------------------------------------------------------------------------ //

export function usagePage(o: { stats: Stats; pool: PoolSetting; poolDays: Array<{ label: string; value: number }>;
                               spend: { top: Spender[]; accounts: number; mean: number; p95: number; total: number };
                               months: Array<{ period: string; tokens: number; packages: number; minutes: number }>;
                               networks: { rows: Array<{ net: string; n: number }>; limit: number };
                               domains: Array<{ domain: string; n: number }>; now: number }): string {
  const s = o.stats, m = s.usage_month;
  const hoursIntoDay = ((o.now % 86_400) / 3600) || 0.01;
  const burnPerHour = s.free_pool_today.tokens / hoursIntoDay;
  const left = o.pool.cap - s.free_pool_today.tokens;
  const empties = o.pool.cap === 0 ? "Paused" : left <= 0 ? "Used up" : burnPerHour > 0 && left / burnPerHour < 24 - hoursIntoDay
    ? `Runs out in about ${Math.max(1, Math.round(left / burnPerHour))} h` : "Lasts the day";
  const poolForm = `<form class="act sep" method="post" action="/settings/free-pool"><h3>Free AI limit</h3><div class="row">` +
    `<select name="mode" aria-label="Setting"><option value="custom">Set a daily limit</option><option value="default">Use the deploy's value (${shortNum(o.pool.deployCap)})</option><option value="paused">Pause the free AI</option></select>` +
    `<input type="text" name="tokens" inputmode="numeric" placeholder="Tokens a day" aria-label="Tokens a day" style="flex:0 1 140px">` +
    `<input type="text" name="reason" required minlength="3" maxlength="300" placeholder="Reason" aria-label="Reason">` +
    `<input type="text" name="confirm" placeholder="Type PAUSE to pause" aria-label="Type PAUSE to pause" style="flex:0 1 150px">` +
    `<button class="btn primary" type="submit">Apply</button></div>` +
    (o.pool.override !== null ? `<span class="muted small">${o.pool.override === 0 ? "Paused" : `${num(o.pool.override)} a day`} by ${esc(o.pool.updated_by ?? "")}, ${esc(ago(o.pool.updated_at, o.now))}</span>` : "") +
    `</form>`;
  return title("Usage & costs") +
    kpi("AI Cost", usd(m.ai_cost_usd.total), { tone: "a" }) +
    kpi("Interview Cost", usd(m.interviews.cost_usd), { tone: "b" }) +
    kpi("Cost per User", usd(o.spend.accounts ? o.spend.total / o.spend.accounts : 0), { tone: "a" }) +
    kpi("Free AI Today", o.pool.cap === 0 ? "Paused" : `${shortNum(s.free_pool_today.tokens)} / ${shortNum(o.pool.cap)}`, { tone: "b" }) +
    card("Free AI", barChart(o.poolDays.map((d) => ({ label: d.label.slice(5), value: d.value, display: shortNum(d.value) })),
      { aria: "Free AI tokens per day, last 14 days", single: true }) + poolForm,
      { span: 3, id: "free-ai", aside: `<span class="muted small">${esc(empties)}</span>` }) +
    card("Top AI Users", siteBars(o.spend.top.slice(0, 6).map((t) => ({ label: shortName(t.email, t.account_id), value: t.cost, display: usd(t.cost), href: userHref(t.account_id) })), "None yet"),
      { span: 1, id: "top" }) +
    card("Monthly Totals", table(["Month", "AI tokens", "Packages", "Interview min"],
      o.months.slice().reverse().map((x) => [esc(x.period), num(x.tokens), num(x.packages), num(x.minutes)]), ""),
      { span: 2, aside: `<span class="muted small">mean ${usd(o.spend.mean)}, p95 ${usd(o.spend.p95)}</span>` }) +
    card("Sign-ups Today", table(["Network", "New", ""],
      o.networks.rows.map((r) => [`<span class="mono">${esc(r.net)}</span>`, num(r.n),
        r.n >= o.networks.limit ? badge("At the limit", "bad") : r.n >= 0.5 * o.networks.limit ? badge("Busy", "warn") : ""]),
      "None."), { span: 1, id: "signups", aside: `<span class="muted small">limit ${num(o.networks.limit)}</span>` }) +
    card("E-mail Domains", siteBars(o.domains.map((d) => ({ label: d.domain, value: d.n })), "None yet"), { span: 1 });
}

// ---- Health ------------------------------------------------------------------------------------- //

export function healthPage(o: { stats: Stats; revenue: Revenue; env: Env; now: number }): string {
  const { stats: s, env } = o;
  const row = (name: string, status: string, detail: string) => [esc(name), status, detail];
  const e = s.sign_in_emails;
  const rows: string[][] = [
    row("Company AI key (Anthropic)", s.ai.state === "ok" ? badge("Working", "ok") : s.ai.state === "transient" ? badge("Busy", "warn") : badge("Down", "bad"),
      s.ai.state === "ok" ? "" : `${esc(s.ai.note || s.ai.summary)} <a href="https://platform.claude.com/settings/billing">Billing</a>`),
    row("Stripe", o.revenue.mode === "live" ? badge("Live", "ok") : o.revenue.mode === "test" ? badge("Test mode", "warn") : badge("Off", "bad"),
      o.revenue.ok ? `${num(o.revenue.payments.length)} payments in 400 days` : esc(o.revenue.why ?? "")),
    row("Sign-in e-mails (Resend)", e.level === "ok" ? badge("OK", "ok") : badge(e.level === "red" ? "Nearly full" : "Filling up", e.level === "red" ? "bad" : "warn"),
      `${num(e.today)} of ${num(e.daily_cap)} today, ${num(e.month)} of ${num(e.monthly_cap)} this month${meter((100 * e.today) / e.daily_cap, e.level === "red" ? "bad" : e.level === "amber" ? "warn" : "ok")}`),
    row("Telegram bot", env.TELEGRAM_BOT_TOKEN ? badge("Configured", "ok") : badge("Off", "warn"), env.OWNER_TELEGRAM_CHAT_ID ? "Alerts go to your chat" : "No owner chat"),
    row("Live interviews (Tavus)", env.TAVUS_API_KEY ? badge("Configured", "ok") : badge("Off", "bad"), `${num(s.usage_month.interviews.minutes)} min this month`),
    row("Google sign-in", env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET ? badge("Configured", "ok") : badge("Off", "warn"), ""),
    row("Apply agent signing key", env.AGENT_SIGNING_KEY ? badge("Configured", "ok") : badge("Off", "bad"),
      `<a href="https://sponsorjobs.ai/.well-known/http-message-signatures-directory">Key directory</a>`),
  ];
  return title("Health") + card("Services", table(["Service", "Status", "Detail"], rows, ""));
}

// ---- Job feed ----------------------------------------------------------------------------------- //

export function feedPage(o: { feed: Feed; runs: Runs; now: number }): string {
  const f = o.feed, now = o.now;
  const age = f.generated_at ? now - f.generated_at : null;
  const status = !f.ok ? badge("Unreadable", "bad") : age !== null && age > FEED_STALE_HOURS * 3600 ? badge("Stale", "warn") : badge("Fresh", "ok");
  return title("Job feed", status) +
    kpi("Jobs", f.ok ? num(f.jobs) : "n/a", { tone: "a" }) +
    kpi("Sponsors", f.employers !== null ? num(f.employers) : "n/a", { tone: "b" }) +
    kpi("Feed Built", f.generated_at ? ago(f.generated_at, now) : "n/a", { tone: "a" }) +
    kpi("Index Built", f.sponsors_at ? ago(f.sponsors_at, now) : "n/a", { tone: "b" }) +
    card("GitHub Jobs", table(["Job", "Result", "When", ""], o.runs.latest.map((r) => [esc(r.workflow),
      r.status !== "completed" ? badge("Running", "plain") : r.conclusion === "success" ? badge("Passed", "ok") : badge(r.conclusion || "unknown", "bad"),
      esc(ago(r.at, now)) + (r.branch ? ` <span class="muted small">${esc(r.branch)}</span>` : ""), `<a href="${esc(r.url)}">Open</a>`]),
      o.runs.ok ? "No runs found." : `GitHub could not be read: ${o.runs.why ?? ""}`));
}

// ---- Alerts ------------------------------------------------------------------------------------- //

export function alertsPage(o: { stats: Stats; env: Env; ai: { state: string; since: number; last_seen: number; last_alert_at: number | null; note: string } | null;
                                tests: AuditRow[]; now: number }): string {
  const { stats: s, env, now } = o;
  const e = s.sign_in_emails;
  const stateBadge = s.ai.state === "ok" ? badge("Working", "ok") : s.ai.state === "transient" ? badge("Busy", "warn") : badge("Down", "bad");
  return title("Alerts") +
    kpi("AI Key", s.ai.state === "ok" ? "Working" : s.ai.state === "transient" ? "Busy" : "Down", { tone: "a" }) +
    kpi("Last Alert", o.ai?.last_alert_at ? ago(o.ai.last_alert_at, now) : "Never", { tone: "b" }) +
    kpi("E-mails Today", `${num(e.today)} / ${num(e.daily_cap)}`, { tone: "a" }) +
    kpi("Telegram", env.OWNER_TELEGRAM_CHAT_ID && env.TELEGRAM_BOT_TOKEN ? "On" : "Off", { tone: "b" }) +
    card("Company AI Key", facts([
      ["State", stateBadge],
      ["Since", o.ai ? esc(date(o.ai.since)) : "unknown"], ["Last failure", o.ai && o.ai.state !== "ok" ? esc(date(o.ai.last_seen)) : "none"],
      ["Cause", o.ai?.note ? esc(o.ai.note) : `<span class="muted">none</span>`],
      ["Owner alerted", o.ai?.last_alert_at ? esc(date(o.ai.last_alert_at)) : "never"],
    ]), { span: 2 }) +
    card("Test Alert", `<form method="post" action="/admin/test-alert" class="act"><div class="row"><button class="btn primary" type="submit">Send test alert</button></div></form>` +
      table(["When", "Result"], o.tests.map((t) => [esc(date(t.at)), t.detail === "sent" ? badge("Sent", "ok") : badge(t.detail, "bad")]), "None sent yet."), { span: 2 });
}

// ---- Audit log ---------------------------------------------------------------------------------- //

function auditTable(rows: AuditRow[], withTarget: boolean): string {
  return table(["When", "Who", "Action", ...(withTarget ? ["Account"] : []), "Detail", "Reason"], rows.map((r) => [
    esc(date(r.at)), esc(r.admin), esc(ACTION_LABEL[r.action] ?? r.action),
    ...(withTarget ? [r.target ? `<a class="mono" href="${userHref(r.target)}">${esc(r.target.slice(0, 14))}</a>` : `<span class="muted">none</span>`] : []),
    esc(r.detail), esc(r.reason)]), "None yet.");
}

export function auditPage(o: { rows: AuditRow[]; more: boolean }): string {
  const last = o.rows[o.rows.length - 1];
  return title("Audit log") +
    card("Actions", auditTable(o.rows, true) +
      (o.more && last ? `<div class="pager"><a class="btn" href="/audit?before=${last.id}">Older</a></div>` : ""));
}

export const notFound = () => title("Not found") + `<p><a href="/">Overview</a></p>`;
