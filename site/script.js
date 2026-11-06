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
//   4. GitHub card: live stars and last commit, with a non-numeric fallback.
//   5. Feature tabs on the home page. Without this script the first tab shows on its own.
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
      const refresh = $("live-refresh");
      if (refresh && when) refresh.textContent = when.charAt(0).toUpperCase() + when.slice(1);
    }
  }
  const h1b = $("live-h1b");
  if (h1b) {
    const s = await getJSON(FEED_BASE + "/feed/sponsors/manifest.json");
    const n = s && s.stats ? Number(s.stats.h1b) : NaN;
    if (Number.isFinite(n) && n > 0) h1b.textContent = fmt(n);
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
  form.addEventListener("submit", (e) => { e.preventDefault(); stopDemo(); checkEmployer(input.value); });
  document.querySelectorAll("[data-emp]").forEach((b) => {
    b.addEventListener("click", () => { stopDemo(); input.value = b.dataset.emp; checkEmployer(b.dataset.emp); input.focus(); });
  });

  // The typed demo: while nobody has touched the field, it types a sponsor's name letter by letter
  // with a blinking caret, runs the lookup so the record changes, holds, clears, and types the next.
  // It runs only while the panel is on screen, stops for good at the first click or key in the
  // field, and never runs for people who asked for less motion.
  const caret = $("emp-caret");
  const panel = $("employer");
  const NAMES = ["Stripe", "Epic Systems", "Mayo Clinic", "Netflix", "Databricks"];
  const lessMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let demo = !lessMotion && !!caret && !!panel;
  let visible = false, running = false, timer = 0;
  const wait = (ms) => new Promise((r) => { timer = setTimeout(r, ms); });
  const measure = (() => {
    const canvas = document.createElement("canvas");
    return (text) => { const c = canvas.getContext("2d"); if (!c) return 0; c.font = getComputedStyle(input).font; return c.measureText(text).width; };
  })();
  const show = (text) => { input.value = text; caret.style.setProperty("--w", measure(text) + "px"); };
  let whole = input.value;   // the name being typed or cleared, so a click mid-word leaves a whole name behind
  function stopDemo() { if (!demo) return; demo = false; clearTimeout(timer); caret.hidden = true; if (whole && whole.startsWith(input.value)) input.value = whole; }
  async function run() {
    if (running) return;
    running = true;
    try {
      await wait(1400);
      let i = 0;
      while (demo && visible) {
        const current = input.value;
        whole = current; caret.hidden = false;
        for (let n = current.length - 1; n >= 0 && demo && visible; n--) { show(current.slice(0, n)); await wait(28); }
        if (!demo || !visible) break;
        await wait(420);
        const name = NAMES[i % NAMES.length]; i++;
        whole = name;
        for (let n = 1; n <= name.length && demo && visible; n++) { show(name.slice(0, n)); await wait(55 + Math.random() * 50); }
        if (!demo || !visible) break;
        await wait(350);
        checkEmployer(name);
        await wait(2800);
      }
    } finally { running = false; if (demo) caret.hidden = true; }
  }
  ["pointerdown", "keydown", "focus", "input"].forEach((ev) => input.addEventListener(ev, stopDemo));
  if (demo && "IntersectionObserver" in window) {
    new IntersectionObserver((entries) => entries.forEach((e) => { visible = e.isIntersecting; if (visible && demo) run(); }), { threshold: 0.6 }).observe(panel);
  }
}

// ---- 3. downloads ------------------------------------------------------------------------
function detectOS() {
  const ua = navigator.userAgent || "";
  if (/Windows/i.test(ua)) return "win";
  if (/Macintosh|Mac OS X/i.test(ua)) return "mac";
  if (/Linux|X11|CrOS/i.test(ua) && !/Android/i.test(ua)) return "linux";
  return null;
}

