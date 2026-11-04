// The admin dashboard's pages. Each takes already-loaded data and returns the page body; admin.ts
// loads the data, adds the right sidebar and wraps it all in the shell. The overview is the kit's
// "Dashboard Overview" block for block; the other pages reuse its blocks.

import type { Stats } from "./admin";
import {
  ACTION_LABEL, type Activity, lastMonths, type AuditRow, type Feed, FEED_STALE_HOURS, type PoolSetting, RANGES, type RangeKey, type Revenue,
  type Runs, type Spender, type UserDetail, type UserFilter, type UserRow, type UserSort, USERS_PER_PAGE, netSince,
} from "./admin_data";
import { MAX_INTERVIEWS } from "./admin_actions";
import type { Env } from "./env";
import { INTERVIEW_SECONDS, PLANS } from "./metering";
import {
  ago, badge, barChart, card, day, donut, esc, kpi, lineBlock, maskEmail, menu, mini, num,
  PASS_LABEL, pageTitle, railList, shortNum, siteBars, table, usd, when,
} from "./admin_ui";

const userHref = (id: string) => `/users/${encodeURIComponent(id)}`;
const planBadge = (plan: string) => badge(PASS_LABEL[plan] ?? plan, plan === "free" ? "plain" : "ok");
/** "pass30" -> "Job Hunt Pass", "pack_3" -> "3-interview pack". */
const productLabel = (k: string) => PASS_LABEL[k] ?? (k.startsWith("pack_") ? `Pack of ${k.slice(5)}` : "Other");
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
    `<a href="${userHref(r.account_id)}">${r.email ? esc(maskEmail(r.email)) : `Anonymous <span class="muted">${esc(r.account_id.slice(5, 11))}</span>`}</a>`,
    r.suspended_at ? badge("Suspended", "bad") : planBadge(r.plan),
    r.plan === "free" ? `<span class="muted">none</span>` : esc(when(r.pass_until, o.now)),
    num(r.tokens_month), num(r.packages_month),
    r.credit_seconds > 0 ? num(r.credit_seconds / INTERVIEW_SECONDS) : `<span class="muted">0</span>`,
    esc(when(r.created_at, o.now)),
  ]);
  const pages = Math.max(1, Math.ceil(o.total / USERS_PER_PAGE));
  const pager = pages <= 1 ? "" : `<div class="pager"><span class="muted small">Page ${o.page} of ${pages}</span>` +
    (o.page > 1 ? `<a class="btn" href="${esc(link({ page: o.page - 1 }))}">Previous</a>` : "") +
    (o.page < pages ? `<a class="btn" href="${esc(link({ page: o.page + 1 }))}">Next</a>` : "") + `</div>`;
  const name = ({ all: "All users", pass: "On a pass", free: "Free", email: "E-mail sign-in", suspended: "Suspended" } as const)[o.filter];
  return title("Users", sortMenu) +
    card(o.q ? `${name}: "${o.q}"` : name, table(["Person", "Plan", "Pass ends", "AI tokens", "Packages", "Interviews", "Joined"], rows,
        o.q ? "No match" : "None yet") + pager);
}

// ---- one person --------------------------------------------------------------------------------- //

export type UserAct = "pass" | "interviews" | "suspend";

/** One person, in the kit's grammar: four number cards, a chart, a short details list, one action
 * at a time behind the kit's tabs, then tables. */
