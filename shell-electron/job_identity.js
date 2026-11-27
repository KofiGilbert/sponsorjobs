// Which job is this page? (2026-11-26, audit case 35)
//
// The Browser keeps one application as its context after an Autofill: its record, the page the
// fill ran on. A page opened afterwards from a link (a card's link, another posting on the same
// careers site) kept that context, so Autofill there filled the new job for the OLD record and a
// send there counted on the old card. The rule now: every page the pane reaches is compared with
// the pages the application was filled on, and a different job lets the context go.
//
// A page's job is read from its address only, never its contents:
//   ids    the job ids in it: the query parameters that name a posting (gh_jid, jobId, req,
//          token...) and the path parts that look like ids (a number of 4+ digits, a UUID, a
//          Workday "Title_R12345", a SmartRecruiters "7439...-title"), each reduced to its digits
//          or its UUID so "gh_jid=4567" and "/jobs/4567" are the same job;
//   path   the path up to the first step of an application ("/apply", "/login", "/thanks"...),
//          so the pages of one application share it.
//
// Same job: the pages share an id; or the same site with a path inside the filled one (or the
//   filled one inside it); or a sign-in host (the person signs in; the app never does); or any
//   page right after the person's own Submit click (the confirmation may be anywhere, case 15);
//   or another site reached from a JOB page Autofill found no form on (its Apply button hands
//   off to the hiring system's form: the same job).
// Another job: the same site with other ids, or another posting's path; or another site.
// Unsure (the same site, a page that is no posting: a privacy page, the careers home): kept.
//
// Pure (no Electron): tests/js/test_job_identity.js runs it under node.
"use strict";

// The query parameters that name a posting. "id" alone only with an id-shaped value.
const JOB_PARAM = /^(gh_jid|jid|job_?id|jobreq(id)?|job_?req(uisition)?_?id|req(uisition)?(_?id)?|posting_?id|position_?id|vacancy_?id|opening_?id|job_?code|token|pid|jobs?|ad_?id|offer_?id|id)$/i;
const NOT_JOB_PARAM = /^(utm_|gh_src|src|source|ref|lang|locale|page|sort|q|query|t)$/i;
// The steps of one application: the path up to the first of these is the job's own.
const STEP_SEG = /^(apply|applynow|apply-now|application|applications|applymanually|autofillwithresume|usemylastapplication|candidate|candidates|login|log-in|signin|sign-in|signup|sign-up|register|registration|account|create-account|thanks|thank-you|thankyou|confirmation|confirm|submitted|success|complete|done|review|step\d*|steps|form|questions|profile|my-information|myinformation|my-experience|voluntary-disclosures|self-identify|job_app)$/i;
// A path part that says "this is a posting" when something follows it.
const POSTING_SEG = /^(job|jobs|position|positions|posting|postings|opening|openings|vacancy|vacancies|requisition|requisitions|role|roles|opportunity|opportunities|career|careers|details|detail|offer|offers|j|o|p)$/i;
const LISTING_SEG = /^(search|results|list|all|index|category|categories|locations?|teams?|departments?)$/i;
// Hosts that only sign a person in (SSO): a step of the same application, never another job.
const SIGNIN_HOST = /^(login|signin|sign-in|sso|auth|accounts?|id|identity|secure|passport)\.|(^|\.)(okta|oktapreview|auth0|onelogin|microsoftonline|b2clogin|pingone|pingidentity|duosecurity|cloudflareaccess)\.com$|^accounts\.google\.com$|^appleid\.apple\.com$|^login\.live\.com$/i;
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

function siteOfHost(host) {
  const parts = String(host || "").toLowerCase().replace(/^www\./, "").split(".").filter(Boolean);
  if (parts.length <= 2) return parts.join(".");
  const second = parts[parts.length - 2];
  const short = parts[parts.length - 1].length === 2 && /^(co|com|ac|org|net|gov|edu)$/.test(second);
  return parts.slice(short ? -3 : -2).join(".");
}