// ---- 3a. the hero download control --------------------------------------------------------
// Three OS tabs over a pill, the way jetbrains.com/idea/download does it. Windows and Linux get
// one "Download" button that opens a menu of files; macOS gets a split pill: "Download" on the
// left fetches whatever the right half has picked. The visitor's own OS is selected first.
// Links start at the GitHub releases page and are swapped for the exact files once the release
// API answers (setDownloadUrls, called from releaseInfo).
const DL_URLS = {
  "win-x64": REPO_URL + "/releases/latest",
  "mac-arm64": REPO_URL + "/releases/latest",
  "src-zip": REPO_URL + "/archive/refs/heads/main.zip",
  "src-tgz": REPO_URL + "/archive/refs/heads/main.tar.gz",
  "src-intel": "https://github.com/KofiGilbert/sponsorjobs#install",
};
const DL_MENU = {
  win: { split: false, hint: 'Windows 10 or later, 64-bit. <a href="download.html">Install steps</a>',
    items: [{ key: "win-x64", label: ".exe installer (64-bit)" }, { key: "src-zip", label: "Source code (.zip)" }] },
  mac: { split: true,
    items: [
      { key: "mac-arm64", label: ".dmg (Apple Silicon)", hint: 'Apple Silicon installer. <a href="download.html">Install steps</a>' },
      { key: "src-intel", label: "Intel Mac: run from source", hint: 'No Intel installer yet. <a href="https://github.com/KofiGilbert/sponsorjobs#install" rel="noopener">Run it from source</a>.' },
    ] },
  linux: { split: false, hint: 'No Linux installer yet. <a href="https://github.com/KofiGilbert/sponsorjobs#install" rel="noopener">Run it from source</a>.',
    items: [{ key: "src-zip", label: "Source code (.zip)" }, { key: "src-tgz", label: "Source code (.tar.gz)" }] },
};
const dlRenders = [];   // one render per control on the page (the hero and the download card)

function setDownloadUrls(urls) {
  Object.assign(DL_URLS, urls);
  dlRenders.forEach((render) => render());
}

// Every ".dlc" on the page becomes a download control; each keeps its own chosen OS and file.
function wireDownloadControl() {
  document.querySelectorAll(".dlc").forEach((root, n) => wireOneDownloadControl(root, n));
}

function wireOneDownloadControl(root, n) {
  const tabs = [...root.querySelectorAll(".dlc-tab")];
  const pill = root.querySelector(".dlc-pill");
  const main = root.querySelector(".dlc-main");
  const pick = root.querySelector(".dlc-pick");
  const label = root.querySelector(".dlc-pick-label");
  const menu = root.querySelector(".dlc-menu");
  const hint = root.querySelector(".dlc-hint-text");
  if (!tabs.length || !pill || !main || !pick || !label || !menu || !hint) return;
  menu.id = "dlc-menu-" + n; pick.setAttribute("aria-controls", menu.id);
  const dlState = { os: "mac", picked: { mac: "mac-arm64" } };
  const detected = detectOS();
  if (detected && DL_MENU[detected]) dlState.os = detected;

  const check = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
  const closeMenu = () => { menu.hidden = true; pick.setAttribute("aria-expanded", "false"); };
  const openMenu = () => {
    menu.hidden = false; pick.setAttribute("aria-expanded", "true");
    const first = menu.querySelector('[aria-checked="true"]') || menu.querySelector(".dlc-item");
    if (first) first.focus();
  };

  function render() {
    const os = dlState.os;
    const spec = DL_MENU[os];
    tabs.forEach((t) => {
      const on = t.dataset.os === os;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
    });
    pill.classList.toggle("is-split", spec.split);
    menu.textContent = "";
    if (spec.split) {
      const picked = spec.items.find((i) => i.key === dlState.picked[os]) || spec.items[0];
      label.textContent = picked.label;
      main.href = DL_URLS[picked.key];
      main.setAttribute("aria-label", "Download " + picked.label);
      hint.innerHTML = picked.hint || spec.hint || "";
      spec.items.forEach((item) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "dlc-item"; b.setAttribute("role", "menuitemradio");
        b.setAttribute("aria-checked", item.key === picked.key ? "true" : "false");
        b.innerHTML = "<span></span>" + check;
        b.firstChild.textContent = item.label;
        b.addEventListener("click", () => { dlState.picked[os] = item.key; closeMenu(); render(); pick.focus(); });
        menu.append(b);
      });
    } else {
      label.textContent = "Download";
      main.removeAttribute("aria-label");
      hint.innerHTML = spec.hint || "";
      spec.items.forEach((item) => {
        const a = document.createElement("a");
        a.className = "dlc-item"; a.setAttribute("role", "menuitem"); a.href = DL_URLS[item.key];
        a.innerHTML = "<span></span>";
        a.firstChild.textContent = item.label;
        a.addEventListener("click", () => closeMenu());
        menu.append(a);
      });
    }
  }
  dlRenders.push(render);

  tabs.forEach((t, i) => {
    t.addEventListener("click", () => { closeMenu(); dlState.os = t.dataset.os; render(); });
    t.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      dlState.os = next.dataset.os; render(); next.focus();
    });
  });
  pick.addEventListener("click", () => (menu.hidden ? openMenu() : closeMenu()));
  pick.addEventListener("keydown", (e) => { if (e.key === "ArrowDown") { e.preventDefault(); openMenu(); } });
  menu.addEventListener("keydown", (e) => {
    const items = [...menu.querySelectorAll(".dlc-item")];
    const at = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); closeMenu(); pick.focus(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); items[(at + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(at - 1 + items.length) % items.length].focus(); }
    else if (e.key === "Tab") closeMenu();
  });
  document.addEventListener("click", (e) => { if (!menu.hidden && !pill.contains(e.target)) closeMenu(); });
  render();
}