export function userPage(o: { u: UserDetail; history: AuditRow[]; revealed: boolean; now: number; act: UserAct }): string {
  const { u, now } = o;
  const id = userHref(u.account_id);
  const onPass = u.plan !== "free";
  const daysLeftN = onPass ? Math.max(0, Math.ceil((u.pass_until - now) / 86_400)) : 0;
  const interviewsLeft = (onPass ? u.pass_seconds : 0) / INTERVIEW_SECONDS + u.credit_seconds / INTERVIEW_SECONDS;
  const freeUsed = u.months.find((m) => m.period === new Date(now * 1000).toISOString().slice(0, 7))?.packages ?? 0;
  const head = title(u.email ? (o.revealed ? u.email : maskEmail(u.email)) : "Anonymous account",
    `<span class="filters">${planBadge(u.plan)}<span class="fsep"></span>${u.suspended_at ? badge("Suspended", "bad") : badge("Active", "ok")}` +
    (u.email && !o.revealed ? `<span class="fsep"></span><form method="post" action="${id}/reveal"><button class="btn" type="submit">Show full e-mail</button></form>` : "") + `</span>`);

  const months = lastMonths(now * 1000, 6).map((m) => ({ label: m.label, value: u.months.find((x) => x.period === m.period)?.tokens ?? 0 }));
  const details = mini([
    ["Account", `<span class="mono">${esc(u.account_id.slice(0, 13))}</span>`],
    ["Joined", esc(day(u.created_at))],
    ["Sign-in", u.email ? "E-mail" : "Anonymous"],
    ["Devices", num(u.sessions)],
    ["Telegram", u.telegram.linked ? (u.telegram.paused ? "Paused" : "Linked") : "No"],
    ["Alias", u.alias ? `${esc((o.revealed ? u.alias.address : maskEmail(u.alias.address)).split("@")[0]!)}` : "No"],
  ]);

  const reason = `<input type="text" name="reason" required minlength="3" maxlength="300" placeholder="Reason" aria-label="Reason">`;
  const forms: Record<UserAct, string> = {
    pass: `<form class="act" method="post" action="${id}/give-pass"><div class="row">` +
      `<select name="pass" aria-label="Pass"><option value="pass30">Job Hunt Pass</option><option value="pass90">Season Pass</option></select>` +
      `${reason}<button class="btn primary" type="submit">Give pass</button></div></form>`,
    interviews: `<form class="act" method="post" action="${id}/add-interviews"><div class="row">` +
      `<select name="interviews" aria-label="Interviews">${Array.from({ length: MAX_INTERVIEWS }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join("")}</select>` +
      `${reason}<button class="btn primary" type="submit">Add</button></div></form>`,
    suspend: u.suspended_at
      ? `<form class="act" method="post" action="${id}/unsuspend"><div class="row">${reason}<button class="btn primary" type="submit">Restore</button></div></form>`
      : `<form class="act" method="post" action="${id}/suspend"><div class="row">${reason}` +
        `<input type="text" name="confirm" required placeholder="Type SUSPEND" aria-label="Type SUSPEND to confirm" style="flex:0 1 130px">` +
        `<button class="btn danger" type="submit">Suspend</button></div></form>`,
  };
  const tabs = ([["pass", "Give Pass"], ["interviews", "Add Interviews"], ["suspend", u.suspended_at ? "Restore" : "Suspend"]] as const)
    .map(([k, l]) => `<a class="ltab${o.act === k ? " on" : ""}" href="${id}?act=${k}">${l}</a>`).join("");

  return head +
    kpi("Plan", ({ free: "Free", pass30: "Job Hunt", pass90: "Season" } as Record<string, string>)[u.plan] ?? u.plan, { tone: "a" }) +
    kpi(onPass ? "Days Left" : "Free Packages", onPass ? num(daysLeftN) : `${num(freeUsed)} / ${num(PLANS.free!.packages)}`, { tone: "b" }) +
    kpi("Packages Left", onPass ? num(u.pass_packages) : num(Math.max(0, PLANS.free!.packages - freeUsed)), { tone: "a" }) +
    kpi("Interviews Left", num(interviewsLeft), { tone: "b" }) +
    card("AI Use", barChart(months.map((m) => ({ label: m.label, value: m.value, display: `${num(m.value)} tokens` })),
      { aria: "AI tokens per month, last 6 months", single: true }), { span: 3 }) +
    card("Details", details, { span: 1 }) +
    `<section class="block s2"><div class="ltabs">${tabs}</div>${forms[o.act]}</section>` +
    card("Live Interviews", table(["Date", "Minutes", "Ended"],
      u.interviews.map((r) => [esc(when(r.started, now)), num(r.used / 60), r.ended ? badge("Yes", "ok") : badge("Open", "plain")]), "None yet"), { span: 2 }) +
    card("History", auditTable(o.history, false, now), { aside: `<a class="muted small" href="/audit">Audit log</a>` });
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
      p90.map((p) => [esc(when(p.created, now)), esc(productLabel(p.product)),
        usd(p.amount / 100), p.refunded ? usd(p.refunded / 100) : `<span class="muted">0</span>`, esc(maskEmail(p.email)),
        (p.disputed ? badge("Disputed", "bad") + " " : "") + `<a href="${esc(link(p.id))}">Stripe</a>`]),
      "None in 90 days."), { span: 4, id: "payments", aside: `<a class="muted small" href="${esc(r.dashboard)}">Open Stripe</a>` }) +
    card("Products", donut(byProduct, { aria: "Payments by product, last 90 days" }), { span: 2, id: "products" }) +
    card("Checks", mini([["Stripe, 30 days", num(paid30)], ["Webhook, 30 days", num(o.granted30)],
      ["Match", paid30 === o.granted30 ? badge("Yes", "ok") : badge("No", "bad")], ["Repeat buyers", num(repeat)]]), { span: 2, id: "checks" });
}

