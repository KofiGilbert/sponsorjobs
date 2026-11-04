// The admin dashboard's look: the shell (sidebar, header, right sidebar), the CSS and the components
// every page is built from. Server-rendered HTML; the only script is the kit's ⌘K search shortcut.
//
// Built to ByeWind's free "Dashboard UI Kit" (SnowUI), Figma Community,
// https://www.figma.com/community/file/1210542873091115123, CC BY 4.0, credited in the footer.
// Every size, colour and type style below was read from that file's "Dashboard Overview" frames
// (light 549:8646, dark 54869:11584) through Figma's plugin API, not estimated from a picture:
//   frame 1440: sidebar 212 (padding 16), header 68 (padding 20 28), right sidebar 280 (padding 16)
//   sidebar: brand 40 high; Favorites / Recently tabs (12/16, 40% and 20%); a favourite is a dot in
//            a 16 slot and its text at 44; a page is a chevron slot (16), gap 4, icon 20, gap 8, text
//            at 72; rows 36 high, 4 apart, radius 12, the active one #000 at 4%; group label 28 high
//   header: sidebar and star buttons (24, padding 4), breadcrumb (12/16, 40% then 100%, "/" at 10%),
//           search 160x28 radius 16 with a ⌘K key, then sun, history, bell, sidebar (24 each, gap 8)
//   content: padding 28; title row 28 high (Semibold 14 with a "Today" menu), the grid 16 below,
//            gap 28; KPI cards 202x112 radius 20 padding 24 (#edeefc / #e6f1fd); blocks #f9f9fa
//            radius 20 padding 24 gap 16
//   type: Inter. Regular 14/20 and 12/16, Semibold 14/20 (block titles), Semibold 24/36 (numbers)
//   text: #000 at 100% / 40% / 20%; lines #000 at 10% (dark #333: #fff, 40%, 20%, lines 15%)
//   "Total Users" chart: tabs (Semibold 14, others at 40%), "|" at 20%, legend dots; y labels in
//            four 62-high rows, seven month labels in equal columns; the line fades from 40% to
//            100% black over a soft radial shading, the comparison line #a0bce8 dashed 2 4
//   "Traffic by Website": label column 57, gap 16, three 2-high segments (100% / 40% / 10%),
//            gap 2, rows 43 apart.   "Traffic by Device": y labels in four 49-high rows; bars 28
//            wide, top radius 8, #a0bce8 #6be6d3 #000 #33d1ff #b899eb #71dd8c.   "Traffic by
//            Location": a 120 ring (outer 60, inner 30) of rounded segments with gaps, black,
//            #33d1ff, #71dd8c, #a0bce8 clockwise from the top; legend rows 32 apart, 12px dots
//   right sidebar: Notifications (24 tile, radius 8), Activities (round avatars on a timeline),
//            Contacts (round avatars), titles Regular 14
// Icons are Phosphor (src/admin_icons.ts), the kit's set, shading at the kit's 8%.

import { KIT, PHOSPHOR, TREND_DOWN, TREND_UP } from "./admin_icons";

export const esc = (v: unknown): string => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
export const num = (n: number) => Math.round(n).toLocaleString("en-US");
export const usd = (n: number) =>
  `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
export const date = (s: number | null | undefined) =>
  s ? new Date(s * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC" : "unknown";
export const day = (s: number | null | undefined) => (s ? new Date(s * 1000).toISOString().slice(0, 10) : "unknown");

/** "kofi.gilbert@gmail.com" -> "ko***@gmail.com": lists show this; a person's own page shows it all. */
export function maskEmail(email: string | null | undefined): string {
  if (!email) return "";
  const at = email.lastIndexOf("@");
  if (at <= 0) return "***";
  return `${email.slice(0, Math.min(2, at))}***${email.slice(at)}`;
}

/** "Just now", "59 minutes ago", "3 days ago" (the kit's wording): how long before `now`. */
export function ago(s: number | null | undefined, now: number): string {
  if (!s) return "never";
  const d = Math.max(0, now - s);
  if (d < 60) return "Just now";
  const unit = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"} ago`;
  if (d < 3600) return unit(Math.floor(d / 60), "minute");
  if (d < 86_400) return unit(Math.floor(d / 3600), "hour");
  return unit(Math.floor(d / 86_400), "day");
}