// Older download buttons (the card at the foot of the home page and download.html) still ship
// with macOS as the main button. A Windows visitor gets Windows first; a Linux visitor is pointed
// at running it from source.
function markPrimaryForThisOS() {
  const os = detectOS();
  if (os !== "win" && os !== "linux") return;
  ["dl-main", "dl-get", "dl-primary"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    if (os === "win") { el.textContent = "Download for Windows"; el.dataset.os = "win"; }
  });
  const alt = $("dl-alt");
  if (alt) {
    alt.textContent = "";
    const a = document.createElement("a");
    a.href = os === "win" ? "download.html" : "https://github.com/KofiGilbert/sponsorjobs#install";
    a.textContent = os === "win" ? "Also on macOS" : "On Linux, run it from source";
    alt.append("Free and open source. ", a, ".");
  }
  const note = $("dl-get-note");
  if (note && os === "win") note.firstChild.textContent = "Windows 10 or later. ";
  const other = $("dl-other");
  if (other && os === "win") { other.dataset.os = "mac"; other.textContent = "Download for macOS (Apple Silicon)"; }
  const req = $("dl-req");
  if (req && os === "win") req.textContent = "Windows 10 or later, 64-bit.";
}

function mb(bytes) {
  const n = Number(bytes);
  return Number.isFinite(n) && n > 0 ? (n / 1048576).toFixed(0) + " MB" : null;
}

async function releaseInfo() {
  const links = document.querySelectorAll("a[data-os]");
  const meta = $("dl-meta");
  if (!links.length && !$("dlc")) return;
  const rel = await getJSON("https://api.github.com/repos/" + REPO + "/releases/latest", 8000);
  if (!rel || !Array.isArray(rel.assets)) return;   // the links already point at releases/latest
  const version = String(rel.tag_name || "").replace(/^v/, "");
  const date = rel.published_at ? shortDate(String(rel.published_at)) : null;
  const pick = (test) => rel.assets.find((a) => test(String(a.name || "")));
  const assets = {
    mac: pick((n) => /arm64\.dmg$/i.test(n)) || pick((n) => /\.dmg$/i.test(n)),
    win: pick((n) => /Setup.*\.exe$/i.test(n)) || pick((n) => /\.exe$/i.test(n)),
  };
  links.forEach((a) => {
    const asset = assets[a.dataset.os];
    if (asset && asset.browser_download_url) a.href = asset.browser_download_url;
  });
  const urls = {};
  if (assets.mac && assets.mac.browser_download_url) urls["mac-arm64"] = assets.mac.browser_download_url;
  if (assets.win && assets.win.browser_download_url) urls["win-x64"] = assets.win.browser_download_url;
  if (rel.tag_name) {
    urls["src-zip"] = REPO_URL + "/archive/refs/tags/" + encodeURIComponent(rel.tag_name) + ".zip";
    urls["src-tgz"] = REPO_URL + "/archive/refs/tags/" + encodeURIComponent(rel.tag_name) + ".tar.gz";
  }
  setDownloadUrls(urls);
  if (!meta) return;
  const primary = $("dl-primary");
  const asset = primary ? assets[primary.dataset.os] : null;
  const parts = [];
  if (version) parts.push("Version " + version);
  const size = asset ? mb(asset.size) : null;
  if (size) parts.push(size);
  if (date) parts.push("released " + date);
  if (parts.length) meta.textContent = parts.join(", ") + ".";
}


