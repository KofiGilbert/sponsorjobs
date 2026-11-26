// Did the person actually send this application? ONE detector for the whole shell (2026-11-26).
//
// The app never submits: the person presses the site's own Submit. Until now the only way to
// know it went through was the confirmation-page watch (applied_watch.js), and it missed a form
// inside a frame that reset itself to blank after Submit (Instacart's Greenhouse frame on
// instacart.careers, 2026-11-26: the employer emailed a confirmation, the app saw nothing).
// So the question is answered from several independent kinds of evidence, each with a
// confidence, combined per application, and application:submitted is announced ONCE per record:
//
//   page        the site's thank-you text, in the top page or in ANY frame: the recipe's marker
//               (0.95) or the model reading the changed page (0.9). Either is enough alone.
//   network     after the person's own (trusted) click on a submit-like control, a POST / PUT
//               from the application page that the server answered 2xx (or a 3xx redirect, a
//               classic form post): a known applicant tracking system's submit endpoint (0.8) or
//               any form post to the application's own site (0.65). Never enough alone: a site
//               that answers 200 with "please fix the errors" keeps the filled form on screen.
//   form-reset  the filled form went blank, or went away, after that click (0.6). Never enough
//               alone; network + form-reset is (Instacart's case).
//   popup       the site opened a new window right after the click (0.3), a hint only.
//   said-yes    the person said "yes, I sent it" (1.0).
//
// (The person's own confirmation EMAIL is the engine's evidence: ui/app.py, inbox/matching.py.)
//
// Combined as independent signals, 1 - prod(1 - c) over the kinds seen; it fires at FIRE_AT.
// Everything that depends on the click (network, form-reset, popup) belongs to the LAST click:
// a new click starts them over, so an earlier failed attempt never adds to a later one.
//
// Privacy: a request is reduced at once to method, host, a path shape (numbers and ids blanked)
// and status. Request bodies, query strings and form values are never read, kept or logged.
//
// Pure (no Electron): tests/js/test_sent_detect.js runs it under node. main.js owns the wiring;
// other shell code (the frame walk) only calls signal().
"use strict";

const WEIGHT = {
  "page-marker": 0.95,
  "page-model": 0.9,
  "network-ats": 0.8,
  "network": 0.65,
  "form-reset": 0.6,
  "popup": 0.3,
  "said-yes": 1,
};
const FIRE_AT = 0.85;
const CLICK_WINDOW_MS = 2 * 60 * 1000;     // what follows a click this soon is about that click
const MIN_FILLED = 3;                      // a "reset" needs a form that really was filled

// --------------------------------------------------------------------------- sites and paths

// "jobs.example.co.uk" -> "example.co.uk"; "boards.greenhouse.io" -> "greenhouse.io".
function siteOf(url) {
  let host = "";
  try { host = (new URL(url).hostname || "").toLowerCase(); } catch (e) { return ""; }
  return siteOfHost(host);
}
function siteOfHost(host) {
  const parts = String(host || "").toLowerCase().replace(/^www\./, "").split(".").filter(Boolean);
  if (parts.length <= 2) return parts.join(".");
  const second = parts[parts.length - 2];
  const short = parts[parts.length - 1].length === 2 && /^(co|com|ac|org|net|gov|edu)$/.test(second);
  return parts.slice(short ? -3 : -2).join(".");
}

// The path with its numbers and ids blanked, short: "/acme/jobs/:n". Never the query.
function pathShape(pathname) {
  return String(pathname || "/")
    .split("/")
    .map((seg) => (/^\d+$/.test(seg) ? ":n"
      : /^[0-9a-f-]{16,}$/i.test(seg) || /^[A-Za-z0-9_-]{24,}$/.test(seg) ? ":id" : seg))
    .join("/")
    .slice(0, 80);
}