/** A table date the kit's way: "Just now", "3 hours ago", then "Nov 4, 2026". */
export function when(s: number | null | undefined, now: number): string {
  if (!s) return "unknown";
  if (now - s >= 0 && now - s < 86_400) return ago(s, now);
  return new Date(s * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}

export const PASS_LABEL: Record<string, string> = { free: "Free", pass30: "Job Hunt Pass", pass90: "Season Pass" };

// ---- icons -------------------------------------------------------------------------------------- //

/** An icon at `size` px (20 in the sidebar, 16 elsewhere), drawn in currentColor. */
export function icon(name: string, size = 16): string {
  const k = KIT[name];
  return `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 ${k ? `${k.box} ${k.box}` : "256 256"}" fill="currentColor" aria-hidden="true">` +
    `${k ? k.paths : PHOSPHOR[name] ?? ""}</svg>`;
}
const trend = (up: boolean) =>
  `<svg class="ic" width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="${up ? TREND_UP : TREND_DOWN}"/></svg>`;

// ---- preferences (a cookie; set by GET /prefs) --------------------------------------------------- //

export interface Prefs {
  theme: "" | "light" | "dark";            // "" follows the Mac's setting
  sideHidden: boolean;                     // the header's left sidebar button
  railHidden: boolean;                     // the header's right sidebar button
  tab: "favorites" | "recently";           // the sidebar's Favorites / Recently tabs
  favs: Array<{ href: string; label: string }>;     // starred pages, newest first
  recent: Array<{ href: string; label: string }>;   // pages visited, newest first
}
export const DEFAULT_FAVS = [{ href: "/", label: "Overview" }, { href: "/usage", label: "Usage & costs" }];

// ---- the shell ---------------------------------------------------------------------------------- //

export type PageId = "overview" | "revenue" | "usage" | "users" | "health" | "feed" | "audit" | "alerts";

interface NavItem { id: PageId; label: string; href: string; children?: Array<{ label: string; href: string; key: string }> }
/** The kit's sidebar, item for item: three dashboards and five pages, the first page expandable
 * into five, the other dashboards into their own sections. */
export const NAV: Array<{ group: string; items: NavItem[] }> = [
  { group: "Dashboards", items: [
    { id: "overview", label: "Overview", href: "/" },
    { id: "revenue", label: "Revenue", href: "/revenue", children: [
      { label: "Payments", href: "/revenue#payments", key: "payments" }, { label: "Products", href: "/revenue#products", key: "products" },
      { label: "Checks", href: "/revenue#checks", key: "checks" }] },
    { id: "usage", label: "Usage & costs", href: "/usage", children: [
      { label: "Free AI", href: "/usage#free-ai", key: "free-ai" }, { label: "Top users", href: "/usage#top", key: "top" },
      { label: "Sign-ups", href: "/usage#signups", key: "signups" }] },
  ] },
  { group: "Pages", items: [
    { id: "users", label: "Users", href: "/users", children: [
      { label: "Overview", href: "/users", key: "all" }, { label: "On a pass", href: "/users?filter=pass", key: "pass" },
      { label: "Free", href: "/users?filter=free", key: "free" }, { label: "E-mail sign-in", href: "/users?filter=email", key: "email" },
      { label: "Suspended", href: "/users?filter=suspended", key: "suspended" }] },
    { id: "health", label: "Health", href: "/health" },
    { id: "feed", label: "Job feed", href: "/feed" },
    { id: "audit", label: "Audit log", href: "/audit" },
    { id: "alerts", label: "Alerts", href: "/alerts" },
  ] },
];

export interface Shell {
  page: PageId;
  title: string;               // the page title and the breadcrumb's last part
  crumbs?: Array<{ label: string; href?: string }>;   // between the group and the title
  viewer: string;              // the Access e-mail
  generatedAt: number;
  body: string;
  rail?: string;               // the right sidebar
  flash?: { kind: "ok" | "bad"; text: string } | null;
  query?: string;              // keeps the search box filled on the Users page
  badges?: Partial<Record<PageId, string>>;   // e.g. a count of problems beside Health
  prefs: Prefs;
  here: string;                // this page's path and query (for the star and the return links)
  sub?: string;                // the open sub-item's key (e.g. the Users filter)
  scriptHash?: string;         // unused by the markup; admin.ts puts it in the CSP
}

const pref = (s: Shell, q: string) => `/prefs?${q}&back=${encodeURIComponent(s.here)}`;

export function shell(s: Shell): string {
  const p = s.prefs;
  const navItem = (it: NavItem) => {
    const on = it.id === s.page;
    const ic = icon(it.id === "overview" && on ? "overviewOn" : it.id, 20);
    const badge = s.badges?.[it.id] ? `<span class="nbadge">${esc(s.badges[it.id])}</span>` : "";
    if (!it.children) {
      return `<a class="nav${on ? " on" : ""}" href="${it.href}"${on ? ' aria-current="page"' : ""}><span class="slot"></span>` +
        `<span class="it">${ic}<span>${esc(it.label)}</span></span>${badge}</a>`;
    }
    return `<details class="navd"${on ? " open" : ""}><summary class="nav${on && !s.sub ? " on" : ""}"><span class="slot caret">${icon("caret")}</span>` +
      `<span class="it">${ic}<span>${esc(it.label)}</span></span>${badge}</summary>` +
      it.children.map((c) => `<a class="nav subi${on && s.sub === c.key ? " on" : ""}" href="${c.href}">${esc(c.label)}</a>`).join("") + `</details>`;
  };
  const list = (p.tab === "recently" ? p.recent : p.favs);
  const favs = `<div class="group"><div class="ftabs"><a class="ftab${p.tab === "favorites" ? " on" : ""}" href="${pref(s, "tab=favorites")}">Favorites</a>` +
    `<a class="ftab${p.tab === "recently" ? " on" : ""}" href="${pref(s, "tab=recently")}">Recently</a></div>` +
    (list.length ? list.map((f) => `<a class="nav fav" href="${esc(f.href)}"><span class="slot"><span class="fdot"></span></span><span>${esc(f.label)}</span></a>`).join("")
      : `<span class="nav fav empty"><span class="slot"></span><span>${p.tab === "recently" ? "Nothing yet" : "Star a page"}</span></span>`) + `</div>`;
  const nav = NAV.map((g) => `<div class="group"><div class="glabel">${esc(g.group)}</div>${g.items.map(navItem).join("")}</div>`).join("");
  const group = NAV.find((g) => g.items.some((i) => i.id === s.page))?.group ?? "Dashboards";
  const crumbs = [{ label: group }, ...(s.crumbs ?? []), { label: s.title }]
    .map((c: { label: string; href?: string }, i, all) => i === all.length - 1 ? `<span class="crumb now">${esc(c.label)}</span>`
      : c.href ? `<a class="crumb" href="${c.href}">${esc(c.label)}</a>` : `<span class="crumb">${esc(c.label)}</span>`)
    .join('<span class="sep">/</span>');
  const starred = p.favs.some((f) => f.href === s.here);
  const nextTheme = p.theme === "" ? "dark" : p.theme === "dark" ? "light" : "system";
  const flash = s.flash ? `<div class="flash ${s.flash.kind}" role="status"><span class="dot ${s.flash.kind === "ok" ? "ok" : "bad"}"></span>${esc(s.flash.text)}</div>` : "";
  const showRail = Boolean(s.rail) && !p.railHidden;
  return `<!doctype html>
<html lang="en"${p.theme ? ` data-theme="${p.theme}"` : ""}><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>${esc(s.title)} · SponsorJobs Admin</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap">
<style>${CSS}</style></head><body>
<div class="app${showRail ? " withrail" : ""}${p.sideHidden ? " noside" : ""}">
<aside class="side"><div class="sidein"><a class="brand" href="/"><span class="logo">S</span><span>SponsorJobs</span></a>${favs}${nav}
<div class="who">${esc(s.viewer)}</div></div></aside>
<div class="main">
<header class="top"><div class="hl">
<a class="hbtn" href="${pref(s, "toggle=side")}" title="${p.sideHidden ? "Show" : "Hide"} the sidebar" aria-label="${p.sideHidden ? "Show" : "Hide"} the sidebar">${icon("sidebar")}</a>
<a class="hbtn" href="${pref(s, `${starred ? "unfav" : "fav"}=1&label=${encodeURIComponent(s.title)}`)}" title="${starred ? "Remove from" : "Add to"} favorites" aria-label="${starred ? "Remove from" : "Add to"} favorites">${icon(starred ? "starOn" : "star")}</a>
<nav class="crumbs" aria-label="Breadcrumb">${crumbs}</nav></div>
<div class="hr"><form class="search" method="get" action="/users" role="search">${icon("search")}<input name="q" value="${esc(s.query ?? "")}" placeholder="Search" aria-label="Search users"><kbd>⌘K</kbd></form>
<div class="hicons">
<a class="hbtn" href="${pref(s, `theme=${nextTheme}`)}" title="Theme: ${p.theme || "system"} (switch to ${nextTheme})" aria-label="Switch theme to ${nextTheme}">${icon("sun")}</a>
<a class="hbtn" href="/audit" title="Audit log" aria-label="Audit log">${icon("history")}</a>
<a class="hbtn" href="/alerts" title="Alerts" aria-label="Alerts">${icon("bell")}${s.badges?.health ? '<span class="hdot"></span>' : ""}</a>
<a class="hbtn" href="${pref(s, "toggle=rail")}" title="${p.railHidden ? "Show" : "Hide"} the right sidebar" aria-label="${p.railHidden ? "Show" : "Hide"} the right sidebar">${icon("sidebar")}</a>
</div></div></header>
<main class="content">${flash}${s.body}
<footer class="foot">Design: ByeWind SnowUI (CC BY 4.0). Icons: Phosphor.</footer>
</main></div>
${showRail ? `<aside class="rail"><div class="railin">${s.rail}</div></aside>` : ""}
</div><script>${KEY_SCRIPT}</script></body></html>`;
}

/** The kit's ⌘K: focus the search box. The CSP allows exactly this script, by hash. */
export const KEY_SCRIPT = "document.addEventListener('keydown',function(e){if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();var i=document.querySelector('.search input');if(i){i.focus();i.select();}}});";

// ---- components --------------------------------------------------------------------------------- //

/** The page title row: the kit's 28-high Semibold 14 title, with optional controls on the right. */
export const pageTitle = (h: string, right = "") => `<div class="ptitle"><h1>${esc(h)}</h1>${right}</div>`;

/** The kit's "Today" menu: a small button that opens a list of links. */
export function menu(current: string, items: Array<{ label: string; href: string }>): string {
  return `<details class="menu"><summary class="tbtn">${esc(current)}${icon("caretDown")}</summary><div class="mlist">` +
    items.map((i) => `<a href="${esc(i.href)}"${i.label === current ? ' aria-current="true"' : ""}>${esc(i.label)}</a>`).join("") + `</div></details>`;
}

/** A KPI card (202x112 in the kit): label, number, and the change with the kit's arrow. */
export function kpi(label: string, value: string, o: { tone?: "a" | "b"; delta?: number | null } = {}): string {
  const d = o.delta;
  const delta = d === undefined || d === null || !Number.isFinite(d) ? "" :
    `<span class="delta">${d >= 0 ? "+" : ""}${d.toFixed(2)}%${trend(d >= 0)}</span>`;
  return `<div class="kpi ${o.tone ?? "a"}"><div class="klabel">${esc(label)}</div>` +
    `<div class="krow"><span class="kvalue">${esc(value)}</span>${delta}</div></div>`;
}

/** A block (the kit's #f9f9fa panel). `span` is the number of grid columns it takes (of 4). */
export const card = (title: string, body: string, o: { span?: 1 | 2 | 3 | 4; aside?: string; id?: string; head?: string } = {}) =>
  `<section class="block s${o.span ?? 4}"${o.id ? ` id="${o.id}"` : ""}><div class="bhead">${o.head ?? `<h2>${esc(title)}</h2>`}${o.aside ?? ""}</div>${body}</section>`;

/** A status: a coloured dot and a word (the kit's order-status style). */
export function badge(text: string, tone: "ok" | "warn" | "bad" | "plain" = "plain"): string {
  return `<span class="status"><span class="dot ${tone}"></span>${esc(text)}</span>`;
}

/** A thin meter in the kit's bar style. */
export function meter(percent: number, level: "ok" | "warn" | "bad" = "ok"): string {
  const p = Math.max(0, Math.min(100, percent));
  return `<div class="meter ${level}" role="img" aria-label="${p.toFixed(0)} percent"><span style="width:${p.toFixed(1)}%"></span></div>`;
}

/** A table in the kit's style: no rules, a 40-high grey header row, 52-high rows striped. */
export function table(head: string[], rows: string[][], empty: string): string {
  const th = head.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = rows.length ? rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")
    : `<tr><td colspan="${head.length}" class="muted">${esc(empty)}</td></tr>`;
  return `<div class="tablewrap"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
}

export function shortNum(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1).replace(/\.0$/, "")}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1).replace(/\.0$/, "")}K`;
  return String(Math.round(n));
}

/** The kit's axes run 0 and three equal steps ("0 10K 20K 30K"); the top is a round number. */
function steps3(max: number): number {
  const raw = Math.max(1, max) / 3;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw) ?? 10 * p;
  return step * 3;
}
const fmtTick = (v: number, unit: "" | "$") => (unit === "$" ? "$" : "") + shortNum(v);

