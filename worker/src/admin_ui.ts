// The admin dashboard's look: one shell (sidebar, top bar, optional right rail), the shared CSS and
// the small components every page is built from. Server-rendered HTML with inline SVG charts and no
// script at all, so the Content-Security-Policy can stay "default-src 'none'".
//
// The visual design follows ByeWind's free "Dashboard UI Kit" (SnowUI), Figma Community,
// https://www.figma.com/community/file/1210542873091115123, licensed CC BY 4.0: rebuilt here from
// scratch in our own markup, and credited in the page footer as the licence asks.

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

/** "2 h ago", "3 d ago": how long before `now`. */
export function ago(s: number | null | undefined, now: number): string {
  if (!s) return "never";
  const d = Math.max(0, now - s);
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86_400) return `${Math.floor(d / 3600)} h ago`;
  return `${Math.floor(d / 86_400)} d ago`;
}

export const PASS_LABEL: Record<string, string> = { free: "Free", pass30: "Job Hunt Pass", pass90: "Season Pass" };

// ---- icons (16px, stroke, currentColor) --------------------------------------------------------- //

const ICON: Record<string, string> = {
  overview: '<path d="M3 13a5 5 0 1 1 10 0"/><path d="M8 13l2.5-3.5"/>',
  users: '<circle cx="6" cy="5.5" r="2.5"/><path d="M1.5 13.5c.6-2.4 2.3-3.5 4.5-3.5s3.9 1.1 4.5 3.5"/><path d="M11 3.2a2.4 2.4 0 0 1 0 4.6M12.2 10.2c1.3.4 2 1.5 2.3 3.3"/>',
  revenue: '<rect x="1.5" y="3.5" width="13" height="9" rx="2"/><path d="M1.5 6.5h13M4 10h3"/>',
  usage: '<path d="M2 13.5V9M6 13.5V5M10 13.5V7.5M14 13.5V2.5"/>',
  health: '<path d="M1.5 8h3l1.5-4 3 8 1.5-4h4"/>',
  audit: '<rect x="3" y="1.5" width="10" height="13" rx="2"/><path d="M5.5 5h5M5.5 8h5M5.5 11h3"/>',
  search: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5l3.5 3.5"/>',
  alert: '<path d="M8 2l6.5 11.5h-13z"/><path d="M8 6.5v3M8 11.5v.5"/>',
  ok: '<circle cx="8" cy="8" r="6.5"/><path d="M5 8.2l2 2 4-4.2"/>',
  arrowUp: '<path d="M4 11l7-7M5.5 4H11v5.5"/>',
  arrowDown: '<path d="M4 5l7 7M11 6.5V12H5.5"/>',
};
export const icon = (name: string, cls = "ic") =>
  `<svg class="${cls}" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" ` +
  `stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name] ?? ""}</svg>`;

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
  title: string;               // the breadcrumb's last part and the <title>
  crumbs?: Array<{ label: string; href?: string }>;   // between "Dashboards" and the title
  viewer: string;              // the Access e-mail
  generatedAt: number;
  body: string;
  rail?: string;               // the right-hand column (overview only)
  flash?: { kind: "ok" | "bad"; text: string } | null;
  query?: string;              // keeps the search box filled on the Users page
  badges?: Partial<Record<PageId, string>>;   // e.g. a count of problems beside Health
}