// Known submit endpoints. Host first (the hiring system's own family), then the path; Ashby
// names the operation in its address (?op=...), the only query part ever looked at.
const ATS = [
  { ats: "Greenhouse", host: /(^|\.)greenhouse\.io$/, path: /\/jobs\/\d+\/?$|\/job_app\/?$|\/applications?\/?$|\/apply\/?$/i },
  { ats: "Lever", host: /(^|\.)lever\.co$/, path: /\/apply\/?$/i },
  { ats: "Ashby", host: /(^|\.)ashbyhq\.com$/, path: /\/application(s)?\/(submit|form)/i,
    op: /submit.*application|application.*submit/i },
  { ats: "Workday", host: /(^|\.)(myworkdayjobs|myworkday|myworkdaysite|workday)\.com$/, path: /(^|\/)submit(application)?(\/|$)/i },
  { ats: "iCIMS", host: /(^|\.)icims\.com$/, path: /(^|\/)(submit|submitapplication)(\/|$)/i },
  { ats: "SmartRecruiters", host: /(^|\.)smartrecruiters\.com$/, path: /\/applications?(\/|$)|(^|\/)submit(\/|$)/i },
  { ats: "SuccessFactors", host: /(^|\.)(successfactors\.(com|eu)|sapsf\.(com|eu))$/, path: /submit|applyjob|jobapply/i },
  { ats: "Taleo", host: /(^|\.)taleo\.net$/, path: /submit|apply\.jsf/i },
  { ats: "Workable", host: /(^|\.)workable\.com$/, path: /\/apply\/?$|\/candidates?\/?$/i },
  { ats: "Recruitee", host: /(^|\.)recruitee\.com$/, path: /\/candidates\/?$/i },
  { ats: "Jobvite", host: /(^|\.)jobvite\.com$/, path: /(^|\/)(apply|submit)(\/|$)/i },
  { ats: "BambooHR", host: /(^|\.)bamboohr\.com$/, path: /applicationsubmit|\/apply\/?$/i },
];

// Requests that are never an application: tracking, error reports, captchas, chat widgets.
const NOISE_HOST = /(^|\.)(google-analytics|googletagmanager|doubleclick|googlesyndication|google|gstatic|recaptcha|hcaptcha|cloudflare|cloudflareinsights|segment|segment\.io|sentry|sentry\.io|datadoghq|browser-intake-datadoghq|hotjar|fullstory|newrelic|nr-data|amplitude|mixpanel|intercom|intercomcdn|launchdarkly|optimizely|heapanalytics|clarity|bing|facebook|linkedin|licdn|twitter|tiktok|onetrust|cookielaw|trustarc|qualtrics|pendo|walkme|zendesk|bugsnag|logrocket|statsig|braze|appcues|px-cloud|perimeterx|arkoselabs|funcaptcha)\.[a-z.]+$/i;
const NOISE_PATH = /(^|\/)(log|logs|logging|track|tracking|analytics|events?|collect|beacon|metrics|telemetry|rum|pixel|ping|heartbeat|autosave|save_?draft|draft|validate|validation|upload|uploads|parse|resume_?parse|parse_?resume|presign|presigned|s3|attachments?|files?|session|csrf|token|refresh|login|signin|sign_in|auth|oauth|consent|cookies?|captcha|graphql\/log)(\/|$|\.)/i;

// {kind: "network-ats" | "network", ats, method, host, path, status} for a request that can be
// an application being sent, else null. ``sites``: the application's own sites (its pages and
// frames); a generic post counts only to one of them.
function classifyRequest(req, sites) {
  const method = String((req && req.method) || "").toUpperCase();
  if (!/^(POST|PUT|PATCH)$/.test(method)) return null;
  const status = Number(req.status) || 0;
  if (!(status >= 200 && status < 400)) return null;
  let u = null;
  try { u = new URL(req.url); } catch (e) { return null; }
  if (!/^https?:$/.test(u.protocol)) return null;
  const host = u.hostname.toLowerCase();
  if (NOISE_HOST.test(host)) return null;
  const out = { method, host, path: pathShape(u.pathname), status };
  for (const a of ATS) {
    if (!a.host.test(host)) continue;
    let op = "";
    try { op = u.searchParams.get("op") || u.searchParams.get("operationName") || ""; } catch (e) { op = ""; }
    if (a.path.test(u.pathname) || (a.op && op && a.op.test(op))) return Object.assign(out, { kind: "network-ats", ats: a.ats });
  }
  if (NOISE_PATH.test(u.pathname)) return null;
  const type = String(req.type || "");
  if (type && !/^(Document|XHR|Fetch|Other)$/i.test(type)) return null;
  const site = siteOfHost(host);
  if (!sites || !site || !(sites.has ? sites.has(site) : sites.includes(site))) return null;
  return Object.assign(out, { kind: "network", ats: "" });
}