/** Monotone cubic (Fritsch-Carlson): the kit's smooth line that never overshoots its points. */
function monotone(pts: Array<[number, number]>): string {
  const n = pts.length;
  if (!n) return "";
  if (n === 1) return `M${pts[0]![0]},${pts[0]![1]}`;
  const dx = pts.slice(1).map((p, i) => p[0] - pts[i]![0]);
  const m = pts.slice(1).map((p, i) => (p[1] - pts[i]![1]) / (dx[i] || 1));
  const t = pts.map((_, i) => i === 0 ? m[0]! : i === n - 1 ? m[n - 2]! : m[i - 1]! * m[i]! <= 0 ? 0 : (m[i - 1]! + m[i]!) / 2);
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i]! / m[i]!, b = t[i + 1]! / m[i]!, h = a * a + b * b;
    if (h > 9) { const k = 3 / Math.sqrt(h); t[i] = k * a * m[i]!; t[i + 1] = k * b * m[i]!; }
  }
  let d = `M${pts[0]![0].toFixed(1)},${pts[0]![1].toFixed(1)}`;
  for (let i = 0; i < n - 1; i++) {
    const [x0, y0] = pts[i]!, [x1, y1] = pts[i + 1]!, h = (x1 - x0) / 3;
    d += ` C${(x0 + h).toFixed(1)},${(y0 + t[i]! * h).toFixed(1)} ${(x1 - h).toFixed(1)},${(y1 - t[i + 1]! * h).toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)}`;
  }
  return d;
}

