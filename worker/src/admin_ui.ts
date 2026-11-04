// The admin dashboard's look: the shell (sidebar, header, optional right sidebar), the CSS and the
// components every page is built from. Server-rendered HTML, no script.
//
// Built to ByeWind's free "Dashboard UI Kit" (SnowUI), Figma Community,
// https://www.figma.com/community/file/1210542873091115123, CC BY 4.0, credited in the footer.
// Every size, colour and type style below was read from that file's "Dashboard Overview" frames
// (light 549:8646, dark 54869:11584) through Figma's plugin API, not estimated from a picture:
//   frame 1440: sidebar 212 (padding 16), header 68 (padding 20 28), right sidebar 280 (padding 16)
//   content: padding 28; title row 28 high (padding 4 8, Semibold 14/20); 16 below it the grid,
//            gap 28; KPI cards 202x112, radius 20, padding 24; blocks radius 20, padding 24, gap 16
//   type: Inter. Regular 14/20 and 12/16, Semibold 14/20 (block titles), Semibold 24/36 (numbers)
//   text: #000 at 100% / 40% / 20%; lines #000 at 10% (dark: #fff, 40%, 20%, lines 15%)
//   fills: KPI cards #edeefc and #e6f1fd (same in dark, with black text); blocks #f9f9fa (dark
//          #fff at 4%); nav active and search #000 at 4% (dark #fff at 10%); table stripes #000 2%
//   nav item 36 high, padding 8, radius 12; group label 28 high, padding 4 12, Regular 14 at 40%
//   search 160x28, radius 16; table header 40 high (Regular 12/16 at 40%), rows 52, padding 12 16
//   charts: #a0bce8 #6be6d3 #000 (dark #fff 40%) #33d1ff #b899eb #71dd8c; bars 28 wide, top radius 8;
//           the main line solid, the comparison line #a0bce8 dashed 2 4 with round caps
// Icons are Phosphor (src/admin_icons.ts), the kit's own set.

import { PHOSPHOR, TREND_DOWN, TREND_UP } from "./admin_icons";

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
  if (d < 3600) return `${Math.floor(d / 60)} minutes ago`;
  if (d < 86_400) return `${Math.floor(d / 3600)} hours ago`;
  return `${Math.floor(d / 86_400)} days ago`;
}

export const PASS_LABEL: Record<string, string> = { free: "Free", pass30: "Job Hunt Pass", pass90: "Season Pass" };

// ---- icons -------------------------------------------------------------------------------------- //

/** A Phosphor icon at `size` px (20 in the sidebar, 16 elsewhere), drawn in currentColor. */
export const icon = (name: string, size = 16) =>
  `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true">${PHOSPHOR[name] ?? ""}</svg>`;
const trend = (up: boolean) =>
  `<svg class="ic" width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="${up ? TREND_UP : TREND_DOWN}"/></svg>`;

// ---- the shell ---------------------------------------------------------------------------------- //

export type PageId = "overview" | "users" | "revenue" | "usage" | "health" | "audit";

const NAV: Array<{ group: string; items: Array<{ id: PageId; label: string; href: string }> }> = [
  { group: "Dashboards", items: [
    { id: "overview", label: "Overview", href: "/" },
    { id: "users", label: "Users", href: "/users" },
    { id: "revenue", label: "Revenue", href: "/revenue" },
    { id: "usage", label: "Usage & costs", href: "/usage" },
  ] },
  { group: "System", items: [
    { id: "health", label: "Health", href: "/health" },
    { id: "audit", label: "Audit log", href: "/audit" },
  ] },
];

export interface Shell {
  page: PageId;
  title: string;               // the page title and the breadcrumb's last part
  crumbs?: Array<{ label: string; href?: string }>;   // between the group and the title
  viewer: string;              // the Access e-mail
  generatedAt: number;
  body: string;
  rail?: string;               // the right sidebar (overview only)
  flash?: { kind: "ok" | "bad"; text: string } | null;
  query?: string;              // keeps the search box filled on the Users page
  badges?: Partial<Record<PageId, string>>;   // e.g. a count of problems beside Health
}