export function shell(s: Shell): string {
  const nav = NAV.map((g) => `<div class="navgroup"><div class="navlabel">${esc(g.group)}</div>` +
    g.items.map((it) => `<a class="navitem${it.id === s.page ? " on" : ""}" href="${it.href}"` +
      (it.id === s.page ? ' aria-current="page"' : "") + `>${icon(it.id)}<span>${esc(it.label)}</span>` +
      (s.badges?.[it.id] ? `<span class="navbadge">${esc(s.badges[it.id])}</span>` : "") + `</a>`).join("") +
    `</div>`).join("");
  const crumbs = [{ label: NAV.find((g) => g.items.some((i) => i.id === s.page))?.group ?? "Dashboards" },
                  ...(s.crumbs ?? []), { label: s.title }]
    .map((c, i, all) => i === all.length - 1 ? `<span class="crumb now">${esc(c.label)}</span>`
      : c.href ? `<a class="crumb" href="${c.href}">${esc(c.label)}</a>` : `<span class="crumb">${esc(c.label)}</span>`)
    .join('<span class="sep">/</span>');
  const flash = s.flash ? `<div class="flash ${s.flash.kind}" role="status">${icon(s.flash.kind === "ok" ? "ok" : "alert")}<span>${esc(s.flash.text)}</span></div>` : "";
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>${esc(s.title)} · SponsorJobs Admin</title>
<style>${CSS}</style></head><body>
<div class="app${s.rail ? " withrail" : ""}">
<aside class="side"><a class="brand" href="/"><span class="logo">S</span><span>SponsorJobs</span></a>${nav}
<div class="who">${esc(s.viewer)}</div></aside>
<div class="main">
<header class="top"><nav class="crumbs" aria-label="Breadcrumb">${crumbs}</nav>
<form class="search" method="get" action="/users" role="search">${icon("search")}<input name="q" value="${esc(s.query ?? "")}" placeholder="Find a user by e-mail or id" aria-label="Find a user"></form>
<span class="stamp">${esc(date(s.generatedAt))}</span></header>
<main class="content">${flash}${s.body}
<footer class="foot">Design based on ByeWind's Dashboard UI Kit (SnowUI), CC BY 4.0. Numbers marked "estimate" come from token counts at list prices.</footer>
</main></div>
${s.rail ? `<aside class="rail">${s.rail}</aside>` : ""}
</div></body></html>`;
}

// ---- components --------------------------------------------------------------------------------- //

/** A KPI tile. `tone` picks the kit's two pastel fills; `delta` is shown as "+12%" with an arrow. */
export function kpi(label: string, value: string, o: { sub?: string; tone?: "a" | "b"; delta?: number | null } = {}): string {
  const d = o.delta;
  const delta = d === undefined || d === null || !Number.isFinite(d) ? "" :
    `<span class="delta">${d >= 0 ? "+" : ""}${d.toFixed(1)}% ${icon(d >= 0 ? "arrowUp" : "arrowDown", "ic sm")}</span>`;
  return `<div class="kpi ${o.tone ?? "a"}"><div class="klabel">${esc(label)}</div>` +
    `<div class="krow"><span class="kvalue">${esc(value)}</span>${delta}</div>` +
    (o.sub ? `<div class="ksub">${esc(o.sub)}</div>` : "") + `</div>`;
}

export const card = (title: string, body: string, o: { cls?: string; aside?: string } = {}) =>
  `<section class="card ${o.cls ?? ""}"><div class="chead"><h2>${esc(title)}</h2>${o.aside ?? ""}</div>${body}</section>`;

export function badge(text: string, tone: "ok" | "warn" | "bad" | "plain" = "plain"): string {
  return `<span class="badge ${tone}">${esc(text)}</span>`;
}

/** A thin meter; `level` colours it. */
export function meter(percent: number, level: "ok" | "warn" | "bad" = "ok"): string {
  const p = Math.max(0, Math.min(100, percent));
  return `<div class="meter ${level}" role="img" aria-label="${p.toFixed(0)} percent"><span style="width:${p.toFixed(1)}%"></span></div>`;
}

export function table(head: string[], rows: string[][], empty: string, o: { cls?: string } = {}): string {
  const th = head.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = rows.length ? rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")
    : `<tr><td colspan="${head.length}" class="muted">${esc(empty)}</td></tr>`;
  return `<div class="tablewrap"><table class="${o.cls ?? ""}"><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
}

const PALETTE = ["c2", "c3", "c1", "c4", "c5", "c6"];

/** Daily bars (one series), inline SVG, with a <title> per bar for hover and the same numbers in a
 * table under it. */