/** The kit's "Total Users" block: tabs, a "|", the legend, then the 246-high chart (y labels in
 * four 62-high rows, x labels in equal columns, the fading line over a soft shading, the dashed
 * comparison). `tabs` are links; the first `on` one is drawn Semibold. */
export function lineBlock(o: { tabs: Array<{ label: string; href: string; on: boolean }>; legend: [string, string];
                               labels: string[]; series: number[]; compare: number[]; unit?: "" | "$"; aria: string;
                               span?: 1 | 2 | 3 | 4; id?: string; cap?: number }): string {
  const W = 575, H = 246, TOP = 31, ZERO = 216;
  const max = steps3(Math.max(1, o.cap ?? 0, ...o.series, ...o.compare));
  const y = (v: number) => ZERO - (v / max) * (ZERO - TOP);
  const xs = (n: number) => (i: number) => (n <= 1 ? W / 2 : (i / (n - 1)) * W);
  const pts = (vals: number[]) => vals.map((v, i) => [xs(vals.length)(i), y(v)] as [number, number]);
  const main = monotone(pts(o.series));
  const cmp = o.compare.length ? monotone(pts(o.compare)) : "";
  const area = main ? `${main} L${W},${H} L0,${H} Z` : "";
  const cap = o.cap ? `<line class="cap" x1="0" x2="${W}" y1="${y(o.cap).toFixed(1)}" y2="${y(o.cap).toFixed(1)}"/>` : "";
  const ticks = [3, 2, 1, 0].map((k) => `<span>${fmtTick((k * max) / 3, o.unit ?? "")}</span>`).join("");
  const head = `<div class="ltabs">${o.tabs.map((t) => `<a class="ltab${t.on ? " on" : ""}" href="${esc(t.href)}">${esc(t.label)}</a>`).join("")}` +
    `<span class="lbar">|</span><span class="lgd"><span><span class="ld"></span>${esc(o.legend[0])}</span><span><span class="ld p1"></span>${esc(o.legend[1])}</span></span></div>`;
  const tips = o.labels.map((l, i) => `${l}: ${fmtTick(o.series[i] ?? 0, o.unit ?? "")}`).join("; ");
  const chart = `<div class="lchart"><div class="ly">${ticks}</div><div class="lplot">` +
    `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(o.aria)}"><title>${esc(tips)}</title><defs>` +
    `<radialGradient id="lshade" cx="0.5" cy="0" r="1"><stop offset="0" class="sh0"/><stop offset="1" class="sh1"/></radialGradient>` +
    `<linearGradient id="lfade" x1="0" y1="0" x2="1" y2="0"><stop offset="0" class="lf0"/><stop offset="1" class="lf1"/></linearGradient></defs>` +
    (area ? `<path class="area" d="${area}"/>` : "") + cap + (cmp ? `<path class="cmp" d="${cmp}"/>` : "") + (main ? `<path class="line" d="${main}"/>` : "") +
    `</svg><div class="lx">${o.labels.map((l) => `<span>${esc(l)}</span>`).join("")}</div></div></div>`;
  return `<section class="block s${o.span ?? 3}"${o.id ? ` id="${o.id}"` : ""}>${head}${chart}</section>`;
}

const PALETTE = ["p1", "p2", "p3", "p4", "p5", "p6"];

/** The kit's "Traffic by Device" and "Marketing & SEO" bars: y labels in four 49-high rows, one
 * 28-wide bar per column with a top radius of 8, labels under. */
export function barChart(points: Array<{ label: string; value: number; display?: string }>, o: { aria: string; unit?: "" | "$"; single?: boolean }): string {
  const max = steps3(Math.max(1, ...points.map((p) => p.value)));
  const ticks = [3, 2, 1, 0].map((k) => `<span>${fmtTick((k * max) / 3, o.unit ?? "")}</span>`).join("");
  const bars = points.map((p, i) => `<div class="bcol"><div class="bar ${o.single ? "p1" : PALETTE[i % PALETTE.length]}${p.value > 0 ? "" : " zero"}" style="height:${((100 * Math.max(0, p.value)) / max).toFixed(1)}%" ` +
    `title="${esc(p.label)}: ${esc(p.display ?? fmtTick(p.value, o.unit ?? ""))}"></div></div>`).join("");
  return `<div class="bchart" role="img" aria-label="${esc(o.aria)}"><div class="by">${ticks}</div><div class="bplot"><div class="bars">${bars}</div>` +
    `<div class="bx">${points.map((p) => `<span>${esc(p.label)}</span>`).join("")}</div></div></div>`;
}

/** An annular sector with the kit's rounded corners: a path inset by `round` and stroked with the
 * same colour and round joins, so every corner ends up with that radius. */
function sector(a0: number, a1: number, R: number, r: number, round: number): string {
  const R2 = R - round, r2 = r + round, cx = 60, cy = 60;
  const pt = (rad: number, a: number) => `${(cx + rad * Math.sin(a)).toFixed(2)},${(cy - rad * Math.cos(a)).toFixed(2)}`;
  const big = a1 - a0 > Math.PI ? 1 : 0;
  // Shrink the angles so the stroke does not grow the segment past its ends at either radius.
  const aO0 = a0 + round / R2, aO1 = a1 - round / R2, aI0 = a0 + round / r2, aI1 = a1 - round / r2;
  if (aO1 <= aO0 || aI1 <= aI0) return "";
  return `M${pt(R2, aO0)} A${R2},${R2} 0 ${big} 1 ${pt(R2, aO1)} L${pt(r2, aI1)} A${r2},${r2} 0 ${big} 0 ${pt(r2, aI0)} Z`;
}