// ---- Usage & costs ------------------------------------------------------------------------------ //

export type PoolAct = "limit" | "default" | "pause";

export function usagePage(o: { stats: Stats; pool: PoolSetting; poolDays: Array<{ label: string; value: number }>;
                               spend: { top: Spender[]; accounts: number; mean: number; p95: number; total: number };
                               months: Array<{ period: string; tokens: number; packages: number; minutes: number }>;
                               networks: { rows: Array<{ net: string; n: number }>; limit: number };
                               domains: Array<{ domain: string; n: number }>; now: number; act: PoolAct }): string {
  const s = o.stats, m = s.usage_month;
  const hoursIntoDay = ((o.now % 86_400) / 3600) || 0.01;
  const burnPerHour = s.free_pool_today.tokens / hoursIntoDay;
  const left = o.pool.cap - s.free_pool_today.tokens;
  const pace = o.pool.cap === 0 ? badge("Paused", "warn") : left <= 0 ? badge("Used up", "bad")
    : burnPerHour > 0 && left / burnPerHour < 24 - hoursIntoDay ? badge(`Runs out in ${Math.max(1, Math.round(left / burnPerHour))} h`, "warn") : badge("On pace", "ok");
  const reason = `<input type="text" name="reason" required minlength="3" maxlength="300" placeholder="Reason" aria-label="Reason">`;
  const forms: Record<PoolAct, string> = {
    limit: `<form class="act" method="post" action="/settings/free-pool"><input type="hidden" name="mode" value="custom"><div class="row">` +
      `<input type="text" name="tokens" inputmode="numeric" placeholder="Tokens a day" aria-label="Tokens a day" style="flex:0 1 140px">${reason}` +
      `<button class="btn primary" type="submit">Apply</button></div></form>`,
    default: `<form class="act" method="post" action="/settings/free-pool"><input type="hidden" name="mode" value="default"><div class="row">${reason}` +
      `<button class="btn primary" type="submit">Use ${shortNum(o.pool.deployCap)}</button></div></form>`,
    pause: `<form class="act" method="post" action="/settings/free-pool"><input type="hidden" name="mode" value="paused"><div class="row">${reason}` +
      `<input type="text" name="confirm" required placeholder="Type PAUSE" aria-label="Type PAUSE to confirm" style="flex:0 1 120px">` +
      `<button class="btn danger" type="submit">Pause</button></div></form>`,
  };
  const tabs = ([["limit", "Set Limit"], ["default", "Default"], ["pause", "Pause"]] as const)
    .map(([k, l]) => `<a class="ltab${o.act === k ? " on" : ""}" href="/usage?act=${k}#free-ai">${l}</a>`).join("");
  const set = o.pool.override !== null ? `<span class="muted small">${o.pool.override === 0 ? "Paused" : `${shortNum(o.pool.override)} a day`}, ${esc(ago(o.pool.updated_at, o.now))}</span>` : "";
  const prev = o.months[o.months.length - 2], cur = o.months[o.months.length - 1];
  return title("Usage & costs") +
    kpi("AI Cost", usd(m.ai_cost_usd.total), { tone: "a", delta: prev && cur ? pct(cur.tokens, prev.tokens) : null }) +
    kpi("Interview Cost", usd(m.interviews.cost_usd), { tone: "b", delta: prev && cur ? pct(cur.minutes, prev.minutes) : null }) +
    kpi("Cost per User", usd(o.spend.accounts ? o.spend.total / o.spend.accounts : 0), { tone: "a" }) +
    kpi("Top User Cost", usd(o.spend.p95), { tone: "b" }) +
    card("Free AI", barChart(o.poolDays.map((d) => ({ label: d.label.slice(5), value: d.value, display: shortNum(d.value) })),
      { aria: "Free AI tokens per day, last 14 days", single: true }), { span: 3, aside: pace }) +
    card("Top AI Users", siteBars(o.spend.top.slice(0, 6).map((t) => ({ label: shortName(t.email, t.account_id), value: t.cost, display: usd(t.cost), href: userHref(t.account_id) })), "None yet"),
      { span: 1, id: "top" }) +
    `<section class="block s2" id="free-ai"><div class="bhead"><div class="ltabs">${tabs}</div>${set}</div>${forms[o.act]}</section>` +
    card("Sign-ups Today", siteBars(o.networks.rows.map((r) => ({ label: r.net.slice(0, 6), value: r.n, display: `${num(r.n)} of ${num(o.networks.limit)}` })), "None yet"),
      { span: 1, id: "signups" }) +
    card("E-mail Domains", siteBars(o.domains.map((d) => ({ label: d.domain.split(".")[0]!, value: d.n, display: `${esc(d.domain)}: ${num(d.n)}` })), "None yet"), { span: 1 }) +
    card("Monthly Totals", table(["Month", "AI tokens", "Packages", "Interview min"],
      o.months.slice().reverse().map((x) => [esc(x.period), num(x.tokens), num(x.packages), num(x.minutes)]), ""), { span: 4 });
}