export function barChart(points: Array<{ label: string; value: number }>, o: { aria: string; unit?: string; cls?: string }): string {
  const W = 760, H = 200, top = 14, bottom = 24, left = 34;
  const max = Math.max(1, ...points.map((p) => p.value));
  const plotH = H - top - bottom;
  const slot = (W - left) / Math.max(1, points.length);
  const bw = Math.max(3, Math.min(28, slot * 0.6));
  const y = (v: number) => top + plotH - (v / max) * plotH;
  const bars = points.map((p, i) => {
    const x = left + i * slot + (slot - bw) / 2;
    const h = p.value > 0 ? Math.max(3, (p.value / max) * plotH) : 0;
    const r = Math.min(6, bw / 2, h);
    const yb = top + plotH;
    const path = h <= 0 ? "" :
      `<path class="bar ${o.cls ?? "c2"}" d="M${x},${yb} V${yb - h + r} Q${x},${yb - h} ${x + r},${yb - h} ` +
      `H${x + bw - r} Q${x + bw},${yb - h} ${x + bw},${yb - h + r} V${yb} Z"/>`;
    return `<g><title>${esc(p.label)}: ${num(p.value)}${o.unit ? " " + esc(o.unit) : ""}</title>` +
      `<rect class="hit" x="${left + i * slot}" y="${top}" width="${slot}" height="${plotH}"/>${path}</g>`;
  }).join("");
  const at = (i: number) => left + i * slot + slot / 2;
  const labels = edgeLabels(points, at, H);
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.aria)}">` +
    `<line class="grid" x1="${left}" x2="${W}" y1="${y(max)}" y2="${y(max)}"/>` +
    `<line class="grid" x1="${left}" x2="${W}" y1="${y(max / 2)}" y2="${y(max / 2)}"/>` +
    `<line class="base" x1="${left}" x2="${W}" y1="${top + plotH}" y2="${top + plotH}"/>` +
    `<text class="axis" x="${left - 8}" y="${y(max) + 4}" text-anchor="end">${shortNum(max)}</text>` +
    `<text class="axis" x="${left - 8}" y="${top + plotH + 4}" text-anchor="end">0</text>` +
    bars + labels + `</svg>`;
}

/** A smooth line (Catmull-Rom as cubic Beziers) with a faint area under it, plus an optional dashed
 * cap line (e.g. the free pool's daily limit). */
export function lineChart(points: Array<{ label: string; value: number }>, o: { aria: string; cap?: number; unit?: string }): string {
  const W = 1100, H = 240, top = 18, bottom = 24, left = 40, right = 8;
  const max = Math.max(1, o.cap ?? 0, ...points.map((p) => p.value));
  const plotH = H - top - bottom, plotW = W - left - right;
  const x = (i: number) => left + (points.length <= 1 ? plotW / 2 : (i / (points.length - 1)) * plotW);
  const y = (v: number) => top + plotH - (v / max) * plotH;
  const pts = points.map((p, i) => [x(i), y(p.value)] as const);
  let d = "";
  pts.forEach((p, i) => {
    if (i === 0) { d = `M${p[0]},${p[1]}`; return; }
    const p0 = pts[i - 2] ?? pts[i - 1]!, p1 = pts[i - 1]!, p2 = p, p3 = pts[i + 1] ?? p;
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += ` C${c1[0]},${Math.min(top + plotH, c1[1]!)} ${c2[0]},${Math.min(top + plotH, c2[1]!)} ${p2[0]},${p2[1]}`;
  });
  const area = pts.length ? `${d} L${pts[pts.length - 1]![0]},${top + plotH} L${pts[0]![0]},${top + plotH} Z` : "";
  const dots = points.map((p, i) => `<g><title>${esc(p.label)}: ${num(p.value)}${o.unit ? " " + esc(o.unit) : ""}</title>` +
    `<circle class="dot" cx="${x(i)}" cy="${y(p.value)}" r="3"/></g>`).join("");
  const cap = o.cap ? `<line class="cap" x1="${left}" x2="${W - right}" y1="${y(o.cap)}" y2="${y(o.cap)}"/>` +
    `<text class="axis" x="${W - right}" y="${y(o.cap) - 5}" text-anchor="end">limit ${shortNum(o.cap)}</text>` : "";
  const labels = edgeLabels(points, x, H);
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.aria)}">` +
    `<line class="base" x1="${left}" x2="${W - right}" y1="${top + plotH}" y2="${top + plotH}"/>` +
    `<text class="axis" x="${left - 8}" y="${y(max) + 4}" text-anchor="end">${shortNum(max)}</text>` +
    `<text class="axis" x="${left - 8}" y="${top + plotH + 4}" text-anchor="end">0</text>` + cap +
    (area ? `<path class="area" d="${area}"/><path class="line" d="${d}"/>` : "") + dots + labels + `</svg>`;
}