// The words the Tracker's timeline shows for each kind of evidence.
function labelOf(e) {
  if (!e) return "";
  if (e.kind === "page-marker" || e.kind === "page-model") return "thank-you page";
  if (e.kind === "network-ats") return "sent to " + (e.ats || "the hiring system");
  if (e.kind === "network") return "sent to the site";
  if (e.kind === "form-reset") return e.gone ? "form closed after Submit" : "form cleared after Submit";
  if (e.kind === "popup") return "the site opened a window";
  if (e.kind === "said-yes") return "you said yes";
  return "";
}
// The family each kind belongs to: one per family counts (the strongest).
const FAMILY = { "page-marker": "page", "page-model": "page", "network-ats": "network", "network": "network",
                 "form-reset": "form", "popup": "popup", "said-yes": "said" };
const CLICK_BOUND = new Set(["network", "form", "popup"]);

function combine(list) {
  let miss = 1;
  for (const e of list) miss *= 1 - Math.max(0, Math.min(1, e.confidence));
  return 1 - miss;
}

// ------------------------------------------------------------------------------- the probe

// Read in the page (the top page, or a frame's isolated world). Arms once per document a note of
// the person's OWN click (event.isTrusted) on a submit-like control, with how many fields were
// filled at that moment, kept in sessionStorage so a navigation or a frame reset still knows.
// Returns {url, clickAt, filledAtClick, filledNow, fieldsNow}. Counts only, never a value.
const CLICK_PROBE_JS = `(() => {
  const SUBMITISH = /^(submit|send|apply|finish|complete|confirm)\\b|\\b(submit|send) (my |your |the )?application\\b/i;
  const NOT = /^(next|continue|back|previous|save|upload|add|remove|delete|cancel|sign ?in|log ?in|search|attach|edit|close)\\b/i;
  const words = (el) => String((el && (el.innerText || el.value || el.getAttribute("aria-label") || el.title)) || "").replace(/\\s+/g, " ").trim().slice(0, 80);
  const count = () => {
    let filled = 0, fields = 0;
    for (const el of document.querySelectorAll("input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='reset']):not([type='image']), textarea, select")) {
      const r = el.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) continue;
      fields++;
      const t = (el.type || "").toLowerCase();
      if (t === "checkbox" || t === "radio") { if (el.checked) filled++; }
      else if (el.tagName === "SELECT") { if (el.selectedIndex > 0) filled++; }
      else if (t === "file") { if (el.files && el.files.length) filled++; }
      else if (String(el.value || "").trim()) filled++;
    }
    return { filled, fields };
  };
  if (!window.__sjSentProbe) {
    window.__sjSentProbe = true;
    const note = (e) => {
      try {
        if (!e.isTrusted) return;
        const b = e.target && e.target.closest && e.target.closest("button, [role='button'], input[type='submit'], input[type='button'], a");
        if (!b) return;
        const w = words(b);
        const typed = (b.type || "").toLowerCase() === "submit";
        if (NOT.test(w) || !(SUBMITISH.test(w) || (typed && !w))) return;
        const c = count();
        const rec = JSON.stringify({ at: Date.now(), filled: c.filled });
        window.__sjSentClick = rec;
        try { sessionStorage.setItem("__sjSentClick", rec); } catch (e2) { /* storage off */ }
      } catch (e3) { /* never in the page's way */ }
    };
    document.addEventListener("pointerdown", note, true);
    document.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") note(e); }, true);
  }
  let rec = null;
  try { rec = JSON.parse(window.__sjSentClick || sessionStorage.getItem("__sjSentClick") || "null"); } catch (e) { rec = null; }
  const c = count();
  return { url: location.href, clickAt: rec ? Number(rec.at) || 0 : 0, filledAtClick: rec ? Number(rec.filled) || 0 : 0,
           filledNow: c.filled, fieldsNow: c.fields };
})()`;

