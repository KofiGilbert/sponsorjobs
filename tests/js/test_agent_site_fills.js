// The 2026-11-26 fixes for what the SITE fills, in the agent loop, without Electron
// (tests/test_autofill_site_fills.py runs it):
//   1. Workday's "Apply Manually" next to "Autofill with Resume": the shell takes Apply Manually
//      itself, before any model call;
//   2. a phone the page keeps rejecting: the shell retypes it once per page (deps.fixPhone) and
//      tells the brain not to type it again; never a six-step retype loop;
//   3. the check against the profile before Ready to send: what it put right sends the run round
//      once more; what it could not goes back to the brain once, then is named at Ready to send.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "sj-sitefill-"));
process.env.TMPDIR = TMP;
process.env.SJ_AGENT_PANE_MS = "300";
process.env.SJ_AGENT_FILL_MS = "300";
process.env.SJ_AGENT_HANG_S = "1";
process.env.SJ_AGENT_WAITFORM_S = "1";
process.env.SJ_AGENT_RETRY_MS = "20";
const A = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const { createAgent } = A;
const assert = (c, m) => { if (!c) throw new Error(m); };

const URL1 = "https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/Analyst_R1/apply";
function fakePane(page) {
  const P = { page, acts: [], views: 0 };
  const wc = {
    getURL: () => URL1, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
    reload: () => {}, navigationHistory: { canGoBack: () => false },
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet")) return "5000|complete|";
      if (expr.includes("pageSignature")) return { labels: ["Name"] };
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("pageShape")) return { fields: 3, buttons: 3, skeleton: false, words: 80 };
      if (expr.includes("TailorAutofill.leftover()")) return [];
      if (expr.includes("TailorAgent.view(")) {
        P.views++;
        return { text: "URL: " + URL1 + "\n" + (typeof P.page === "function" ? P.page(P) : P.page) };
      }
      if (expr.includes("resumeInputOpen")) return false;
      if (expr.includes("TailorAgent.act(")) { P.acts.push(expr); return { ok: true }; }
      if (expr.includes("submitButton")) return null;
      return true;
    },
  };
  return { pane: { webContents: wc }, P };
}
function setup(page, brain, extra) {
  const { pane, P } = fakePane(page);
  const posts = [], progress = [];
  const deps = Object.assign({
    noPolling: true,
    getPane: () => pane, openInPane: () => {}, waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: [] }),
    sendProgress: (info) => progress.push(JSON.parse(JSON.stringify(info))),
    acquireDebugger: () => null, frames: {},
    applyDecision: async () => ({ fill: true, click_submit: false }),
    isNewPage: () => false, attachResume: async () => false,
    getApp: async (p) => (p.startsWith("/api/autoapply/agent/parked") ? { ok: true, parked: [] } : { hosts: [] }),
    postApp: async (p, b) => {
      posts.push([p, b]);
      if (p === "/api/autoapply/agent/step") return brain(P, b, posts);
      if (p === "/api/autoapply/agent/ask" && b && b.known_only) return { ok: true, answers: {} };
      if (p === "/api/autoapply/agent/critic") return { ok: true, issues: [] };
      return { ok: true };
    },
  }, extra || {});
  return { agent: createAgent(deps), posts, progress, P };
}
const steps = (posts) => posts.filter(([p]) => p === "/api/autoapply/agent/step").map(([, b]) => b);
const ok = (actions, extra) => Object.assign({ ok: true, summary: "Working on it", actions, done: null, cost: 0.01 }, extra || {});
const READY = ok([], { done: { outcome: "ready_to_submit", reason: "only Submit is left" } });
const REVIEW = "Review\n[3]<input label=\"Email\" value=\"(filled)\" required>\n[9]<button>Submit</button>";