// ---- 5. feature tabs ---------------------------------------------------------------------
// ---- 5. features: text blocks scroll past a pinned app window --------------------------------
// The block nearest the middle of the pinned zone is the live one: its text is full strength, the
// rail's thumb sits on its fifth, its clip shows and plays, the tab names it. People who asked for
// less motion get the still frame of each clip instead.
const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
function wireValues() {
  const root = document.querySelector(".vals");
  if (!root) return;
  const blocks = [...root.querySelectorAll(".val")];
  const frames = [...root.querySelectorAll(".vals-frame")];
  const names = [...root.querySelectorAll(".vals-tag span")];
  const icons = [...root.querySelectorAll(".vals-tag svg")];
  const thumb = root.querySelector(".vals-thumb");
  const phone = root.querySelector(".phone");
  const media = root.querySelector(".vals-media");
  let current = -1;
  const show = (i) => {
    if (i === current) return;
    current = i;
    const section = root.closest("#product");
    if (section) section.dataset.i = String(i);   // the step's hue (halo, thumb, tab) follows this
    blocks.forEach((b, k) => b.classList.toggle("is-on", k === i));
    names.forEach((n, k) => n.classList.toggle("is-on", k === i));
    icons.forEach((s, k) => { if (k === i) s.removeAttribute("hidden"); else s.setAttribute("hidden", ""); });   // SVG has no .hidden property
    frames.forEach((f, k) => {
      const on = k === i;
      f.classList.toggle("is-on", on);
      const v = f.querySelector("video");
      if (!v) return;
      if (on) { if (!reducedMotion) { if (v.preload === "none") v.preload = "auto"; v.currentTime = 0; v.play().catch(() => {}); } }
      else { v.pause(); }
    });
    if (thumb) { thumb.style.height = (100 / blocks.length) + "%"; thumb.style.top = (i * 100 / blocks.length) + "%"; }
    if (phone) {
      const on = frames[i] && frames[i].nextElementSibling === phone;
      phone.classList.remove("is-on");
      if (on) { void phone.offsetWidth; phone.classList.add("is-on"); }
    }
  };
  // The live block, by the same rule jetbrains.com uses (read from its page script): a block is
  // live while its top is above the bottom of the pinned window and its bottom is more than 100px
  // below the window's top; the first block also while its bottom is below the window's top. When
  // several qualify, the last one wins.
  const pick = () => {
    if (!media || narrow.matches) return;
    const stickyTop = parseFloat(getComputedStyle(media).top) || 170;
    const winH = media.getBoundingClientRect().height || 500;
    let i = 0;
    blocks.forEach((b, k) => {
      const r = b.getBoundingClientRect();
      const on = k === 0 ? r.bottom >= stickyTop : (r.top < winH && r.bottom > stickyTop + 100);
      if (on) i = k;
    });
    show(i);
  };
  let ticking = false;
  const onScroll = () => { if (!ticking) { ticking = true; requestAnimationFrame(() => { pick(); ticking = false; }); } };
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);
  if (reducedMotion) frames.forEach((f) => { const v = f.querySelector("video"); if (v) { v.removeAttribute("autoplay"); v.pause(); } });

  // Narrow screens: jetbrains.com swaps the pinned layout for one card per block, each with its own
  // picture. Here each block takes its own clip (and the Apply block its phone), and a clip plays
  // while its card is mostly on screen.
  const narrow = window.matchMedia("(max-width: 960px)");
  const framesHome = root.querySelector(".vals-frames");
  let io = null;
  const layout = () => {
    if (narrow.matches) {
      frames.forEach((f, k) => { const b = blocks[k]; if (b && f.parentElement !== b) { b.append(f); if (phone && k === 2) b.append(phone); } });
      frames.forEach((f) => f.classList.add("is-on"));
      if (phone) phone.classList.add("is-on");
      if (!io && "IntersectionObserver" in window) {
        io = new IntersectionObserver((entries) => entries.forEach((e) => { const v = e.target.querySelector("video"); if (!v) return; if (e.isIntersecting && !reducedMotion) { if (v.preload === "none") v.preload = "auto"; v.play().catch(() => {}); } else v.pause(); }), { threshold: 0.5 });
        frames.forEach((f) => io.observe(f));
      }
    } else {
      frames.forEach((f) => { if (f.parentElement !== framesHome) framesHome.append(f); });
      if (phone && phone.parentElement !== framesHome) framesHome.insertBefore(phone, frames[3] || null);
      if (io) { io.disconnect(); io = null; }
      current = -1;
      pick();
    }
  };
  narrow.addEventListener("change", layout);
  layout();
  if (!narrow.matches) pick();
}

// ---- 6. the header's scroll edge -------------------------------------------------------------
// The header is a floating layer; its edge shows only once content has scrolled under it.
function wireHeaderEdge() {
  const head = document.querySelector(".site-head");
  if (!head) return;
  let ticking = false;
  const update = () => { head.classList.toggle("is-scrolled", window.scrollY > 2); ticking = false; };
  window.addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
  update();
}

wireValues();
wireHeaderEdge();
wireEmployerPanel();
wireDownloadControl();
markPrimaryForThisOS();
liveNumbers();
releaseInfo();
