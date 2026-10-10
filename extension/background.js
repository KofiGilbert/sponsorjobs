// Background service worker. Two jobs:
//
// 1. VISA BADGES, with or without the SponsorJobs app. Content scripts (on linkedin.com etc.)
//    send us a company name; we answer from the sponsor INDEX the feed publishes
//    (scripts/build_sponsor_index.py -> https://feed.sponsorjobs.ai/feed/sponsors/), matched
//    here in the browser by sponsor_core.js, a port of the app's own matcher. Private by
//    design: we download whole shards (one per first letter) and never send a company name
//    anywhere. Shards are cached in IndexedDB and the manifest is re-checked at most once a
//    day, so after the first few lookups this is offline. Only if the index can't be had
//    (first run while offline, feed down) do we ask the local app, as before.
//
// 2. A bridge to your LOCAL SponsorJobs app (127.0.0.1:57000) for the app-only features: match
//    score, autofill profile, drafted answers, tracking. Fetching from the service worker (with
//    host_permissions for 127.0.0.1) bypasses the page's CORS rules AND lets us send the
//    X-Tailor-Extension header the app's private endpoints require; a web page can't set that
//    header cross-origin, so those endpoints (your saved PII, your API credits) aren't reachable
//    from any site you visit. With no app running these answer {ok:false, error:"cant_reach_app"}
//    and the page shows an install prompt instead.

importScripts("sponsor_core.js");

const APP = "http://127.0.0.1:57000";
const EXT_HEADER = { "X-Tailor-Extension": "1" };   // required by the app's guarded routes
const CACHE = new Map();          // company|location -> {t, data}
const TTL_MS = 10 * 60 * 1000;    // 10 min per company

// Whether the app answered lately (true/false), or null before we have asked. Lets the page
// point "Tailor my CV" at the app when it's there and at the install page when it isn't.
const APP_STATE = { up: null, at: 0 };

// One place that adds the header, checks the HTTP status (so an app error like a 403 or
// 400 is reported as {ok:false} rather than spread into a fake {ok:true}), and maps a
// dropped connection to a clear "app not running" signal.
async function call(path, opts = {}) {
  try {
    const r = await fetch(`${APP}${path}`, {
      cache: "no-store", ...opts,
      headers: { ...EXT_HEADER, ...(opts.headers || {}) },
    });
    APP_STATE.up = true; APP_STATE.at = Date.now();
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: data.error || `http_${r.status}`, status: r.status };
    return { ok: true, ...data };
  } catch {
    APP_STATE.up = false; APP_STATE.at = Date.now();
    return { ok: false, error: "cant_reach_app" };   // app not running
  }
}

const postJSON = (path, body) => call(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
});

// ================================ the in-extension sponsor index ===========================
const INDEX_BASE = "https://feed.sponsorjobs.ai/feed/sponsors";
const INDEX_VERSION = 1;                       // the shard format sponsor_core.js reads
const DAY_MS = 24 * 60 * 60 * 1000;
const RETRY_MS = 5 * 60 * 1000;                // after a failed manifest fetch with nothing cached
const STORE_KEY = "sj_sponsor_index";          // chrome.storage.local: {manifest, checkedAt}
const SHARD_FILE = /^[a-z0-9_]+\.json\.gz$/;   // never follow a manifest to an odd path

let INDEX = null;                 // {manifest, checkedAt}, mirrored in chrome.storage.local
let manifestFailAt = 0;
let manifestPending = null;
const SHARDS = new Map();         // key -> {version, shard}  (prepared, in memory)
const shardPending = new Map();   // key -> Promise