/** The kit's "Traffic by Location": a 120 ring (outer 60, inner 30) of rounded segments with gaps,
 * clockwise from the top, and the legend (dot, label, share) in rows 32 apart. */
export function donut(parts: Array<{ label: string; value: number }>, o: { aria: string }): string {
  const DONUT = ["p3", "p4", "p6", "p1", "p5", "p2"];
  const total = parts.reduce((a, p) => a + p.value, 0);
  const GAP = 0.07;                                        // radians between segments (about 4px mid-ring)
  const live = parts.filter((p) => p.value > 0);
  let a = 0;
  const segs = total <= 0 ? `<circle cx="60" cy="60" r="45" class="ringempty"/>` : parts.map((p, i) => {
    const span = (p.value / total) * 2 * Math.PI;
    const a0 = a + (live.length > 1 ? GAP / 2 : 0), a1 = a + span - (live.length > 1 ? GAP / 2 : 0);
    a += span;
    if (p.value <= 0) return "";
    const d = live.length === 1 ? "" : sector(a0, a1, 60, 30, 3);
    return d ? `<path class="seg ${DONUT[i % DONUT.length]}" d="${d}"><title>${esc(p.label)}: ${num(p.value)}</title></path>`
      : live.length === 1 ? `<circle cx="60" cy="60" r="45" class="ringfull ${DONUT[i % DONUT.length]}"><title>${esc(p.label)}: ${num(p.value)}</title></circle>` : "";
  }).join("");
  const rows = parts.map((p, i) => `<li><span class="ldot"><span class="ld ${DONUT[i % DONUT.length]}"></span></span><span class="ll">${esc(p.label)}</span>` +
    `<span class="lv">${total ? ((100 * p.value) / total).toFixed(1) : "0.0"}%</span></li>`).join("");
  return `<div class="donut"><svg width="120" height="120" viewBox="0 0 120 120" role="img" aria-label="${esc(o.aria)}">${segs}</svg><ul class="dleg">${rows}</ul></div>`;
}

/** The kit's "Traffic by Website" list: a 57 label, then three 2-high segments (100%, 40%, 10%) whose
 * width follows the value. Rows 43 apart. The value shows on hover. */
export function siteBars(rows: Array<{ label: string; value: number; display?: string; href?: string }>, empty: string): string {
  if (!rows.length) return `<p class="muted small">${esc(empty)}</p>`;
  const max = Math.max(1, ...rows.map((r) => r.value));
  return `<ul class="sbars">${rows.map((r) => {
    const w = Math.max(8, Math.round((25 * r.value) / max));
    const seg = (cls: string) => `<span class="${cls}" style="width:${w}px"></span>`;
    return `<li title="${esc(r.label)}: ${esc(r.display ?? num(r.value))}"><span class="sl">${r.href ? `<a href="${r.href}">${esc(r.label)}</a>` : esc(r.label)}</span>` +
      `<span class="st">${seg("s1")}${seg("s2")}${seg("s3")}</span></li>`;
  }).join("")}</ul>`;
}

/** A wider list for full pages: label, the same three-tone bar scaled to the row, and the value. */
export function hbars(rows: Array<{ label: string; value: number; display?: string; href?: string }>, empty: string): string {
  if (!rows.length) return `<p class="muted small">${esc(empty)}</p>`;
  const max = Math.max(1, ...rows.map((r) => r.value));
  return `<ul class="hbars">${rows.map((r) => {
    const w = Math.max(2, (100 * r.value) / max) / 3;
    return `<li><span class="sl">${r.href ? `<a href="${r.href}">${esc(r.label)}</a>` : esc(r.label)}</span>` +
      `<span class="st">${["s1", "s2", "s3"].map((c) => `<span class="${c}" style="width:${w.toFixed(1)}%"></span>`).join("")}</span>` +
      `<span class="sv">${esc(r.display ?? num(r.value))}</span></li>`;
  }).join("")}</ul>`;
}

/** A short list in the kit's legend style: a 12px label in 40% and its value on the right. */
export const mini = (rows: Array<[string, string]>) =>
  `<ul class="dleg mini">${rows.map(([k, v]) => `<li><span class="ll muted">${esc(k)}</span><span class="lv">${v}</span></li>`).join("")}</ul>`;

/** A labelled field list (label left in 40%, value right), as on a person's page. */
export const facts = (rows: Array<[string, string]>) =>
  `<dl class="facts">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;

/** The right sidebar's three lists, as in the kit: "Notifications" (a 24 icon tile), "Activities"
 * (round avatars joined by a line) and "Contacts" (round avatars). */
export function railList(title: string, items: Array<{ icon?: string; initial?: string; text: string; sub?: string; href?: string; tone?: "a" | "b" | "bad" | "warn" }>,
                         o: { kind: "notifications" | "activities" | "contacts"; empty?: string; id?: string }): string {
  const list = items.length ? items.map((it, i) => {
    const av = o.kind === "notifications"
      ? `<span class="rt ${it.tone ?? (i % 2 ? "b" : "a")}">${icon(it.icon ?? "bug")}</span>`
      : `<span class="av ${PALETTE[(i + 1) % PALETTE.length]}">${it.initial ? esc(it.initial) : icon(it.icon ?? "user")}</span>`;
    return `<li class="ri ${o.kind}${o.kind === "activities" && i < items.length - 1 ? " tl" : ""}">${av}` +
      `<span class="rx"><span class="r1">${it.href ? `<a href="${it.href}">${esc(it.text)}</a>` : esc(it.text)}</span>` +
      (it.sub ? `<span class="r2">${esc(it.sub)}</span>` : "") + `</span></li>`;
  }).join("") : `<li class="ri"><span class="rx"><span class="r2">${esc(o.empty ?? "Nothing yet")}</span></span></li>`;
  return `<section class="rsec"${o.id ? ` id="${o.id}"` : ""}><h2>${esc(title)}</h2><ul>${list}</ul></section>`;
}

// ---- CSS ---------------------------------------------------------------------------------------- //

const DARK = `--bg:#333;--t1:#fff;--t2:rgba(255,255,255,.4);--t3:rgba(255,255,255,.2);--ln:rgba(255,255,255,.15);
--fill:rgba(255,255,255,.1);--block:rgba(255,255,255,.04);--stripe:rgba(255,255,255,.04);--p3:rgba(255,255,255,.4);
--sh:rgba(255,255,255,.10);--l0:rgba(255,255,255,.4);--l1:rgba(255,255,255,.4);--menu:#3d3d3d;--p3s:#8a8a8a;color-scheme:dark`;