export function shell(s: Shell): string {
  const nav = NAV.map((g) => `<div class="group"><div class="glabel">${esc(g.group)}</div>` +
    g.items.map((it) => {
      const on = it.id === s.page;
      return `<a class="nav${on ? " on" : ""}" href="${it.href}"${on ? ' aria-current="page"' : ""}><span class="slot"></span>` +
        `<span class="it">${icon(it.id === "overview" && on ? "overviewOn" : it.id, 20)}<span>${esc(it.label)}</span></span>` +
        (s.badges?.[it.id] ? `<span class="nbadge">${esc(s.badges[it.id])}</span>` : "") + `</a>`;
    }).join("") + `</div>`).join("");
  const group = NAV.find((g) => g.items.some((i) => i.id === s.page))?.group ?? "Dashboards";
  const crumbs = [{ label: group }, ...(s.crumbs ?? []), { label: s.title }]
    .map((c: { label: string; href?: string }, i, all) => i === all.length - 1 ? `<span class="crumb now">${esc(c.label)}</span>`
      : c.href ? `<a class="crumb" href="${c.href}">${esc(c.label)}</a>` : `<span class="crumb">${esc(c.label)}</span>`)
    .join('<span class="sep">/</span>');
  const flash = s.flash ? `<div class="flash ${s.flash.kind}" role="status"><span class="dot ${s.flash.kind === "ok" ? "ok" : "bad"}"></span>${esc(s.flash.text)}</div>` : "";
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>${esc(s.title)} · SponsorJobs Admin</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap">
<style>${CSS}</style></head><body>
<div class="app${s.rail ? " withrail" : ""}">
<aside class="side"><div class="group"><a class="brand" href="/"><span class="logo">S</span><span>SponsorJobs</span></a></div>${nav}
<div class="who">${esc(s.viewer)}</div></aside>
<div class="main">
<header class="top"><nav class="crumbs" aria-label="Breadcrumb">${crumbs}</nav>
<form class="search" method="get" action="/users" role="search">${icon("search")}<input name="q" value="${esc(s.query ?? "")}" placeholder="Search" aria-label="Search users"></form></header>
<main class="content">${flash}${s.body}
<footer class="foot">Design: ByeWind SnowUI (CC BY 4.0), Phosphor icons. Costs are estimates. ${esc(date(s.generatedAt))}</footer>
</main></div>
${s.rail ? `<aside class="rail">${s.rail}</aside>` : ""}
</div></body></html>`;
}

// ---- components --------------------------------------------------------------------------------- //

/** The page title row: the kit's 28-high Semibold 14 title, with optional controls on the right. */
export const pageTitle = (h: string, right = "") => `<div class="ptitle"><h1>${esc(h)}</h1>${right}</div>`;

/** A KPI card (202x112 in the kit): label, number, and an optional change with the kit's arrow. */
export function kpi(label: string, value: string, o: { tone?: "a" | "b"; delta?: number | null } = {}): string {
  const d = o.delta;
  const delta = d === undefined || d === null || !Number.isFinite(d) ? "" :
    `<span class="delta">${d >= 0 ? "+" : ""}${d.toFixed(2)}%${trend(d >= 0)}</span>`;
  return `<div class="kpi ${o.tone ?? "a"}"><div class="klabel">${esc(label)}</div>` +
    `<div class="krow"><span class="kvalue">${esc(value)}</span>${delta}</div></div>`;
}

/** A block (the kit's #f9f9fa panel). `span` is the number of grid columns it takes (of 4). */
export const card = (title: string, body: string, o: { span?: 1 | 2 | 3 | 4; aside?: string; id?: string } = {}) =>
  `<section class="block s${o.span ?? 4}"${o.id ? ` id="${o.id}"` : ""}><div class="bhead"><h2>${esc(title)}</h2>${o.aside ?? ""}</div>${body}</section>`;

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
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}K`;
  return String(Math.round(n));
}

/** Three y-axis labels (max, middle, 0) and the x labels (first, middle, last), as HTML so they
 * keep the kit's 12px whatever the chart's width. */
function axes(max: number, xs: string[]): { y: string; x: string } {
  const y = `<div class="yax"><span>${shortNum(max)}</span><span>${shortNum(max / 2)}</span><span>0</span></div>`;
  const x = xs.length ? `<div class="xax"><span>${esc(xs[0]!)}</span>${xs.length > 2 ? `<span>${esc(xs[Math.floor((xs.length - 1) / 2)]!)}</span>` : ""}` +
    `${xs.length > 1 ? `<span>${esc(xs[xs.length - 1]!)}</span>` : ""}</div>` : "";
  return { y, x };
}
const niceMax = (v: number) => {
  if (v <= 4) return 4;
  const p = 10 ** Math.floor(Math.log10(v));
  return Math.ceil(v / (p / 2)) * (p / 2);
};