// ---- IndexedDB: shard text by key, tagged with the index version it came from ----
function idb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("sponsorjobs-index", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("shards", { keyPath: "key" });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idbDo(mode, fn) {
  const db = await idb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction("shards", mode);
      const req = fn(tx.objectStore("shards"));
      tx.oncomplete = () => resolve(req && req.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally { db.close(); }
}
const idbGet = (key) => idbDo("readonly", (s) => s.get(key)).catch(() => null);
const idbPut = (row) => idbDo("readwrite", (s) => s.put(row)).catch(() => null);
const idbClear = () => idbDo("readwrite", (s) => s.clear()).catch(() => null);

// The manifest, re-checked at most daily. A failed check keeps the cached one (and tries again
// in an hour); with nothing cached it returns null so the lookup can fall back to the app.
async function getManifest() {
  if (!INDEX) {
    try { INDEX = (await chrome.storage.local.get(STORE_KEY))[STORE_KEY] || null; } catch { INDEX = null; }
  }
  if (INDEX && Date.now() - INDEX.checkedAt < DAY_MS) return INDEX.manifest;
  if (!INDEX && Date.now() - manifestFailAt < RETRY_MS) return null;
  if (!manifestPending) manifestPending = refreshManifest().finally(() => { manifestPending = null; });
  return manifestPending;
}

async function refreshManifest() {
  try {
    const r = await fetch(`${INDEX_BASE}/manifest.json`, { cache: "no-cache" });
    if (!r.ok) throw new Error(`http_${r.status}`);
    const m = await r.json();
    if (!m || m.index_version !== INDEX_VERSION || !m.version || !m.shards) throw new Error("bad_manifest");
    if (INDEX && INDEX.manifest.version !== m.version) {   // new data: drop the old shards
      SHARDS.clear();
      await idbClear();
    }
    INDEX = { manifest: m, checkedAt: Date.now() };
    try { await chrome.storage.local.set({ [STORE_KEY]: INDEX }); } catch {}
    return m;
  } catch {
    manifestFailAt = Date.now();
    if (INDEX) { INDEX.checkedAt = Date.now() - DAY_MS + 60 * 60 * 1000; return INDEX.manifest; }
    return null;
  }
}

// gzip bytes (GitHub Pages serves the .gz as-is) or already-decoded JSON (a host that sends
// Content-Encoding: gzip, which fetch undoes for us). Both arrive here as one string.
async function bodyText(r) {
  const buf = new Uint8Array(await r.arrayBuffer());
  if (buf[0] === 0x1f && buf[1] === 0x8b) {
    const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
    return await new Response(stream).text();
  }
  return new TextDecoder().decode(buf);
}

async function fetchShard(key, m) {
  const meta = m.shards[key];
  if (!meta) return SponsorCore.prepareShard({});         // nothing published under this letter
  if (!SHARD_FILE.test(meta.file || "")) throw new Error("bad_shard_name");
  const cached = await idbGet(key);
  let text = cached && cached.version === m.version ? cached.json : null;
  if (text == null) {
    const r = await fetch(`${INDEX_BASE}/${meta.file}`);
    if (!r.ok) throw new Error(`http_${r.status}`);
    text = await bodyText(r);
    const doc = JSON.parse(text);
    if (doc.version !== m.version && INDEX) INDEX.checkedAt = 0;   // mid-deploy: recheck soon
    await idbPut({ key, version: m.version, json: text });
    return SponsorCore.prepareShard(doc.records);
  }
  return SponsorCore.prepareShard(JSON.parse(text).records);
}

async function loadShard(key, m) {
  const have = SHARDS.get(key);
  if (have && have.version === m.version) return have.shard;
  if (!shardPending.has(key)) {
    shardPending.set(key, fetchShard(key, m)
      .then((shard) => { SHARDS.set(key, { version: m.version, shard }); return shard; })
      .finally(() => shardPending.delete(key)));
  }
  return shardPending.get(key);
}

// The app's /api/sponsors/lookup answer, computed here. Only shard FILE NAMES go over the
// network (one per first letter), never the company. null when the index is unavailable.
async function indexLookup(company, location) {
  try {
    const m = await getManifest();
    if (!m) return null;
    const keys = SponsorCore.shardsFor(company);
    const loaded = {};
    await Promise.all(keys.map(async (k) => { loaded[k] = await loadShard(k, m); }));
    const data = SponsorCore.lookupResponse(company, location, (k) => loaded[k] || null, m.employers);
    if (Date.now() - APP_STATE.at > 60 * 1000) call("/api/sponsors/status");   // refresh app presence
    return { ok: true, ...data, source: "index", data_version: m.version, app: APP_STATE.up };
  } catch {
    return null;
  }
}

async function indexStatus() {
  const m = await getManifest();
  if (!m) return { ok: false, error: "index_unavailable" };
  return { ok: true, ...(m.stats || {}), employers: m.employers, version: m.version,
           generated_at: m.generated_at };
}

// The cache key includes the LOCATION, not just the employer. Sponsor data is per
// employer, but the badge is per ROLE: Spotify New York and Spotify London get different
// answers, and keying on company alone would serve the first one's badge to the second.
async function lookup(company, location) {
  const key = `${(company || "").trim().toLowerCase()}|${(location || "").trim().toLowerCase()}`;
  if (!key.replace("|", "")) return { ok: true, matched: false, visa: [] };
  const hit = CACHE.get(key);
  if (hit && Date.now() - hit.t < TTL_MS) return hit.data;
  let data = await indexLookup(company, location);
  if (!data) {                    // no index (offline first run, feed down): ask the app
    const q = `company=${encodeURIComponent(company || "")}&location=${encodeURIComponent(location || "")}`;
    data = await call(`/api/sponsors/lookup?${q}`);
    if (data.ok) data = { ...data, source: "app", app: true };
  }
  if (data.ok) CACHE.set(key, { t: Date.now(), data });
  return data;
}

// Profile-vs-JD match, cached per JD text (a card's JD is stable, so we ask once per posting).
const MATCH_CACHE = new Map();   // jd-hash -> {t, data}
async function matchScore(jd) {
  const key = (jd || "").trim().slice(0, 4000);
  if (!key) return { ok: true, has_profile: null, score: null };
  const hit = MATCH_CACHE.get(key);
  if (hit && Date.now() - hit.t < TTL_MS) return hit.data;
  const data = await postJSON("/api/match/score", { jd: key });
  if (data.ok) MATCH_CACHE.set(key, { t: Date.now(), data });
  return data;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || !msg.type) return;
  if (msg.type === "lookup")   { lookup(msg.company, msg.location).then(sendResponse); return true; }
  if (msg.type === "match")    { matchScore(msg.jd).then(sendResponse); return true; }
  if (msg.type === "tracking") { call("/api/tracking/summary").then(sendResponse); return true; }
  if (msg.type === "status")   { call("/api/sponsors/status").then(sendResponse); return true; }
  if (msg.type === "index_status") { indexStatus().then(sendResponse); return true; }
  if (msg.type === "autofill") { call("/api/profile/autofill").then(sendResponse); return true; }
  if (msg.type === "prefs_get") { call("/api/profile/prefs").then(sendResponse); return true; }
  if (msg.type === "prefs_set") { postJSON("/api/profile/prefs", msg.prefs).then(sendResponse); return true; }
  if (msg.type === "draft") {
    postJSON("/api/profile/draft", {
      jd: msg.jd || "", questions: msg.questions || [], cover_letter: !!msg.cover_letter,
    }).then(sendResponse);
    return true;
  }
  if (msg.type === "referral") {
    postJSON("/api/profile/referral", {
      role: msg.role || "", company: msg.company || "", jd: msg.jd || "",
      recipient_name: msg.recipient_name || "", relationship: msg.relationship || "",
    }).then(sendResponse);
    return true;
  }
});