// ---- Health ------------------------------------------------------------------------------------- //

export function healthPage(o: { stats: Stats; revenue: Revenue; env: Env; now: number; emailDays: Array<{ label: string; value: number }> }): string {
  const { stats: s, env } = o;
  const e = s.sign_in_emails;
  const ok = (v: unknown) => (v ? badge("On", "ok") : badge("Off", "bad"));
  const rows: string[][] = [
    ["AI key", s.ai.state === "ok" ? badge("Working", "ok") : s.ai.state === "transient" ? badge("Busy", "warn") : badge("Down", "bad")],
    ["Stripe", o.revenue.mode === "live" ? badge("Live", "ok") : o.revenue.mode === "test" ? badge("Test", "warn") : badge("Off", "bad")],
    ["E-mails", e.level === "ok" ? badge("OK", "ok") : badge(e.level === "red" ? "Full" : "Busy", e.level === "red" ? "bad" : "warn")],
    ["Telegram", ok(env.TELEGRAM_BOT_TOKEN && env.OWNER_TELEGRAM_CHAT_ID)],
    ["Tavus", ok(env.TAVUS_API_KEY)],
    ["Google sign-in", ok(env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET)],
    ["Signing key", ok(env.AGENT_SIGNING_KEY)],
  ];
  return title("Health") +
    kpi("AI Key", s.ai.state === "ok" ? "Working" : s.ai.state === "transient" ? "Busy" : "Down", { tone: "a" }) +
    kpi("Stripe", o.revenue.mode === "live" ? "Live" : o.revenue.mode === "test" ? "Test" : "Off", { tone: "b" }) +
    kpi("E-mails Today", `${num(e.today)} / ${num(e.daily_cap)}`, { tone: "a" }) +
    kpi("Interview Min", num(s.usage_month.interviews.minutes), { tone: "b" }) +
    card("Services", table(["Service", "Status"], rows, ""), { span: 2 }) +
    card("Sign-in E-mails", barChart(o.emailDays.map((d) => ({ label: d.label.slice(5), value: d.value })),
      { aria: "Sign-in e-mails sent per day, last 14 days", single: true }), { span: 2, aside: `<span class="muted small">limit ${num(e.daily_cap)} a day</span>` });
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
    card("GitHub Jobs", table(["Job", "Result", "When", "Run"], o.runs.latest.map((r) => [esc(r.workflow),
      r.status !== "completed" ? badge("Running", "plain") : r.conclusion === "success" ? badge("Passed", "ok") : badge(r.conclusion || "unknown", "bad"),
      esc(when(r.at, now)), `<a href="${esc(r.url)}">Open</a>`]),
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
    card("Company AI Key", mini([
      ["State", stateBadge],
      ["Since", o.ai ? esc(when(o.ai.since, now)) : "Unknown"], ["Last failure", o.ai && o.ai.state !== "ok" ? esc(when(o.ai.last_seen, now)) : "None"],
      ["Cause", o.ai?.note ? esc(o.ai.note.slice(0, 40)) : "None"],
      ["Owner alerted", o.ai?.last_alert_at ? esc(when(o.ai.last_alert_at, now)) : "Never"],
    ]), { span: 2 }) +
    card("Test Alerts", table(["When", "Result"], o.tests.map((t) => [esc(when(t.at, now)), t.detail === "sent" ? badge("Sent", "ok") : badge("Failed", "bad")]), "None yet"),
      { span: 2, aside: `<form method="post" action="/admin/test-alert"><button class="btn primary" type="submit">Send test alert</button></form>` });
}

// ---- Audit log ---------------------------------------------------------------------------------- //

function auditTable(rows: AuditRow[], withTarget: boolean, now: number): string {
  return table(["When", "Who", "Action", ...(withTarget ? ["Account"] : []), "Detail", "Reason"], rows.map((r) => [
    esc(when(r.at, now)), esc(r.admin.split("@")[0]!), esc(ACTION_LABEL[r.action] ?? r.action),
    ...(withTarget ? [r.target ? `<a class="mono" href="${userHref(r.target)}">${esc(r.target.slice(5, 11))}</a>` : `<span class="muted">none</span>`] : []),
    esc(r.detail), `<span class="wrapc">${esc(r.reason)}</span>`]), "None yet");
}

export function auditPage(o: { rows: AuditRow[]; more: boolean; now: number;
                               counts: { total: number; passes: number; suspends: number; last: number | null } }): string {
  const last = o.rows[o.rows.length - 1];
  return title("Audit log") +
    kpi("Actions, 30 Days", num(o.counts.total), { tone: "a" }) +
    kpi("Passes Given", num(o.counts.passes), { tone: "b" }) +
    kpi("Suspensions", num(o.counts.suspends), { tone: "a" }) +
    kpi("Last Action", o.counts.last ? when(o.counts.last, o.now) : "None", { tone: "b" }) +
    card("Actions", auditTable(o.rows, true, o.now) +
      (o.more && last ? `<div class="pager"><a class="btn" href="/audit?before=${last.id}">Older</a></div>` : ""));
}

export const notFound = () => title("Not found") + `<p><a href="/">Overview</a></p>`;
