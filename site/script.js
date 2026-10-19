// SponsorJobs website. No analytics, no third-party scripts, no framework.
//
// Everything here is progressive: the HTML carries real numbers with an "as of" date, and the
// script replaces them with live ones when the fetch works. When it does not, the page keeps
// what it has and never shows a made-up figure.
//
//   1. Live numbers: roles and refresh time from the public job feed manifest, employers with
//      H-1B approvals from the sponsor index manifest.
//   2. "Check an employer": looks a name up in the same public sponsor index the browser
//      extension uses, in the browser, by downloading the one shard the name lives in.
//      sponsor_core.js (a verbatim copy of extension/sponsor_core.js) does the matching.
//   3. Download buttons: the visitor's own OS first; version, size and date from the GitHub
//      releases API, with the releases page as the fallback link.
//   4. GitHub card: live stars and forks, with a non-numeric fallback.
//
// The ONE place the feed host lives; site/_headers connect-src must list the same host.
const FEED_BASE = "https://feed.sponsorjobs.ai";
const REPO = "KofiGilbert/sponsorjobs";
const REPO_URL = "https://github.com/" + REPO;

const $ = (id) => document.getElementById(id);
const fmt = (n) => Number(n).toLocaleString("en-US");

async function getJSON(url, ms) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms || 6000);
  try {
    const res = await fetch(url, { signal: controller.signal, mode: "cors", cache: "no-store" });
    if (!res.ok) return null;
    return await res.json();
  } catch (_err) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