(async () => {
  // ---- pure helpers -----------------------------------------------------------------------
  assert(A.manualOverParse("[1]<button>Autofill with Resume</button>\n[2]<button>Apply Manually</button>") === 2, "Apply Manually over the parse");
  assert(A.manualOverParse("[2]<button>Apply Manually</button>") === 0, "only one way in: the brain's choice");
  assert(A.manualOverParse("[1]<button>Use My Last Application</button>\n[4]<a>Apply Manually</a>") === 4, "a link counts too");
  assert(A.phoneError("Errors Found\n*[8]<button>Error-Phone Number</button>"), "Workday's error button");
  assert(A.phoneError("Error: \"Enter a valid format for Phone Number.\""), "the error line");
  assert(!A.phoneError("[5]<combobox label=\"Phone Device Type\" value=\"\" required>"), "a phone device type is no error");
  assert(!A.phoneError("Error: \"Enter a valid format for Phone Device Type.\""), "nor its error");

  // ---- 1. Apply Manually by the shell, no model call ----------------------------------------
  {
    const start = "Operations Analyst\n[2]<button>Autofill with Resume</button>\n[3]<button>Apply Manually</button>\n[4]<button>Use My Last Application</button>";
    const form = "My Information\n[5]<input label=\"First Name\" value=\"(filled)\" required>\n[9]<button>Submit</button>";
    const { agent, posts, P } = setup((Pp) => (Pp.acts.length ? form : start), () => READY);
    await agent.run(1, { url: URL1, company: "Acme" });
    assert(P.acts.length >= 1 && /"op":"click","index":3/.test(P.acts[0]), "the shell clicked Apply Manually first: " + P.acts[0]);
    const first = steps(posts)[0];
    assert(first && !/Autofill with Resume/.test(first.view), "the brain is first asked on the form, not the choice");
  }

  // ---- 2. the phone retyped once per page, never a loop -------------------------------------
  {
    let fixes = 0;
    const bad = "My Information\nErrors Found\n*[8]<button>Error-Phone Number</button>\n[27]<input label=\"Phone Number\" value=\"(filled)\" required>\n[30]<button>Next</button>";
    const { agent, posts } = setup(bad, (P, b, all) => (steps(all).length >= 3 ? READY : ok([{ op: "click", index: 30 }])), {
      fixPhone: async () => { fixes++; return { typed: 1, of: 1 }; },
    });
    await agent.run(2, { url: URL1, company: "Acme" });
    assert(fixes === 1, "one retype on this page, not one a step: " + fixes);
    const s1 = steps(posts)[0];
    assert(JSON.stringify(s1.nudges).includes("Do not type the phone again"), "the brain is told: " + JSON.stringify(s1.nudges));
  }

  // ---- 3a. the profile check put something right: one more round, then Ready to send ---------
  {
    let checks = 0;
    const { agent, progress } = setup(REVIEW, () => READY, {
      checkPrefill: async () => { checks++; return checks === 1 ? { ok: true, fixed: { contact: 1, history: 0, labels: ["Email"] }, issues: [] }
                                                                : { ok: true, fixed: { contact: 0, history: 0, labels: [] }, issues: [] }; },
    });
    const r = await agent.run(3, { url: URL1, company: "Acme" });
    assert(r.ready, "handed over " + JSON.stringify(r));
    assert(checks >= 1, "the check ran");
    const events = progress.filter((x) => x.activity).pop().activity.events.map((e) => e.verb + " " + e.summary);
    assert(events.some((e) => /^Corrected 1 box the site filled/.test(e)), "the feed says what it corrected: " + events.join(" | "));
  }

  // ---- 3b. what it could not put right: back to the brain once, then named at Ready to send --
  {
    const issue = "Work Experience 2 is not a job in your profile";
    const { agent, posts, progress } = setup(REVIEW, () => READY, {
      checkPrefill: async () => ({ ok: true, fixed: { contact: 0, history: 0, labels: [] }, issues: [issue] }),
    });
    const r = await agent.run(4, { url: URL1, company: "Acme" });
    assert(r.ready, "still handed over, once the brain had its round " + JSON.stringify(r));
    const nudged = steps(posts).some((b) => JSON.stringify(b.nudges || []).includes(issue));
    assert(nudged, "the brain was told once");
    const last = progress.filter((x) => x.state === "ready").pop();
    assert(last && (last.noted || []).includes(issue), "named at Ready to send: " + JSON.stringify(last && last.noted));
  }
  console.log("ok");
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