/** The kit's "Total Users" line chart: a solid line with a soft fill, an optional dashed
 * comparison line (#a0bce8), HTML axes. The SVG stretches; strokes do not scale. */
export function lineChart(series: { label: string; points: Array<{ label: string; value: number }> },
                          o: { aria: string; compare?: { label: string; points: number[] }; cap?: number; unit?: string }): string {
  const pts = series.points;
  const max = niceMax(Math.max(1, o.cap ?? 0, ...pts.map((p) => p.value), ...(o.compare?.points ?? [])));
  const W = 1000, H = 200;
  const path = (vals: number[]) => {
    const xy = vals.map((v, i) => [vals.length <= 1 ? W / 2 : (i / (vals.length - 1)) * W, H - (v / max) * H] as const);
    let d = "";
    xy.forEach((p, i) => {
      if (i === 0) { d = `M${p[0].toFixed(1)},${p[1].toFixed(1)}`; return; }
      const p0 = xy[i - 2] ?? xy[i - 1]!, p1 = xy[i - 1]!, p3 = xy[i + 1] ?? p;
      const c1 = [p1[0] + (p[0] - p0[0]) / 6, Math.min(H, p1[1] + (p[1] - p0[1]) / 6)];
      const c2 = [p[0] - (p3[0] - p1[0]) / 6, Math.min(H, p[1] - (p3[1] - p1[1]) / 6)];
      d += ` C${c1[0]!.toFixed(1)},${c1[1]!.toFixed(1)} ${c2[0]!.toFixed(1)},${c2[1]!.toFixed(1)} ${p[0].toFixed(1)},${p[1].toFixed(1)}`;
    });
    return d;
  };
  const main = path(pts.map((p) => p.value));
  const area = pts.length ? `${main} L${W},${H} L0,${H} Z` : "";
  const cmp = o.compare ? `<path class="cmp" d="${path(o.compare.points)}"/>` : "";
  const cap = o.cap ? `<line class="cap" x1="0" x2="${W}" y1="${H - (o.cap / max) * H}" y2="${H - (o.cap / max) * H}"/>` : "";
  const { y, x } = axes(max, pts.map((p) => p.label.slice(5)));
  const tips = pts.map((p) => `${p.label}: ${num(p.value)}${o.unit ? " " + o.unit : ""}`).join("; ");
  return `<div class="chart"><div class="plotrow">${y}<div class="plot">` +
    `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(o.aria)}"><title>${esc(tips)}</title>` +
    `<defs><linearGradient id="lf" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="lf0"/><stop offset="1" class="lf1"/></linearGradient></defs>` +
    (area ? `<path class="area" d="${area}"/>` : "") + cap + cmp + (main ? `<path class="line" d="${main}"/>` : "") +
    `</svg></div></div>${x}</div>`;
}

/** The legend the kit puts beside a chart title: "• This year  • Last year". */
export const legend = (items: Array<{ label: string; cls: string }>) =>
  `<span class="lgd">${items.map((i) => `<span><span class="ld ${i.cls}"></span>${esc(i.label)}</span>`).join("")}</span>`;

const PALETTE = ["p1", "p2", "p3", "p4", "p5", "p6"];
/** The kit's "Traffic by Location" order: black, #33d1ff, #71dd8c, #a0bce8. */
const DONUT = ["p3", "p4", "p6", "p1", "p5", "p2"];

/** The kit's "Traffic by Device" bars: one 28-wide bar per category, top radius 8, palette colours. */
export function barChart(points: Array<{ label: string; value: number; display?: string }>, o: { aria: string; unit?: string; single?: boolean }): string {
  const max = niceMax(Math.max(1, ...points.map((p) => p.value)));
  const { y } = axes(max, []);
  const bars = points.map((p, i) => `<div class="bcol"><div class="bar ${o.single ? "p1" : PALETTE[i % PALETTE.length]}" style="height:${((100 * p.value) / max).toFixed(1)}%" ` +
    `title="${esc(p.label)}: ${esc(p.display ?? num(p.value))}${o.unit ? " " + esc(o.unit) : ""}"></div></div>`).join("");
  const labels = points.length <= 12 ? `<div class="xcat">${points.map((p) => `<span>${esc(p.label)}</span>`).join("")}</div>`
    : axes(max, points.map((p) => p.label.slice(5))).x;
  return `<div class="chart short" role="img" aria-label="${esc(o.aria)}"><div class="plotrow">${y}<div class="plot bars${points.length > 12 ? " dense" : ""}">${bars}</div></div>${labels}</div>`;
}