/** A donut with a legend (share of each part). */
export function donut(parts: Array<{ label: string; value: number }>, o: { aria: string; center?: string }): string {
  const total = parts.reduce((a, p) => a + p.value, 0);
  const R = 52, C = 2 * Math.PI * R;
  let off = 0;
  const rings = total <= 0 ? `<circle class="ring empty" cx="70" cy="70" r="${R}"/>` : parts.map((p, i) => {
    const len = (p.value / total) * C;
    const gap = parts.filter((q) => q.value > 0).length > 1 ? Math.min(4, len / 3) : 0;
    const seg = `<circle class="ring ${PALETTE[i % PALETTE.length]}" cx="70" cy="70" r="${R}" ` +
      `stroke-dasharray="${Math.max(0, len - gap)} ${C}" stroke-dashoffset="${-off}"><title>${esc(p.label)}: ${num(p.value)}</title></circle>`;
    off += len;
    return seg;
  }).join("");
  const legend = parts.map((p, i) => `<li><span class="sw ${PALETTE[i % PALETTE.length]}"></span><span>${esc(p.label)}</span>` +
    `<span class="lv">${total ? ((100 * p.value) / total).toFixed(1) : "0.0"}%</span></li>`).join("");
  return `<div class="donut"><svg viewBox="0 0 140 140" role="img" aria-label="${esc(o.aria)}">` +
    `<g transform="rotate(-90 70 70)">${rings}</g>` +
    (o.center ? `<text class="dc" x="70" y="76" text-anchor="middle">${esc(o.center)}</text>` : "") +
    `</svg><ul class="legend">${legend}</ul></div>`;
}

/** Horizontal bars, one per row (the kit's "Traffic by Website" list). */
export function hbars(rows: Array<{ label: string; value: number; display?: string; href?: string }>, empty: string): string {
  if (!rows.length) return `<p class="muted">${esc(empty)}</p>`;
  const max = Math.max(1, ...rows.map((r) => r.value));
  return `<ul class="hbars">${rows.map((r, i) => `<li><span class="hl">${r.href ? `<a href="${r.href}">${esc(r.label)}</a>` : esc(r.label)}</span>` +
    `<span class="ht"><span class="hb ${PALETTE[i % PALETTE.length]}" style="width:${Math.max(2, (100 * r.value) / max).toFixed(1)}%"></span></span>` +
    `<span class="hv">${esc(r.display ?? num(r.value))}</span></li>`).join("")}</ul>`;
}

/** First, middle and last x-axis labels ("MM-DD"); the end ones are anchored inward so they are
 * never cut off at the chart's edge. */
function edgeLabels(points: Array<{ label: string }>, x: (i: number) => number, H: number): string {
  if (!points.length) return "";
  const last = points.length - 1;
  return [0, Math.floor(last / 2), last].filter((v, i, a) => a.indexOf(v) === i).map((i) => {
    const anchor = i === 0 && last > 0 ? "start" : i === last && last > 0 ? "end" : "middle";
    const dx = anchor === "start" ? -6 : anchor === "end" ? 6 : 0;
    return `<text class="axis" x="${x(i) + dx}" y="${H - 6}" text-anchor="${anchor}">${esc(points[i]!.label.slice(5))}</text>`;
  }).join("");
}