// ---------------------------------------------------------------------------- the detector

// o: {now() -> ms, onFire(result), onAsk(ask)}.
//   result: {recordId, url, pageUrl, how, label, confidence, evidence: [{kind, label, confidence, ...}], res}
//   ask:    {recordId, url, reason, clicked}
function createDetector(o) {
  o = o || {};
  const now = o.now || (() => Date.now());
  const fired = new Set();          // record keys announced this session
  let cur = null;

  const keyOf = (st) => (st.recordId != null ? "r:" + st.recordId : "u:" + (st.startUrl || "").split(/[?#]/)[0]);

  function snapshot(st) {
    const evidence = [...st.evidence.values()].sort((a, b) => b.confidence - a.confidence);
    return { recordId: st.recordId, url: st.startUrl, pageUrl: st.pageUrl || st.fillUrl || st.startUrl,
             evidence, confidence: combine(evidence), label: evidence.length ? evidence[0].label : "",
             clicked: !!st.clickAt };
  }
  function howOf(ev) {
    if (!ev.length) return "";
    if (ev.length === 1 || ev[0].confidence >= FIRE_AT) {
      const k = ev[0].kind;
      if (k === "page-marker") return "marker";
      if (k === "page-model") return "model";
      if (k === "said-yes") return "said-yes";
      if (ev.length === 1) return k.startsWith("network") ? "network" : k;
    }
    return "combined";
  }

  function add(st, e) {
    const fam = FAMILY[e.kind];
    if (!fam) return;
    e.confidence = WEIGHT[e.kind];
    e.label = labelOf(e);
    e.at = now();
    const had = st.evidence.get(fam);
    if (!had || had.confidence < e.confidence) st.evidence.set(fam, e);
    maybeFire(st, e);
  }

  function maybeFire(st, last) {
    if (st.fired) return;
    const snap = snapshot(st);
    if (snap.confidence < FIRE_AT) return;
    st.fired = true;
    fired.add(keyOf(st));
    snap.how = howOf(snap.evidence);
    snap.res = (last && last.res) || null;
    if (last && last.pageUrl) snap.pageUrl = last.pageUrl;
    if (o.onFire) { try { o.onFire(snap); } catch (e) { /* the shell's own error */ } }
  }

  function clickBoundReset(st) {
    for (const fam of CLICK_BOUND) st.evidence.delete(fam);
  }

  function onClick(st, at, filled, where) {
    if (!at || at <= st.clickAt) return;
    st.clickAt = at;
    st.clickFilled = Number(filled) || 0;
    st.clickWhere = where || "";
    clickBoundReset(st);
  }
  const afterClick = (st) => st.clickAt && now() - st.clickAt <= CLICK_WINDOW_MS;

  function signal(s) {
    s = s || {};
    const st = cur;
    if (!st || st.fired) return null;
    if (s.recordId != null && st.recordId != null && String(s.recordId) !== String(st.recordId)) return null;
    const where = String(s.frameUrl || s.pageUrl || s.url || "");
    const site = siteOf(where);
    if (site && (s.frame || s.kind === "probe")) st.sites.add(site);
    switch (s.kind) {
      case "submit-click":
        onClick(st, Number(s.at) || now(), s.filled, where);
        break;
      case "probe": {
        // a page or frame's CLICK_PROBE_JS answer: a new click, and whether the form went blank
        if (s.clickAt) onClick(st, Number(s.clickAt), s.filledAtClick, where);
        if (!afterClick(st) || !s.clickAt || Number(s.clickAt) < st.clickAt) break;
        const before = Number(s.filledAtClick) || 0;
        const gone = Number(s.fieldsNow) === 0;
        if (before >= MIN_FILLED && (gone || Number(s.filledNow) === 0)) add(st, { kind: "form-reset", gone, where: site });
        break;
      }
      case "form-reset": {
        if (!afterClick(st)) break;
        const before = Number(s.filledBefore != null ? s.filledBefore : st.clickFilled) || 0;
        const after = Number(s.filledAfter) || 0;
        if (before >= MIN_FILLED && after === 0) add(st, { kind: "form-reset", gone: !!s.gone, where: site });
        break;
      }
      case "page":
        add(st, { kind: s.how === "model" ? "page-model" : "page-marker", frame: !!s.frame,
                  pageUrl: s.pageUrl || "", res: s.res || null });
        break;
      case "network": {
        if (!afterClick(st)) break;
        const r = classifyRequest(s, st.sites);
        if (r) add(st, Object.assign({}, r, { kind: r.kind }));
        break;
      }
      case "popup":
        if (afterClick(st)) add(st, { kind: "popup", where: site });
        break;
      case "said-yes":
        add(st, { kind: "said-yes" });
        break;
      default:
        return null;
    }
    return snapshot(st);
  }

  return {
    // A fill ran: watch this application. The same record filled again (the next page of the
    // form) keeps what was seen; another record first gives the earlier one its ask.
    arm(a) {
      a = a || {};
      const recordId = a.recordId == null || a.recordId === "" ? null : a.recordId;
      if (cur && !cur.fired && recordId != null && String(cur.recordId) === String(recordId)) {
        cur.fillUrl = a.fillUrl || cur.fillUrl;
        for (const u of [a.startUrl, a.fillUrl]) { const s = siteOf(u); if (s) cur.sites.add(s); }
        return snapshot(cur);
      }
      if (cur && !cur.fired) this.leave("switched");
      const st = { recordId, startUrl: a.startUrl || "", fillUrl: a.fillUrl || "", pageUrl: "",
                   sites: new Set([siteOf(a.startUrl), siteOf(a.fillUrl)].filter(Boolean)),
                   clickAt: 0, clickFilled: 0, clickWhere: "", evidence: new Map(), fired: false, asked: false };
      if (a.again) fired.delete(keyOf(st));
      if (fired.has(keyOf(st))) st.fired = true;      // announced already this session: once per record
      cur = st;
      return snapshot(st);
    },
    signal,
    // "Fill again" on an application already announced: its next send counts again.
    forget(recordId) {
      fired.delete("r:" + recordId);
      if (cur && cur.fired && String(cur.recordId) === String(recordId)) cur = null;
    },
    // The person moved on with no evidence: ask them ONCE, "Did you send it?". reason:
    // "left-site" | "closed" | "switched". Anything else (the watch timed out, the agent
    // recorded the send itself) only lets go: the Tracker's own ask covers it later.
    leave(reason) {
      const st = cur;
      if (!st) return null;
      let ask = null;
      if (!st.fired && !st.asked && /^(left-site|closed|switched)$/.test(String(reason || ""))) {
        st.asked = true;
        ask = { recordId: st.recordId, url: st.startUrl, reason, clicked: !!st.clickAt };
        if (o.onAsk) { try { o.onAsk(ask); } catch (e) { /* the shell's own error */ } }
      }
      if (reason !== "switched") cur = null;
      return ask;
    },
    state: () => (cur ? snapshot(cur) : null),
    armed: () => !!cur && !cur.fired,
    wasFired: (recordId) => fired.has("r:" + recordId),
    sites: () => (cur ? [...cur.sites] : []),
  };
}

// The shell's one detector. main.js configures it; any other shell code only calls signal().
let _default = null;
let _hooks = {};
function shared() {
  if (!_default) _default = createDetector({ onFire: (r) => _hooks.onFire && _hooks.onFire(r),
                                             onAsk: (a) => _hooks.onAsk && _hooks.onAsk(a) });
  return _default;
}
function configure(hooks) { _hooks = Object.assign({}, _hooks, hooks || {}); return shared(); }
// Listeners that only watch the signals (the frame walk's tests); the detector decides.
const _watchers = new Set();
function signal(s) {
  for (const w of _watchers) { try { w(Object.assign({}, s || {}, { at: (s && s.at) || Date.now() })); } catch (e) { /* theirs */ } }
  return shared().signal(s);
}
function onSignal(cb) { _watchers.add(cb); return () => _watchers.delete(cb); }

module.exports = {
  WEIGHT, FIRE_AT, CLICK_WINDOW_MS, MIN_FILLED, CLICK_PROBE_JS,
  siteOf, pathShape, classifyRequest, labelOf, combine, createDetector,
  configure, signal, onSignal, shared,
};