// One value's id form: its UUID, else its longest digit run of 4+ (a year alone is not an id), else "".
function idOf(v) {
  const s = String(v || "");
  const u = UUID.exec(s);
  if (u) return u[0].toLowerCase();
  if (/^[0-9a-f]{24,}$/i.test(s)) return s.toLowerCase();
  const runs = s.match(/\d{4,}/g) || [];
  const best = runs.sort((a, b) => b.length - a.length)[0] || "";
  if (!best || (best.length === 4 && /^(19|20)\d\d$/.test(best) && runs.length === 1 && /^\d{4}$/.test(s))) return "";
  return best.replace(/^0+(?=\d{4})/, "");
}
// Path parts that are ids: a number, a UUID, a long hex id, or a part ending in one ("Title_R12345",
// "engineer-4567", "7439000000-engineer"). A slug of words alone is not.
function pathIds(segs) {
  const out = [];
  for (const seg of segs) {
    if (UUID.test(seg) || /^[0-9a-f]{24,}$/i.test(seg) || /^\d{4,}$/.test(seg)
        || /(^|[_-])[A-Za-z]{0,4}-?\d{4,}$/.test(seg) || /^\d{6,}-/.test(seg)) {
      const id = idOf(seg);
      if (id) out.push(id);
    }
  }
  return out;
}

// {ok, host, site, ids: [...], path: [segments up to the first application step], posting}
function identity(url) {
  let u = null;
  try { u = new URL(String(url || "")); } catch (e) { return { ok: false }; }
  if (!/^https?:$/.test(u.protocol)) return { ok: false };
  const host = u.hostname.toLowerCase().replace(/^www\./, "");
  const segs = u.pathname.split("/").filter(Boolean).map((s) => { try { return decodeURIComponent(s); } catch (e) { return s; } });
  const ids = new Set(pathIds(segs));
  for (const [k, v] of u.searchParams) {
    if (NOT_JOB_PARAM.test(k) || !JOB_PARAM.test(k)) continue;
    const id = idOf(v);
    if (id) ids.add(id);
  }
  // a hash route ("#/jobs/1234") counts as path ids too (single-page career sites)
  if (u.hash && u.hash.length > 2) for (const id of pathIds(u.hash.replace(/^#!?/, "").split(/[/?&=]/).filter(Boolean))) ids.add(id);
  const cut = segs.findIndex((s) => STEP_SEG.test(s));
  const path = (cut < 0 ? segs : segs.slice(0, cut)).map((s) => s.toLowerCase());
  const pi = path.findIndex((s) => POSTING_SEG.test(s));
  const posting = ids.size > 0 || (pi >= 0 && pi < path.length - 1 && !LISTING_SEG.test(path[pi + 1]));
  return { ok: true, host, site: siteOfHost(host), ids: [...ids], path, posting };
}

const prefixOf = (a, b) => a.length <= b.length && a.every((s, i) => s === b[i]);

// The same job as one filled page? {same, why}
function compare(filled, next) {
  if (!filled.ok || !next.ok) return { same: true, why: "not a web page" };
  if (next.ids.some((id) => filled.ids.includes(id))) return { same: true, why: "same job id" };
  if (next.site !== filled.site) {
    if (SIGNIN_HOST.test(next.host)) return { same: true, why: "sign-in" };
    return { same: false, why: "another site" };
  }
  if (next.ids.length && filled.ids.length) return { same: false, why: "another posting" };
  if (prefixOf(next.path, filled.path) || prefixOf(filled.path, next.path)) return { same: true, why: "same application" };
  if (next.posting) return { same: false, why: "another posting" };
  return { same: true, why: "same site" };
}

// The pages one application was filled on. add(url) after each fill; check(url, {afterSubmit})
// -> {same, why} for the page the pane reached now.
function createAnchor(first) {
  const pages = [];
  const a = {
    // ``o.form``: false for a job page with no form on it (Autofill said "not a form")
    add(url, o) {
      const id = identity(url);
      if (!id.ok) return a;
      const had = pages.find((p) => p.host === id.host && p.path.join("/") === id.path.join("/") && p.ids.join() === id.ids.join());
      if (had) { had.form = had.form || !(o && o.form === false); pages.splice(pages.indexOf(had), 1); pages.push(had); }
      else pages.push(Object.assign(id, { form: !(o && o.form === false) }));
      return a;
    },
    check(url, o) {
      const next = identity(url);
      if (!next.ok || !pages.length) return { same: true, why: "not a web page" };
      if (o && o.afterSubmit) return { same: true, why: "after your Submit" };
      let why = "";
      for (const p of pages) {
        const c = compare(p, next);
        if (c.same) return c;
        why = why || c.why;
      }
      const last = pages[pages.length - 1];
      if (why === "another site" && last.form === false) return { same: true, why: "hand-off from the job page" };
      return { same: false, why };
    },
    pages: () => pages.slice(),
    size: () => pages.length,
  };
  if (first) a.add(first);
  return a;
}

// Two addresses, the same job? (the renderer's question: is the page open still the job the
// resume was tailored for?)
function sameJob(a, b, o) { return createAnchor(a).check(b, o).same; }

module.exports = { identity, compare, createAnchor, sameJob, siteOfHost, idOf };
