// Background service worker, the bridge between a job page and your LOCAL SponsorJobs app.
// Content scripts (running on linkedin.com etc.) send us a company name; we ask the
// local app (127.0.0.1:57000) for its visa-sponsor flags and hand the answer back.
// Fetching from the service worker (with host_permissions for 127.0.0.1) bypasses the
// page's CORS rules AND lets us send the X-Tailor-Extension header the app's private
// endpoints require, a web page can't set that header cross-origin, so those endpoints
// (your saved PII, your API credits) aren't reachable from any site you visit.

const APP = "http://127.0.0.1:57000";
const EXT_HEADER = { "X-Tailor-Extension": "1" };   // required by the app's guarded routes
const CACHE = new Map();          // company(lowercased) -> {t, data}
const TTL_MS = 10 * 60 * 1000;    // 10 min per company

// One place that adds the header, checks the HTTP status (so an app error like a 403 or
// 400 is reported as {ok:false} rather than spread into a fake {ok:true}), and maps a
// dropped connection to a clear "app not running" signal.
async function call(path, opts = {}) {
  try {
    const r = await fetch(`${APP}${path}`, {
      cache: "no-store", ...opts,
      headers: { ...EXT_HEADER, ...(opts.headers || {}) },
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: data.error || `http_${r.status}`, status: r.status };
    return { ok: true, ...data };
  } catch {
    return { ok: false, error: "cant_reach_app" };   // app not running
  }
}

const postJSON = (path, body) => call(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
});

// The cache key includes the LOCATION, not just the employer. Sponsor data is per
// employer, but the badge is per ROLE: Spotify New York and Spotify London get different
// answers, and keying on company alone would serve the first one's badge to the second.
async function lookup(company, location) {
  const key = `${(company || "").trim().toLowerCase()}|${(location || "").trim().toLowerCase()}`;
  if (!key.replace("|", "")) return { ok: true, matched: false, visa: [] };
  const hit = CACHE.get(key);
  if (hit && Date.now() - hit.t < TTL_MS) return hit.data;
  const q = `company=${encodeURIComponent(company || "")}&location=${encodeURIComponent(location || "")}`;
  const data = await call(`/api/sponsors/lookup?${q}`);
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