const CSS = `
:root{--bg:#fff;--t1:#000;--n1:#1c1c1c;--t2:rgba(0,0,0,.4);--t3:rgba(0,0,0,.2);--ln:rgba(0,0,0,.1);--fill:rgba(0,0,0,.04);
--block:#f9f9fa;--stripe:rgba(0,0,0,.02);--ka:#edeefc;--kb:#e6f1fd;
--p1:#a0bce8;--p2:#6be6d3;--p3:#000;--p4:#33d1ff;--p5:#b899eb;--p6:#71dd8c;--sh:rgba(0,0,0,.10);--l0:rgba(0,0,0,.4);--l1:#000;--menu:#fff;--p3s:#000;
--ok:#71dd8c;--warn:#ffc555;--bad:#ff4747;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){${DARK}}}
:root[data-theme="dark"]{${DARK}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--t1)}
body{font:400 14px/20px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--p1);outline-offset:1px}
ul{list-style:none;margin:0;padding:0}h1,h2,h3{margin:0}
.app{display:grid;grid-template-columns:212px minmax(0,1fr);min-height:100vh}
.app.withrail{grid-template-columns:212px minmax(0,1fr) 280px}
.app.noside{grid-template-columns:minmax(0,1fr)}.app.noside.withrail{grid-template-columns:minmax(0,1fr) 280px}.app.noside .side{display:none}
.ic{flex:none;display:block}
.side{border-right:1px solid var(--ln)}.sidein{padding:16px;display:flex;flex-direction:column;gap:8px;position:sticky;top:0;height:100vh;overflow-y:auto}
.group{display:flex;flex-direction:column;gap:4px;padding-bottom:12px}
.brand{display:flex;align-items:center;gap:8px;height:40px;padding:8px;border-radius:8px;margin-bottom:4px}
.logo{width:24px;height:24px;border-radius:80px;background:var(--t1);color:var(--bg);display:grid;place-items:center;font-size:12px;font-weight:600}
.ftabs{display:flex;gap:8px;height:24px}
.ftab{padding:4px 12px;border-radius:12px;font-size:12px;line-height:16px;color:var(--t3)}.ftab.on{color:var(--t2)}.ftab:hover{background:var(--fill)}
.glabel{height:28px;padding:4px 12px;color:var(--t2)}
.nav{display:flex;align-items:center;gap:4px;height:36px;padding:8px;border-radius:12px;cursor:pointer;list-style:none}
.nav::-webkit-details-marker{display:none}
.nav:hover,.nav.on{background:var(--fill)}
.slot{width:16px;height:16px;flex:none;display:grid;place-items:center;color:var(--t3)}
.navd[open]>summary .caret svg{transform:rotate(90deg)}
.fdot{width:6px;height:6px;border-radius:50%;background:var(--t3)}
.nav.fav{gap:4px}.nav.fav.empty{color:var(--t3);cursor:default}.nav.fav.empty:hover{background:none}
.it{display:flex;align-items:center;gap:8px;min-width:0}.it span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nav.subi{padding-left:56px}
.nbadge{margin-left:auto;font-size:12px;line-height:16px;color:var(--bad)}
.who{margin-top:auto;color:var(--t2);font-size:12px;line-height:16px;padding:8px;overflow-wrap:anywhere}
.main{min-width:0;display:flex;flex-direction:column}
.top{height:68px;padding:20px 28px;border-bottom:1px solid var(--ln);display:flex;align-items:center;justify-content:space-between;gap:20px}
.hl,.hr{display:flex;align-items:center;gap:8px;min-width:0}.hr{gap:20px}
.hicons{display:flex;gap:8px}
.hbtn{width:24px;height:24px;padding:4px;border-radius:8px;display:grid;place-items:center;flex:none;position:relative}.hbtn:hover{background:var(--fill)}
.hdot{position:absolute;top:2px;right:2px;width:6px;height:6px;border-radius:50%;background:var(--bad)}
.crumbs{display:flex;align-items:center;gap:8px;min-width:0;white-space:nowrap;overflow:hidden;margin-left:8px}
.crumb{padding:4px 12px;font-size:12px;line-height:16px;color:var(--t2);border-radius:12px}a.crumb:hover{background:var(--fill)}.crumb.now{color:var(--t1)}
.sep{color:var(--ln)}
.search{display:flex;align-items:center;gap:8px;height:28px;width:160px;padding:4px 8px;border-radius:16px;background:var(--fill);color:var(--t3)}
.search input{border:0;background:transparent;color:var(--t1);font:inherit;outline:none;width:100%;min-width:0;padding:0}
.search input::placeholder{color:var(--t3)}
.search kbd{font:inherit;font-size:12px;line-height:16px;color:var(--t3);border:1px solid var(--ln);background:var(--fill);border-radius:6px;padding:0 4px;height:16px;display:inline-flex;align-items:center}
.content{padding:28px;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:28px;align-content:start}
.content>*{grid-column:1/-1}
.ptitle{display:flex;align-items:center;justify-content:space-between;gap:12px;height:28px;margin-bottom:-12px}
.ptitle h1{font-size:14px;line-height:20px;font-weight:600;padding:4px 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.menu{position:relative}.menu summary{list-style:none}.menu summary::-webkit-details-marker{display:none}
.tbtn{display:inline-flex;align-items:center;gap:4px;height:24px;padding:4px 8px;border-radius:8px;font-size:12px;line-height:16px;cursor:pointer}.tbtn:hover{background:var(--fill)}
.mlist{position:absolute;right:0;top:28px;z-index:5;background:var(--menu);border:1px solid var(--ln);border-radius:12px;padding:4px;min-width:140px;display:flex;flex-direction:column;box-shadow:0 8px 24px rgba(0,0,0,.08)}
.mlist a{padding:6px 8px;border-radius:8px;font-size:12px;line-height:16px}.mlist a:hover,.mlist a[aria-current]{background:var(--fill)}
.kpi{grid-column:span 1;border-radius:20px;padding:24px;display:flex;flex-direction:column;gap:8px;color:#000;min-height:112px;min-width:0}
.kpi.a{background:var(--ka)}.kpi.b{background:var(--kb)}
.krow{display:flex;align-items:center;justify-content:space-between;gap:8px;min-width:0}
.kvalue{font-size:24px;line-height:36px;font-weight:600;color:var(--n1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.delta{display:inline-flex;align-items:center;gap:4px;font-size:12px;line-height:16px;white-space:nowrap}
.block{background:var(--block);border-radius:20px;padding:24px;display:flex;flex-direction:column;gap:16px;min-width:0}
.block.s1{grid-column:span 1}.block.s2{grid-column:span 2}.block.s3{grid-column:span 3}.block.s4{grid-column:1/-1}
.bhead{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:20px;flex-wrap:wrap}.bhead form{margin:-4px 0}
.block h2{font-size:14px;line-height:20px;font-weight:600}
.muted{color:var(--t2)}.small{font-size:12px;line-height:16px}
.ltabs{display:flex;align-items:center;gap:16px;min-height:20px;flex-wrap:wrap}
.ltab{color:var(--t1);opacity:.4}.ltab.on{opacity:1;font-weight:600}.ltab:hover{opacity:1}
.lbar{color:var(--t3)}
.lgd{display:inline-flex;gap:16px;font-size:12px;line-height:16px;align-items:center}.lgd>span{display:inline-flex;align-items:center;gap:4px;padding:2px 8px 2px 4px}
.ld{width:6px;height:6px;border-radius:50%;display:inline-block;flex:none;background:var(--p3);margin:0 3px}
.ld.p1{background:var(--p1)}.ld.p2{background:var(--p2)}.ld.p3{background:var(--p3)}.ld.p4{background:var(--p4)}.ld.p5{background:var(--p5)}.ld.p6{background:var(--p6)}
.lchart{display:flex;gap:16px;height:246px}
.ly{width:23px;display:flex;flex-direction:column;flex:none}.ly span{height:62px;display:flex;align-items:center;justify-content:flex-end;font-size:12px;line-height:16px;color:var(--t2)}
.ly span:last-child{height:60px}
.lplot{position:relative;flex:1;min-width:0}.lplot svg{width:100%;height:100%;display:block;overflow:visible}
.lplot .line{fill:none;stroke:url(#lfade);stroke-width:1.5;vector-effect:non-scaling-stroke;stroke-linecap:round}
.lplot .cmp{fill:none;stroke:var(--p1);stroke-width:1.5;stroke-dasharray:2 4;vector-effect:non-scaling-stroke;stroke-linecap:round}
.lplot .cap{stroke:var(--t3);stroke-dasharray:4 4;vector-effect:non-scaling-stroke}
.lplot .area{fill:url(#lshade)}.sh0{stop-color:var(--sh)}.sh1{stop-color:var(--sh);stop-opacity:0}.lf0{stop-color:var(--l0)}.lf1{stop-color:var(--l1)}
.lx{position:absolute;left:0;right:0;bottom:0;height:16px;display:flex}.lx span{flex:1;text-align:center;font-size:12px;line-height:16px;color:var(--t2)}
.bchart{display:flex;gap:16px;height:196px}
.by{width:23px;display:flex;flex-direction:column;flex:none}.by span{height:49px;display:flex;align-items:center;justify-content:flex-end;font-size:12px;line-height:16px;color:var(--t2)}
.bplot{flex:1;min-width:0;display:flex;flex-direction:column}
.bars{height:168px;display:flex;align-items:flex-end;margin-top:8px}
.bcol{flex:1;display:flex;justify-content:center;align-items:flex-end;height:100%;min-width:0}
.bar{width:28px;max-width:80%;border-radius:8px 8px 0 0;min-height:2px}.bar.zero{min-height:0}
.bar.p1{background:var(--p1)}.bar.p2{background:var(--p2)}.bar.p3{background:var(--p3)}.bar.p4{background:var(--p4)}.bar.p5{background:var(--p5)}.bar.p6{background:var(--p6)}
.bx{display:flex;height:16px;margin-top:4px}.bx span{flex:1;text-align:center;font-size:12px;line-height:16px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.donut{display:flex;align-items:center;gap:40px;padding:38px 20px 38px 20px;min-width:0}
.donut svg{flex:none}.seg.p1{fill:var(--p1);stroke:var(--p1)}.seg.p2{fill:var(--p2);stroke:var(--p2)}.seg.p3{fill:var(--p3s);stroke:var(--p3s)}.seg.p4{fill:var(--p4);stroke:var(--p4)}.seg.p5{fill:var(--p5);stroke:var(--p5)}.seg.p6{fill:var(--p6);stroke:var(--p6)}
.seg{stroke-width:6;stroke-linejoin:round}
.ringempty{fill:none;stroke:var(--fill);stroke-width:30}.ringfull{fill:none;stroke-width:30}.ringfull.p3{stroke:var(--p3s)}.ringfull.p4{stroke:var(--p4)}.ringfull.p6{stroke:var(--p6)}.ringfull.p1{stroke:var(--p1)}
.dleg{display:flex;flex-direction:column;gap:16px;flex:1;min-width:0;font-size:12px;line-height:16px}
.dleg li{display:flex;align-items:center}.dleg.mini{gap:20px;padding-top:4px}.dleg.mini .lv{text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:65%}.ldot{width:12px;height:12px;display:grid;place-items:center;flex:none}.ldot .ld{margin:0}
.ll{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.dleg .lv{margin-left:12px}
.sbars{display:flex;flex-direction:column;gap:27px;padding-top:8px}
.sbars li{display:grid;grid-template-columns:57px minmax(0,1fr);align-items:center;gap:16px;font-size:12px;line-height:16px}
.sl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.sl a:hover{text-decoration:underline}
.st{display:flex;gap:2px;align-items:center;height:2px;min-width:0}.st span{height:2px;border-radius:80px;display:block;flex:none}
.st .s1{background:var(--t1)}.st .s2{background:var(--t2)}.st .s3{background:var(--ln)}
.hbars{display:flex;flex-direction:column;gap:20px}
.hbars li{display:grid;grid-template-columns:minmax(0,120px) minmax(40px,1fr) auto;align-items:center;gap:16px;font-size:12px;line-height:16px}
.hbars .st span{flex:none}.sv{color:var(--t2);text-align:right;white-space:nowrap}
.meter{height:4px;background:var(--fill);border-radius:80px;overflow:hidden;margin-top:8px}.meter span{display:block;height:100%;background:var(--t1);border-radius:80px}
.meter.warn span{background:var(--warn)}.meter.bad span{background:var(--bad)}
.status{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;font-size:12px;line-height:16px}
.dot{width:6px;height:6px;border-radius:50%;display:inline-block;flex:none;background:var(--t3)}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
.tablewrap{overflow-x:auto;margin:0 -8px}
table{width:100%;border-collapse:separate;border-spacing:0 4px}
th{text-align:left;font-weight:400;font-size:12px;line-height:16px;color:var(--t2);padding:12px 16px;white-space:nowrap}
td{padding:16px;line-height:20px;vertical-align:middle;white-space:nowrap}td.wrap{white-space:normal}
tbody tr:nth-child(odd) td{background:var(--stripe)}
tbody tr:hover td{background:var(--fill)}
td a{text-decoration:underline;text-decoration-color:var(--t3);text-underline-offset:3px}td a:hover{text-decoration-color:var(--t1)}
td:first-child{overflow-wrap:anywhere}
.wrapc{white-space:normal;display:block;max-width:28ch}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.flash{display:flex;align-items:center;gap:8px;border-radius:12px;padding:8px 12px;background:var(--fill)}
.filters{display:flex;gap:4px;flex-wrap:wrap;align-items:center}
.chip{border-radius:8px;padding:4px 8px;font-size:12px;line-height:16px;color:var(--t2)}.chip:hover{background:var(--fill)}.chip.on{background:var(--fill);color:var(--t1)}
.fsep{width:1px;height:16px;background:var(--ln);margin:0 8px}
.pager{display:flex;gap:8px;justify-content:flex-end;align-items:center}
.btn{font:inherit;font-size:12px;line-height:16px;border:0;background:var(--fill);color:var(--t1);border-radius:8px;padding:6px 12px;cursor:pointer;display:inline-flex;align-items:center;gap:4px;white-space:nowrap}
.btn:hover{background:var(--ln)}.btn.primary{background:var(--t1);color:var(--bg)}.btn.danger{color:var(--bad)}
.act{display:flex;flex-direction:column;gap:8px}.act+.act,.act.sep{border-top:1px solid var(--ln);padding-top:16px}
.act h3{font-size:12px;line-height:16px;font-weight:400;color:var(--t2)}.act .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.act select,.act input[type=text]{font:inherit;color:var(--t1);background:var(--fill);border:0;border-radius:8px;height:28px;padding:4px 8px}
.act input[type=text]{flex:1;min-width:160px}.act input::placeholder{color:var(--t3)}
.facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:12px 24px;margin:0}.facts dt{color:var(--t2)}.facts dd{margin:0;overflow-wrap:anywhere}
.rail{border-left:1px solid var(--ln)}.railin{padding:16px;display:flex;flex-direction:column;gap:16px;position:sticky;top:0;height:100vh;overflow-y:auto}
.rsec{display:flex;flex-direction:column;gap:4px}
.rsec h2{font-size:14px;line-height:20px;font-weight:400;padding:8px 4px}
.ri{display:flex;gap:8px;padding:8px;border-radius:12px;position:relative;min-width:0}
.ri.contacts{align-items:center}
.rt{width:24px;height:24px;border-radius:8px;display:grid;place-items:center;flex:none;color:#000}
.rt.a{background:var(--ka)}.rt.b{background:var(--kb)}.rt.bad{background:#ffe5e5}.rt.warn{background:#fff3d6}
.av{width:24px;height:24px;border-radius:80px;display:grid;place-items:center;flex:none;color:#000;font-size:12px;line-height:16px;font-weight:600}
.av.p1{background:var(--p1)}.av.p2{background:var(--p2)}.av.p3{background:var(--ka)}.av.p4{background:var(--p4)}.av.p5{background:var(--p5)}.av.p6{background:var(--p6)}
.ri.tl::after{content:"";position:absolute;left:19.5px;top:36px;width:1px;height:16px;background:var(--ln)}
.rx{display:flex;flex-direction:column;min-width:0}.r1{overflow-wrap:anywhere}.r1 a:hover{text-decoration:underline}.r2{font-size:12px;line-height:16px;color:var(--t2)}
.foot{color:var(--t2);font-size:12px;line-height:16px}
details.more summary{cursor:pointer;color:var(--t2);font-size:12px;line-height:16px}
@media (max-width:1280px){.app.withrail{grid-template-columns:212px minmax(0,1fr)}.app.noside.withrail{grid-template-columns:minmax(0,1fr)}.rail{grid-column:1/-1;border-left:0;border-top:1px solid var(--ln)}.railin{position:static;height:auto;padding:28px}}
@media (max-width:1100px){.content{grid-template-columns:repeat(2,minmax(0,1fr))}.block.s3,.block.s2{grid-column:1/-1}.block.s1{grid-column:span 1}.hicons{display:none}}
@media (max-width:760px){.app,.app.withrail{grid-template-columns:minmax(0,1fr)}
.side{border-right:0;border-bottom:1px solid var(--ln)}.sidein{position:static;height:auto;flex-direction:row;flex-wrap:wrap;gap:4px;padding:12px 16px}
.sidein .group{flex-direction:row;flex-wrap:wrap;padding:0}.glabel,.who,.slot,.ftabs,.nav.fav,.nav.subi{display:none}.navd{display:contents}
.rail{grid-column:1}.railin{padding:20px 16px}.top{padding:16px;height:auto;flex-wrap:wrap}.hr{width:100%}.search{width:100%}.content{padding:20px 16px;gap:16px;grid-template-columns:minmax(0,1fr)}
.kpi,.block.s1{grid-column:1/-1}.donut{padding:16px 0;gap:24px;flex-wrap:wrap}}
`;