/** The kit's "Traffic by Location" donut with its legend (label and share). */
export function donut(parts: Array<{ label: string; value: number }>, o: { aria: string }): string {
  const total = parts.reduce((a, p) => a + p.value, 0);
  const R = 48, C = 2 * Math.PI * R, STROKE = 20, GAP = 4;
  const live = parts.filter((p) => p.value > 0).length;
  let off = 0;
  // Round caps add STROKE/2 at each end, so a segment's dash is shortened by STROKE + GAP to leave
  // the kit's visible gap; a slice too small for that is drawn as a dot (a zero-length dash).
  const rings = total <= 0 ? `<circle class="ring empty" cx="60" cy="60" r="${R}"/>` : parts.map((p, i) => {
    const len = (p.value / total) * C;
    const dash = live > 1 ? Math.max(0.01, len - STROKE - GAP) : C;
    const start = live > 1 ? off + Math.min(len / 2, (STROKE + GAP) / 2) - (dash <= 0.01 ? 0 : 0) : 0;
    const seg = p.value <= 0 ? "" : `<circle class="ring ${DONUT[i % DONUT.length]}" cx="60" cy="60" r="${R}" ` +
      `stroke-dasharray="${dash.toFixed(2)} ${C.toFixed(2)}" stroke-dashoffset="${(-start).toFixed(2)}"><title>${esc(p.label)}: ${num(p.value)}</title></circle>`;
    off += len;
    return seg;
  }).join("");
  const rows = parts.map((p, i) => `<li><span class="ld ${DONUT[i % DONUT.length]}"></span><span>${esc(p.label)}</span>` +
    `<span class="lv">${total ? ((100 * p.value) / total).toFixed(1) : "0.0"}%</span></li>`).join("");
  return `<div class="donut"><svg width="120" height="120" viewBox="0 0 120 120" role="img" aria-label="${esc(o.aria)}">` +
    `<g transform="rotate(-90 60 60)">${rings}</g></svg><ul class="dleg">${rows}</ul></div>`;
}

/** The kit's "Traffic by Website" list: a label, then a short bar in three tones (the value, and the
 * rest of the scale faint). */
export function hbars(rows: Array<{ label: string; value: number; display?: string; href?: string }>, empty: string,
                      o: { compact?: boolean } = {}): string {
  if (!rows.length) return `<p class="muted">${esc(empty)}</p>`;
  const max = Math.max(1, ...rows.map((r) => r.value));
  return `<ul class="sbars${o.compact ? " compact" : ""}">${rows.map((r) => {
    const w = Math.max(4, (100 * r.value) / max);
    const shown = r.display ?? num(r.value);
    return `<li title="${esc(r.label)}: ${esc(shown)}"><span class="sl">${r.href ? `<a href="${r.href}">${esc(r.label)}</a>` : esc(r.label)}</span>` +
      `<span class="st"><span class="s1" style="width:${(w * 0.6).toFixed(1)}%"></span><span class="s2" style="width:${(w * 0.4).toFixed(1)}%"></span><span class="s3"></span></span>` +
      (o.compact ? "" : `<span class="sv">${esc(shown)}</span>`) + `</li>`;
  }).join("")}</ul>`;
}