function relativeTime(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const mins = Math.round((Date.now() - t) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + (mins === 1 ? " minute ago" : " minutes ago");
  const hours = Math.round(mins / 60);
  if (hours < 48) return hours + (hours === 1 ? " hour ago" : " hours ago");
  const days = Math.round(hours / 24);
  return days + " days ago";
}

function shortDate(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return new Date(t).toLocaleDateString("en-US", { day: "numeric", month: "short", year: "numeric" });
}

// ---- 1. live numbers ---------------------------------------------------------------------
async function liveNumbers() {
  const roles = $("live-roles");
  if (roles) {
    const m = await getJSON(FEED_BASE + "/feed/manifest.json");
    const count = m ? Number(m.count) : NaN;
    if (Number.isFinite(count) && count > 0) {
      roles.textContent = fmt(count);
      const when = m.generated_at ? relativeTime(String(m.generated_at)) : null;
      const note = $("live-roles-note");
      if (note && when) note.textContent = "refreshed " + when;
      const refresh = $("live-refresh");
      if (refresh && when) refresh.textContent = when;
      const refreshNote = $("live-refresh-note");
      if (refreshNote && when) refreshNote.textContent = "live";
    }
  }
  const h1b = $("live-h1b");
  if (h1b) {
    const s = await getJSON(FEED_BASE + "/feed/sponsors/manifest.json");
    const n = s && s.stats ? Number(s.stats.h1b) : NaN;
    if (Number.isFinite(n) && n > 0) {
      h1b.textContent = fmt(n);
      const note = $("live-h1b-note");
      const d = s.generated_at ? shortDate(String(s.generated_at)) : null;
      if (note && d) note.textContent = "index of " + d;
    }
  }
}

// ---- 2. check an employer -------------------------------------------------------------------
const shards = new Map();     // shard key -> promise of a prepared shard

async function fetchShard(key) {
  const res = await fetch(FEED_BASE + "/feed/sponsors/" + key + ".json.gz", { mode: "cors" });
  if (!res.ok) throw new Error("shard " + key + " " + res.status);
  let buf = new Uint8Array(await res.arrayBuffer());
  // The bucket stores gzip. When it is served with Content-Encoding: gzip the browser has
  // already inflated it; when it comes through raw, inflate it here.
  if (buf[0] === 0x1f && buf[1] === 0x8b) {
    if (typeof DecompressionStream === "undefined") throw new Error("no DecompressionStream");
    const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
    buf = new Uint8Array(await new Response(stream).arrayBuffer());
  }
  const doc = JSON.parse(new TextDecoder().decode(buf));
  return window.SponsorCore.prepareShard(doc.records);
}

function loadShard(key) {
  if (!shards.has(key)) {
    const p = fetchShard(key).catch((err) => { shards.delete(key); throw err; });
    shards.set(key, p);
  }
  return shards.get(key);
}

function fyLabel(rec) {
  if (!rec.h1b_approvals) return "";
  const first = rec.h1b_first_fy || rec.h1b_last_fy;
  return first === rec.h1b_last_fy ? "FY" + rec.h1b_last_fy : "FY" + first + " to FY" + rec.h1b_last_fy;
}

function renderEmployer(name, rec) {
  const verdict = $("emp-verdict");
  const forEl = $("emp-for");
  const since = $("emp-since");
  const set = (id, text, off) => { const el = $(id); if (!el) return; el.textContent = text; el.classList.toggle("off", !!off); };
  forEl.textContent = rec ? rec.display_name : name;
  if (!rec) {
    verdict.textContent = "No record";
    verdict.classList.remove("yes");
    since.textContent = "No H-1B, PERM or E-Verify filing under this name. Try the legal name, or the parent company.";
    set("emp-h1b", "0", true); set("emp-perm", "0", true); set("emp-last", "none", true); set("emp-everify", "not found", true);
    return;
  }
  const sponsors = rec.h1b_approvals > 0 || rec.perm_certs > 0;
  verdict.textContent = sponsors ? "Sponsors: yes" : "Sponsors: no record";
  verdict.classList.toggle("yes", sponsors);
  const bits = [];
  if (rec.h1b_approvals > 0) bits.push("H-1B approvals on record " + fyLabel(rec));
  if (rec.perm_certs > 0) bits.push("PERM certifications on record");
  if (!sponsors && rec.e_verify) bits.push("Enrolled in E-Verify, no H-1B or PERM filings found");
  if (rec.cap_exempt) bits.push("Cap-exempt: no H-1B lottery");
  since.textContent = bits.join(". ") + (bits.length ? "." : "");
  set("emp-h1b", fmt(rec.h1b_approvals), rec.h1b_approvals === 0);
  set("emp-perm", fmt(rec.perm_certs), rec.perm_certs === 0);
  set("emp-last", rec.h1b_last_fy ? "FY" + rec.h1b_last_fy : "none", !rec.h1b_last_fy);
  set("emp-everify", rec.e_verify ? "Enrolled" : "Not found", !rec.e_verify);
}

async function checkEmployer(name) {
  const panel = $("employer");
  const status = $("emp-status");
  const core = window.SponsorCore;
  name = String(name || "").trim();
  if (!panel || !core || !name) return;
  panel.setAttribute("aria-busy", "true");
  status.classList.remove("warn");
  status.textContent = "Reading the public index";
  try {
    const keys = core.shardsFor(name);
    const loaded = await Promise.all(keys.map(loadShard));
    const byKey = Object.fromEntries(keys.map((k, i) => [k, loaded[i]]));
    const rec = core.lookup(name, (k) => byKey[k] || null);
    renderEmployer(name, rec);
    status.textContent = rec ? "Matched to the employer's own filings." : "";
  } catch (_err) {
    status.classList.add("warn");
    status.textContent = "Could not reach the index right now. Try again in a moment.";
  } finally {
    panel.removeAttribute("aria-busy");
  }
}

function wireEmployerPanel() {
  const form = $("emp-form");
  const input = $("emp-q");
  if (!form || !input) return;
  form.addEventListener("submit", (e) => { e.preventDefault(); checkEmployer(input.value); });
  document.querySelectorAll("[data-emp]").forEach((b) => {
    b.addEventListener("click", () => { input.value = b.dataset.emp; checkEmployer(b.dataset.emp); input.focus(); });
  });
}

// ---- 3. downloads ------------------------------------------------------------------------
function detectOS() {
  const ua = navigator.userAgent || "";
  if (/Windows/i.test(ua)) return "win";
  if (/Macintosh|Mac OS X/i.test(ua)) return "mac";
  return null;
}

function markPrimaryForThisOS() {
  const os = detectOS();
  const mac = $("dl-mac");
  const win = $("dl-win");
  if (mac && win) {
    // On the home page both are buttons; the visitor's own OS gets the solid one.
    if (os === "win") { mac.classList.add("btn-ghost"); win.classList.remove("btn-ghost"); }
    else if (os === "mac") { win.classList.add("btn-ghost"); mac.classList.remove("btn-ghost"); }
    else { mac.classList.add("btn-ghost"); win.classList.add("btn-ghost"); }
  }
  document.querySelectorAll(".dl-card[data-os]").forEach((card) => {
    card.dataset.primary = String(card.dataset.os === os);
  });
  // Put the visitor's OS first in the download cards.
  const cards = document.querySelector(".dl-cards");
  if (cards && os === "win") {
    const winCard = cards.querySelector('[data-os="win"]');
    if (winCard) cards.prepend(winCard);
  }
}

function mb(bytes) {
  const n = Number(bytes);
  return Number.isFinite(n) && n > 0 ? (n / 1048576).toFixed(0) + " MB" : null;
}

async function releaseInfo() {
  const cards = document.querySelectorAll(".dl-card[data-os]");
  if (!cards.length) return;
  const rel = await getJSON("https://api.github.com/repos/" + REPO + "/releases/latest", 8000);
  if (!rel || !Array.isArray(rel.assets)) return;   // the links already point at releases/latest
  const version = String(rel.tag_name || "").replace(/^v/, "");
  const date = rel.published_at ? shortDate(String(rel.published_at)) : null;
  const pick = (test) => rel.assets.find((a) => test(String(a.name || "")));
  const assets = {
    mac: pick((n) => /arm64\.dmg$/i.test(n)) || pick((n) => /\.dmg$/i.test(n)),
    win: pick((n) => /Setup.*\.exe$/i.test(n)) || pick((n) => /\.exe$/i.test(n)),
  };
  cards.forEach((card) => {
    const asset = assets[card.dataset.os];
    const link = card.querySelector("a.btn");
    const meta = card.querySelector(".meta");
    if (asset && asset.browser_download_url && link) link.href = asset.browser_download_url;
    if (meta) {
      meta.textContent = "";
      const parts = [];
      if (version) parts.push(["Version", version]);
      const size = asset ? mb(asset.size) : null;
      if (size) parts.push(["Size", size]);
      if (date) parts.push(["Released", date]);
      parts.forEach(([k, v]) => {
        const span = document.createElement("span");
        const b = document.createElement("b");
        b.textContent = v;
        span.append(k + " ", b);
        meta.append(span);
      });
    }
  });
  const line = $("dl-version-line");
  if (line && version) line.textContent = "Latest release: " + version + (date ? ", " + date : "") + ".";
}

// ---- 4. GitHub card ----------------------------------------------------------------------
async function githubCard() {
  const stars = $("gh-stars");
  if (!stars) return;
  const d = await getJSON("https://api.github.com/repos/" + REPO, 8000);
  if (!d) return;   // keeps the non-numeric fallback already in the HTML
  if (Number(d.stargazers_count) > 0) {
    stars.textContent = fmt(d.stargazers_count);
    stars.classList.remove("v-text");
  } else {
    stars.textContent = "Just published";
    stars.classList.add("v-text");
  }
  const forks = $("gh-forks");
  if (forks && Number.isFinite(Number(d.forks_count))) forks.textContent = fmt(d.forks_count);
  const pushed = $("gh-pushed");
  if (pushed && d.pushed_at) {
    const when = relativeTime(String(d.pushed_at));
    if (when) pushed.textContent = when;
  }
}

wireEmployerPanel();
markPrimaryForThisOS();
liveNumbers();
releaseInfo();
githubCard();