export function shortNum(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}K`;
  return String(Math.round(n));
}

/** A labelled field list (label left, value right), as on a person's page. */
export const facts = (rows: Array<[string, string]>) =>
  `<dl class="facts">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;

// ---- CSS ---------------------------------------------------------------------------------------- //

const CSS = `
:root{--bg:#fff;--side:#fff;--card:#f7f9fb;--ink:#1c1c1c;--ink2:#5c5c5c;--muted:#8f8f8f;--line:#e8e8e8;--hover:rgba(28,28,28,.05);
--ta:#e3f5ff;--tb:#e5ecf6;--tink:#1c1c1c;--line-c:#1c1c1c;--area:rgba(28,28,28,.06);
--c1:#a8c5da;--c2:#95a4fc;--c3:#baedbd;--c4:#b1e3ff;--c5:#c6c7f8;--c6:#a1e3cb;
--good:#2f8f6b;--warn:#a86a00;--bad:#d63c3c;--goodbg:#e9f7ef;--warnbg:#fff4dc;--badbg:#ffecec;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1c;--side:#1c1c1c;--card:#272727;--ink:#fff;--ink2:#b5b5b5;--muted:#8a8a8a;--line:#343434;
--hover:rgba(255,255,255,.06);--line-c:#c6c7f8;--area:rgba(198,199,248,.08);
--good:#7fd6a8;--warn:#ffc555;--bad:#ff7a7a;--goodbg:rgba(127,214,168,.12);--warnbg:rgba(255,197,85,.12);--badbg:rgba(255,122,122,.12);color-scheme:dark}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink)}
body{font:14px/1.45 -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit}
.app{display:grid;grid-template-columns:212px minmax(0,1fr);min-height:100vh}
.app.withrail{grid-template-columns:212px minmax(0,1fr) 280px}
.side{border-right:1px solid var(--line);padding:20px 16px;background:var(--side);position:sticky;top:0;height:100vh;overflow-y:auto;display:flex;flex-direction:column;gap:18px}
.brand{display:flex;align-items:center;gap:8px;text-decoration:none;font-weight:600;padding:0 8px}
.logo{width:24px;height:24px;border-radius:50%;background:var(--ink);color:var(--bg);display:grid;place-items:center;font-size:12px;font-weight:700}
.navgroup{display:flex;flex-direction:column;gap:2px}.navlabel{color:var(--muted);font-size:13px;padding:4px 8px}
.navitem{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;text-decoration:none;color:var(--ink);position:relative}
.navitem:hover{background:var(--hover)}.navitem.on{background:var(--hover);font-weight:500}
.navitem.on::before{content:"";position:absolute;left:-8px;top:8px;bottom:8px;width:4px;border-radius:2px;background:var(--ink)}
.navbadge{margin-left:auto;background:var(--badbg);color:var(--bad);border-radius:8px;padding:0 6px;font-size:12px;font-weight:600}
.who{margin-top:auto;color:var(--muted);font-size:12px;padding:0 8px;overflow-wrap:anywhere}
.ic{flex:none}.ic.sm{width:12px;height:12px}
.main{min-width:0;display:flex;flex-direction:column}
.top{display:flex;align-items:center;gap:12px;padding:16px 28px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.crumbs{display:flex;align-items:center;gap:8px;flex:1;min-width:0;flex-wrap:wrap}
.crumb{color:var(--muted);text-decoration:none}.crumb.now{color:var(--ink)}a.crumb:hover{color:var(--ink)}.sep{color:var(--muted)}
.search{display:flex;align-items:center;gap:6px;background:var(--hover);border-radius:8px;padding:4px 10px;color:var(--muted);width:min(280px,100%)}
.search input{border:0;background:transparent;color:var(--ink);font:inherit;outline:none;width:100%;padding:2px 0}
.search:focus-within{outline:2px solid var(--c2)}
.stamp{color:var(--muted);font-size:12px}
.content{padding:24px 28px 40px;display:flex;flex-direction:column;gap:20px;max-width:1480px;width:100%}
h1{font-size:18px;font-weight:600;margin:0}
.ptitle{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}
.ptitle p{margin:4px 0 0;color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px}
.kpi{border-radius:16px;padding:20px 24px;color:var(--tink);display:flex;flex-direction:column;gap:8px}.kpi.a{background:var(--ta)}.kpi.b{background:var(--tb)}
.klabel{font-weight:600;font-size:14px}.krow{display:flex;align-items:center;justify-content:space-between;gap:8px}
.kvalue{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.delta{font-size:12px;display:inline-flex;align-items:center;gap:2px}.ksub{font-size:12px;color:rgba(28,28,28,.6)}
.grid2{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:16px}.grid2e{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.card{background:var(--card);border-radius:16px;padding:20px 24px;min-width:0}
.chead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap}
.card h2{font-size:14px;font-weight:600;margin:0}
.muted{color:var(--muted)}.small{font-size:12px}
.chart{width:100%;height:auto;display:block}.chart .bar.c1{fill:var(--c1)}.chart .bar.c2{fill:var(--c2)}.chart .bar.c3{fill:var(--c3)}.chart .bar.c4{fill:var(--c4)}
.chart g:hover .bar{opacity:.75}.chart .hit{fill:transparent}.chart .grid{stroke:var(--line);stroke-dasharray:3 4}.chart .base{stroke:var(--line)}
.chart .axis{fill:var(--muted);font-size:11px}.chart .line{fill:none;stroke:var(--line-c);stroke-width:2}.chart .area{fill:var(--area)}
.chart .dot{fill:var(--card);stroke:var(--line-c);stroke-width:1.5}.chart .cap{stroke:var(--muted);stroke-dasharray:4 4}
.donut{display:flex;align-items:center;gap:20px;flex-wrap:wrap}.donut svg{width:140px;height:140px;flex:none}
.ring{fill:none;stroke-width:22}.ring.empty{stroke:var(--line)}.ring.c1{stroke:var(--c1)}.ring.c2{stroke:var(--c2)}.ring.c3{stroke:var(--c3)}.ring.c4{stroke:var(--c4)}.ring.c5{stroke:var(--c5)}.ring.c6{stroke:var(--c6)}
.dc{fill:var(--ink);font-size:18px;font-weight:600}
.legend{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px;flex:1;min-width:150px}
.legend li{display:flex;align-items:center;gap:8px}.legend .lv{margin-left:auto;font-variant-numeric:tabular-nums}
.sw{width:8px;height:8px;border-radius:50%}.sw.c1,.hb.c1{background:var(--c1)}.sw.c2,.hb.c2{background:var(--c2)}.sw.c3,.hb.c3{background:var(--c3)}.sw.c4,.hb.c4{background:var(--c4)}.sw.c5,.hb.c5{background:var(--c5)}.sw.c6,.hb.c6{background:var(--c6)}
.hbars{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:12px}
.hbars li{display:grid;grid-template-columns:minmax(0,38%) minmax(0,1fr) 11em;align-items:center;gap:12px}
.hl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.hl a{text-decoration:none}.hl a:hover{text-decoration:underline}
.ht{height:6px;background:var(--line);border-radius:3px;overflow:hidden}.hb{display:block;height:100%;border-radius:3px}
.hv{font-variant-numeric:tabular-nums;color:var(--ink2);font-size:13px;text-align:right;white-space:nowrap}
.meter{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:8px}.meter span{display:block;height:100%;background:var(--c2);border-radius:3px}
.meter.warn span{background:var(--warn)}.meter.bad span{background:var(--bad)}
.badge{display:inline-block;border-radius:8px;padding:1px 8px;font-size:12px;font-weight:500;background:var(--hover);color:var(--ink2);white-space:nowrap}
.badge.ok{background:var(--goodbg);color:var(--good)}.badge.warn{background:var(--warnbg);color:var(--warn)}.badge.bad{background:var(--badbg);color:var(--bad)}
.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:400;color:var(--muted);padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top;font-variant-numeric:tabular-nums}
tbody tr:hover td{background:var(--hover)}td a{text-decoration:underline;text-decoration-color:var(--muted);text-underline-offset:2px}td a:hover{text-decoration-color:var(--ink)}
td.wrap,td:first-child{overflow-wrap:anywhere}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.flash{display:flex;align-items:center;gap:8px;border-radius:12px;padding:10px 14px}.flash.ok{background:var(--goodbg);color:var(--good)}.flash.bad{background:var(--badbg);color:var(--bad)}
.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.chip{border-radius:8px;padding:4px 10px;text-decoration:none;color:var(--ink2);background:var(--hover);font-size:13px}.chip.on{background:var(--ink);color:var(--bg)}
.pager{display:flex;gap:8px;justify-content:flex-end;align-items:center;margin-top:12px}
.btn{font:inherit;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:8px;padding:6px 12px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:6px}
.btn:hover{background:var(--hover)}.btn.primary{background:var(--ink);color:var(--bg);border-color:var(--ink)}.btn.danger{color:var(--bad);border-color:var(--bad)}
.act{display:flex;flex-direction:column;gap:8px;padding:14px 0;border-top:1px solid var(--line)}.act:first-of-type{border-top:0;padding-top:0}
.act h3{font-size:13px;font-weight:600;margin:0}.act.sep{border-top:1px solid var(--line);margin-top:16px;padding-top:16px}.act .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.act select,.act input[type=text]{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
.act input[type=text]{flex:1;min-width:180px}
.facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:8px 20px;margin:0}.facts dt{color:var(--muted)}.facts dd{margin:0;overflow-wrap:anywhere}
.attn{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:14px}
.attn li{display:grid;grid-template-columns:24px minmax(0,1fr);gap:8px}.attn .ib{width:24px;height:24px;border-radius:8px;display:grid;place-items:center}
.attn .ib.plain{background:var(--hover);color:var(--ink2)}.attn .ib.bad{background:var(--badbg);color:var(--bad)}.attn .ib.warn{background:var(--warnbg);color:var(--warn)}.attn .ib.ok{background:var(--goodbg);color:var(--good)}
.attn .t{overflow-wrap:anywhere}.attn .s{color:var(--muted);font-size:12px}
.rail{border-left:1px solid var(--line);padding:20px;display:flex;flex-direction:column;gap:24px;position:sticky;top:0;height:100vh;overflow-y:auto}
.rail h2{font-size:14px;font-weight:600;margin:0 0 12px}
.foot{color:var(--muted);font-size:12px;margin-top:12px}
details summary{cursor:pointer;color:var(--ink2);font-size:13px;margin-top:8px}
@media (max-width:1180px){.app.withrail{grid-template-columns:212px minmax(0,1fr)}.rail{grid-column:2;position:static;height:auto;border-left:0;border-top:1px solid var(--line);padding:24px 28px}}
@media (max-width:900px){.grid2,.grid2e{grid-template-columns:minmax(0,1fr)}}
@media (max-width:760px){.app,.app.withrail{grid-template-columns:minmax(0,1fr)}.side{position:static;height:auto;flex-direction:row;flex-wrap:wrap;gap:8px;border-right:0;border-bottom:1px solid var(--line);padding:12px 16px}
.navgroup{flex-direction:row;flex-wrap:wrap}.search{width:100%;order:3}.crumbs{flex-basis:100%}.navlabel,.who{display:none}.navitem.on::before{display:none}.rail{grid-column:1;padding:20px 16px}.top,.content{padding-left:16px;padding-right:16px}}
`;