/** A labelled field list (label left in 40%, value right), as on a person's page. */
export const facts = (rows: Array<[string, string]>) =>
  `<dl class="facts">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;

/** The right sidebar's lists: "Notifications" (a 24 icon tile, #edeefc / #e6f1fd), "Activities" (round
 * avatars joined by a line) and "Contacts" (round avatars). */
export function railList(title: string, items: Array<{ icon: string; text: string; sub?: string; href?: string; tone?: "a" | "b" | "bad" | "warn" }>,
                         o: { timeline?: boolean; round?: boolean; empty?: string } = {}): string {
  const list = items.length ? items.map((it, i) => `<li class="ri${o.timeline && i < items.length - 1 ? " tl" : ""}">` +
    `<span class="rt ${o.timeline || o.round ? "round" : it.tone ?? (i % 2 ? "b" : "a")}">${icon(it.icon)}</span>` +
    `<span class="rx"><span class="r1">${it.href ? `<a href="${it.href}">${esc(it.text)}</a>` : esc(it.text)}</span>` +
    (it.sub ? `<span class="r2">${esc(it.sub)}</span>` : "") + `</span></li>`).join("")
    : `<li class="ri"><span class="rx"><span class="r2">${esc(o.empty ?? "Nothing yet")}</span></span></li>`;
  return `<section class="rsec"><h2>${esc(title)}</h2><ul>${list}</ul></section>`;
}

// ---- CSS ---------------------------------------------------------------------------------------- //

const CSS = `
:root{--bg:#fff;--t1:#000;--n1:#1c1c1c;--t2:rgba(0,0,0,.4);--t3:rgba(0,0,0,.2);--ln:rgba(0,0,0,.1);--fill:rgba(0,0,0,.04);
--block:#f9f9fa;--stripe:rgba(0,0,0,.02);--ka:#edeefc;--kb:#e6f1fd;
--p1:#a0bce8;--p2:#6be6d3;--p3:#000;--p4:#33d1ff;--p5:#b899eb;--p6:#71dd8c;--lf:rgba(0,0,0,.10);
--ok:#71dd8c;--warn:#ffc555;--bad:#ff4747;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#333;--t1:#fff;--t2:rgba(255,255,255,.4);--t3:rgba(255,255,255,.2);--ln:rgba(255,255,255,.15);
--fill:rgba(255,255,255,.1);--block:rgba(255,255,255,.04);--stripe:rgba(255,255,255,.04);--p3:rgba(255,255,255,.4);--lf:rgba(255,255,255,.10);color-scheme:dark}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--t1)}
body{font:400 14px/20px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;font-feature-settings:"cv11"}
a{color:inherit;text-decoration:none}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--p1);outline-offset:1px}
ul{list-style:none;margin:0;padding:0}h1,h2,h3{margin:0}
.app{display:grid;grid-template-columns:212px minmax(0,1fr);min-height:100vh}
.app.withrail{grid-template-columns:212px minmax(0,1fr) 280px}
.ic{flex:none;display:block}
.side{border-right:1px solid var(--ln);padding:16px;display:flex;flex-direction:column;gap:8px;position:sticky;top:0;height:100vh;overflow-y:auto}
.group{display:flex;flex-direction:column;gap:4px;padding-bottom:12px}
.brand{display:flex;align-items:center;gap:8px;height:40px;padding:8px;border-radius:8px}
.logo{width:24px;height:24px;border-radius:80px;background:var(--t1);color:var(--bg);display:grid;place-items:center;font-size:12px;font-weight:600}
.glabel{height:28px;padding:4px 12px;color:var(--t2)}
.nav{display:flex;align-items:center;gap:4px;height:36px;padding:8px;border-radius:12px}
.nav:hover,.nav.on{background:var(--fill)}
.slot{width:16px;flex:none}.it{display:flex;align-items:center;gap:8px;min-width:0}
.nbadge{margin-left:auto;font-size:12px;line-height:16px;color:var(--bad)}
.who{margin-top:auto;color:var(--t2);font-size:12px;line-height:16px;padding:8px;overflow-wrap:anywhere}
.main{min-width:0;display:flex;flex-direction:column}
.top{height:68px;padding:20px 28px;border-bottom:1px solid var(--ln);display:flex;align-items:center;justify-content:space-between;gap:20px}
.crumbs{display:flex;align-items:center;gap:4px;min-width:0;white-space:nowrap;overflow:hidden}
.crumb{padding:4px 12px;font-size:12px;line-height:16px;color:var(--t2);border-radius:12px}a.crumb:hover{background:var(--fill)}.crumb.now{color:var(--t1)}
.sep{color:var(--ln)}
.search{display:flex;align-items:center;gap:8px;height:28px;width:160px;padding:4px 8px;border-radius:16px;background:var(--fill);color:var(--t3)}
.search input{border:0;background:transparent;color:var(--t1);font:inherit;outline:none;width:100%;padding:0}
.search input::placeholder{color:var(--t3)}
.content{padding:28px;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:28px;align-content:start}
.content>*{grid-column:1/-1}
.ptitle{display:flex;align-items:center;justify-content:space-between;gap:12px;height:28px;margin-bottom:-12px}
.ptitle h1{font-size:14px;line-height:20px;font-weight:600;padding:4px 8px}
.kpi{grid-column:span 1;border-radius:20px;padding:24px;display:flex;flex-direction:column;gap:8px;color:#000;min-height:112px;min-width:0}
.kpi.a{background:var(--ka)}.kpi.b{background:var(--kb)}
.klabel{font-weight:400}
.krow{display:flex;align-items:center;justify-content:space-between;gap:8px;min-width:0}
.kvalue{font-size:24px;line-height:36px;font-weight:600;color:var(--n1);font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.delta{display:inline-flex;align-items:center;gap:4px;font-size:12px;line-height:16px}
.block{background:var(--block);border-radius:20px;padding:24px;display:flex;flex-direction:column;gap:16px;min-width:0}
.block.s1{grid-column:span 1}.block.s2{grid-column:span 2}.block.s3{grid-column:span 3}.block.s4{grid-column:1/-1}
.bhead{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:20px;flex-wrap:wrap}
.block h2{font-size:14px;line-height:20px;font-weight:600}
.muted{color:var(--t2)}.small{font-size:12px;line-height:16px}
.lgd{display:inline-flex;gap:16px;font-size:12px;line-height:16px;align-items:center}.lgd>span{display:inline-flex;align-items:center;gap:6px}
.ld{width:6px;height:6px;border-radius:50%;display:inline-block;flex:none;background:var(--t1)}
.ld.p1{background:var(--p1)}.ld.p2{background:var(--p2)}.ld.p3{background:var(--p3)}.ld.p4{background:var(--p4)}.ld.p5{background:var(--p5)}.ld.p6{background:var(--p6)}
.chart{display:flex;flex-direction:column;gap:8px}
.plotrow{display:flex;gap:16px;height:222px}.chart.short .plotrow{height:172px}
.yax{display:flex;flex-direction:column;justify-content:space-between;font-size:12px;line-height:16px;color:var(--t2);text-align:right;min-width:23px;margin:-8px 0}
.plot{position:relative;flex:1;min-width:0}.plot svg{width:100%;height:100%;display:block;overflow:visible}
.plot .line{fill:none;stroke:var(--p3);stroke-width:2;vector-effect:non-scaling-stroke;stroke-linecap:round}
.plot .cmp{fill:none;stroke:var(--p1);stroke-width:2;stroke-dasharray:2 4;vector-effect:non-scaling-stroke;stroke-linecap:round}
.plot .cap{stroke:var(--t3);stroke-dasharray:4 4;vector-effect:non-scaling-stroke}
.plot .area{fill:url(#lf)}.lf0{stop-color:var(--lf)}.lf1{stop-color:var(--lf);stop-opacity:0}
.xax{display:flex;justify-content:space-between;font-size:12px;line-height:16px;color:var(--t2);padding-left:39px}
.plot.bars{display:flex;align-items:flex-end;justify-content:space-around;gap:8px;border-bottom:1px solid var(--ln)}
.bcol{flex:1;display:flex;justify-content:center;align-items:flex-end;height:100%;min-width:0}
.bar{width:28px;max-width:100%;border-radius:8px 8px 0 0;min-height:2px}
.plot.bars.dense .bar{width:100%;max-width:28px;border-radius:4px 4px 0 0}
.bar.p1{background:var(--p1)}.bar.p2{background:var(--p2)}.bar.p3{background:var(--p3)}.bar.p4{background:var(--p4)}.bar.p5{background:var(--p5)}.bar.p6{background:var(--p6)}
.xcat{display:flex;justify-content:space-around;gap:8px;font-size:12px;line-height:16px;color:var(--t2);padding-left:39px;text-align:center}.xcat span{flex:1;min-width:0}
.donut{display:flex;align-items:center;gap:40px;flex-wrap:wrap;justify-content:center;padding:8px 0}
.ring{fill:none;stroke-width:20;stroke-linecap:round}.ring.empty{stroke:var(--fill)}
.ring.p1{stroke:var(--p1)}.ring.p2{stroke:var(--p2)}.ring.p3{stroke:var(--p3)}.ring.p4{stroke:var(--p4)}.ring.p5{stroke:var(--p5)}.ring.p6{stroke:var(--p6)}
.dleg{display:flex;flex-direction:column;gap:12px;min-width:140px;flex:1;max-width:220px;font-size:12px;line-height:16px}
.dleg li{display:flex;align-items:center;gap:6px}.dleg .lv{margin-left:auto;font-variant-numeric:tabular-nums}
.sbars{display:flex;flex-direction:column;gap:20px;padding-top:4px}
.sbars.compact li{grid-template-columns:minmax(0,57px) minmax(0,80px)}.sbars li{display:grid;grid-template-columns:minmax(0,80px) minmax(24px,1fr) auto;align-items:center;gap:16px;font-size:12px;line-height:16px}
.sl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.sl a:hover{text-decoration:underline}
.st{display:flex;gap:2px;align-items:center;height:2px}.st span{height:2px;border-radius:80px;display:block}
.st .s1{background:var(--t1)}.st .s2{background:var(--t2)}.st .s3{background:var(--ln);flex:1}
.sv{color:var(--t2);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.meter{height:4px;background:var(--fill);border-radius:80px;overflow:hidden;margin-top:8px}.meter span{display:block;height:100%;background:var(--t1);border-radius:80px}
.meter.warn span{background:var(--warn)}.meter.bad span{background:var(--bad)}
.status{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;font-size:12px;line-height:16px}
.dot{width:6px;height:6px;border-radius:50%;display:inline-block;flex:none;background:var(--t3)}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
.tablewrap{overflow-x:auto;margin:0 -8px}
table{width:100%;border-collapse:separate;border-spacing:0 4px}
th{text-align:left;font-weight:400;font-size:12px;line-height:16px;color:var(--t2);padding:12px 16px;white-space:nowrap}
td{padding:16px;line-height:20px;vertical-align:middle;font-variant-numeric:tabular-nums}
tbody tr:nth-child(odd) td{background:var(--stripe)}
tbody tr:hover td{background:var(--fill)}
td a{text-decoration:underline;text-decoration-color:var(--t3);text-underline-offset:3px}td a:hover{text-decoration-color:var(--t1)}
td:first-child{overflow-wrap:anywhere}
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
.rail{border-left:1px solid var(--ln);padding:16px;display:flex;flex-direction:column;gap:16px;position:sticky;top:0;height:100vh;overflow-y:auto}
.rsec{display:flex;flex-direction:column;gap:4px}
.rsec h2{font-size:14px;line-height:20px;font-weight:400;padding:8px 4px}
.ri{display:flex;gap:8px;padding:8px;border-radius:12px;position:relative;min-width:0}
.rt{width:24px;height:24px;border-radius:8px;display:grid;place-items:center;flex:none;color:#000}
.rt.a{background:var(--ka)}.rt.b{background:var(--kb)}.rt.bad{background:#ffe5e5}.rt.warn{background:#fff3d6}
.rt.round{border-radius:80px;background:var(--fill);color:var(--t1)}
.ri.tl::after{content:"";position:absolute;left:19.5px;top:36px;width:1px;height:20px;background:var(--ln)}
.rx{display:flex;flex-direction:column;min-width:0}.r1{overflow-wrap:anywhere}.r1 a:hover{text-decoration:underline}.r2{font-size:12px;line-height:16px;color:var(--t2)}
.foot{color:var(--t2);font-size:12px;line-height:16px}
details summary{cursor:pointer;color:var(--t2);font-size:12px;line-height:16px}
@media (max-width:1280px){.app.withrail{grid-template-columns:212px minmax(0,1fr)}.rail{grid-column:2;position:static;height:auto;border-left:0;border-top:1px solid var(--ln);padding:28px}}
@media (max-width:1100px){.content{grid-template-columns:repeat(2,minmax(0,1fr))}.block.s3,.block.s2{grid-column:1/-1}.block.s1{grid-column:span 1}}
@media (max-width:760px){.app,.app.withrail{grid-template-columns:minmax(0,1fr)}
.side{position:static;height:auto;flex-direction:row;flex-wrap:wrap;gap:4px;border-right:0;border-bottom:1px solid var(--ln);padding:12px 16px}
.side .group{flex-direction:row;flex-wrap:wrap;padding:0}.glabel,.who,.slot{display:none}
.rail{grid-column:1;padding:20px 16px}.top{padding:16px;height:auto;flex-wrap:wrap}.search{width:100%}.content{padding:20px 16px;gap:16px;grid-template-columns:minmax(0,1fr)}
.kpi,.block.s1{grid-column:1/-1}}
`;
