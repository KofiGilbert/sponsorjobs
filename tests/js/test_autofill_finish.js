// "Autofill, then the agent finishes" (2026-11-26), without Electron
// (tests/test_autofill_finish.py runs it):
//   1. the trigger rule: the agent steps in only for required fields still empty or invalid,
//      further pages, or a job page instead of the form;
//   2. the AI plan is skipped on a page whose fields it was already asked about;
//   3. the agent carries on from the page Autofill filled: the job is never opened again, the
//      ladder never reopens it, and nothing is set aside to carry on by itself later;
//   4. one cost figure for the whole application (Autofill's plan, the steps, a letter), and the
//      $0.30 cap stops the run with what is left said in plain words;
//   5. Ready to send without Send: the person clicks the site's Submit; nothing here presses it.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "sj-finish-"));
process.env.TMPDIR = TMP;
process.env.SJ_AGENT_PANE_MS = "300";
process.env.SJ_AGENT_FILL_MS = "300";
process.env.SJ_AGENT_HANG_S = "1";
process.env.SJ_AGENT_WAITFORM_S = "1";
process.env.SJ_AGENT_RETRY_MS = "20";
const F = require(path.join(__dirname, "..", "..", "shell-electron", "finish.js"));
const { createAgent } = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };

const URL1 = "https://job-boards.greenhouse.io/acme/jobs/123";
function fakePane(page) {
  const P = { page, acts: [], views: 0, pointer: [] };
  const wc = {
    getURL: () => URL1, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
    reload: () => { P.reloads = (P.reloads || 0) + 1; }, navigationHistory: { canGoBack: () => false },
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet")) return "5000|complete|";
      if (expr.includes("pageSignature")) return { labels: ["Name"] };
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("pageShape")) return { fields: 3, buttons: 3, skeleton: false, words: 80 };
      if (expr.includes("TailorAutofill.leftover()")) return [];
      if (expr === "!!window.TailorPointer") return true;
      if (expr.includes("TailorPointer.at(")) { P.pointer.push(/TailorPointer\.at\(el, ("[^"]*")\)/.exec(expr)[1]); return true; }
      if (expr.includes("TailorPointer.hide()")) { P.hidden = (P.hidden || 0) + 1; return true; }
      if (expr.includes("TailorAgent.view(")) {
        P.views++;
        return { text: "URL: " + URL1 + "\n" + (typeof P.page === "function" ? P.page(P) : P.page) };
      }
      if (expr.includes("resumeInputOpen")) return false;
      if (expr.includes("TailorAgent.act(")) { P.acts.push(expr); return { ok: true }; }
      if (expr.includes("highlightSubmit")) { P.outlined = true; return true; }
      return true;
    },
  };
  return { pane: { webContents: wc }, P };
}
function setup(page, brain, extra) {
  const { pane, P } = fakePane(page);
  const posts = [], progress = [], opened = [];
  const deps = Object.assign({
    noPolling: true,
    getPane: () => pane, openInPane: (u) => opened.push(u), waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "", pointerSource: () => "/* pointer */",
    runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: [], cost: 0 }),
    sendProgress: (info) => progress.push(JSON.parse(JSON.stringify(info))),
    acquireDebugger: () => null, frames: {},
    applyDecision: async () => ({ fill: true, click_submit: true }),     // even a site the list allows
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
  return { agent: createAgent(deps), posts, progress, opened, P };
}
const steps = (posts) => posts.filter(([p]) => p === "/api/autoapply/agent/step").map(([, b]) => b);
const ok = (actions, extra) => Object.assign({ ok: true, summary: "Working on it", actions, done: null, cost: 0.01 }, extra || {});
const READY = ok([], { done: { outcome: "ready_to_submit", reason: "only Submit is left" } });
const FINISH = { url: URL1, company: "Acme", lane: "assisted", autonomous: true, finish: true, finishWhy: "Some required fields are still empty",
                 resume: { entry: null, state: null, samePage: true } };

(async () => {
  // ---- 1. the trigger rule ------------------------------------------------------------------
  const go = (r, x) => F.needsFinish(r, x);
  assert(go({ ok: true, filled: 12, left: [] }).go === false, "Autofill alone finished the page: no agent");
  assert(go({ ok: true, filled: 12, left: [], submit_highlighted: true }).go === false, "a one-page form, all filled: no agent");
  assert(go({ ok: true, left: ["Why do you want to work here?"] }).why === "empty", "a required field left");
  assert(go({ ok: true, left: [] }, { invalid: 1 }).why === "invalid", "the page marks a field invalid");
  assert(go({ ok: true, left: [], next_page: true }).why === "pages", "the form goes on");
  assert(go({ ok: false, reason: "not-a-form", apply: "Apply" }).why === "job-page", "the job page, not the form");
  assert(go({ ok: false, reason: "no-fields", page: 1 }).why === "job-page", "an overview page with no form yet");
  for (const reason of ["blocked", "linkedin", "fill-by-hand", "bad-url", "no-page", "stalled"]) {
    assert(go({ ok: false, reason, left: ["x"] }).go === false, "never handed over: " + reason);
  }
  assert(go({ ok: true, left: ["x"], captcha: true }).go === false, "a captcha is the person's");
  assert(go({ ok: true, left: ["x"], submitted: true }).go === false, "already sent");
  assert(go(null).go === false, "no report, no agent");
  assert(go({ ok: true, left: [""] }).go === false, "an empty label is not a question");

  // ---- 2. the plan is skipped on a page it already planned ----------------------------------
  {
    const a = [{ label: "First name", type: "text" }, { label: "Country", type: "select", options: ["A", "B"], required: true }];
    const k = F.planKey(7, "https://acme.wd1.myworkdayjobs.com/x/apply?a=1");
    assert(k === F.planKey(7, "https://acme.wd1.myworkdayjobs.com/x/apply?a=2"), "the query does not make a new page");
    assert(k !== F.planKey(8, "https://acme.wd1.myworkdayjobs.com/x/apply"), "nor another application the same one");
    let seen = F.notePlanned(null, a);
    assert(F.unplannedFields(seen, a).length === 0, "the same fields: nothing to plan");
    assert(F.unplannedFields(seen, a.slice(0, 1)).length === 0, "fewer of them (some filled): nothing to plan");
    const more = a.concat([{ label: "Visa details", type: "textarea", required: true }]);
    assert(F.unplannedFields(seen, more).length === 1, "a follow-up question that appeared is planned");
    const changed = [{ label: "Country", type: "select", options: ["A", "B", "C"], required: true }];
    assert(F.unplannedFields(seen, changed).length === 1, "a list with other choices is another field");
    seen = F.notePlanned(seen, more);
    assert(F.unplannedFields(seen, more).length === 0, "noted");
  }

  // ---- 3 + 5. carries on from this page; Ready to send without Send ---------------------------
  {
    const form = "Apply\n[3]<input label=\"First name\" value=\"(filled)\" required>\n[7]<input label=\"Why us?\" value=\"\" required>\n[9]<button>Submit application</button>";
    const filledForm = form.replace('label="Why us?" value=""', 'label="Why us?" value="(filled)"');
    const { agent, posts, progress, opened, P } = setup((Pp) => (Pp.acts.length ? filledForm : form), (Pp, b, all) => (steps(all).length >= 2 ? READY : ok([{ op: "click", index: 7 }])), {
      runAutoApplyOnPane: async (rid, o) => { assert(o.samePlanSkip === true, "the agent's fill skips a plan already made"); return { ok: true, filled: 0, left: [], cost: 0 }; },
    });
    const r = await agent.run(11, FINISH);
    assert(r.ok && r.ready, "ready " + JSON.stringify(r));
    assert(opened.length === 0, "the job was never opened again: " + opened.join(","));
    assert(!posts.some(([p]) => p === "/api/autoapply/agent/precheck"), "no fresh start");
    assert(P.pointer.some((x) => /Clicking: Why us/.test(x)), "the pointer named the click: " + P.pointer.join(" | "));
    assert(P.hidden >= 1, "the pointer is gone when the run ends");
    const ready = progress.filter((x) => x.state === "ready").pop();
    assert(ready && ready.finish === true, "the Ready line says it is a finish run");
    assert(/click Submit on the site/.test(ready.message) && !/press Send/.test(ready.message), "the person's own Submit: " + ready.message);
    assert(P.outlined, "the site's Submit is outlined for the person");
    assert(!P.acts.some((e) => /Submit application/.test(e)), "never clicked");
    const notice = posts.find(([p, b]) => p === "/api/autoapply/agent/ready" && b && b.sig !== undefined);
    assert(notice && !("notice" in notice[1]), "no Ready to send notice to Telegram for a finish run");
    // Send refuses: autonomous was asked and the site allowed a click, but a finish run is the person's
    const s = await agent.send(11);
    assert(s.ok === false && s.reason === "your-click", "nothing presses Submit: " + JSON.stringify(s));
    assert(progress.filter((x) => x.record_id === 11).every((x) => x.state !== "submitted"), "never submitted");
  }

  // ---- 3b. the ladder never reopens the job; a stop leaves the page as it is -----------------
  {
    const stuck = "Apply\n[7]<input label=\"Why us?\" value=\"\" required>\n[8]<button>Next</button>";
    const { agent, posts, progress, opened } = setup(stuck, () => ok([{ op: "reopen_job" }]));
    const p = agent.run(12, FINISH);
    setTimeout(() => agent.stop(), 2500);
    await p;
    assert(opened.length === 0, "reopen_job did nothing: " + opened.join(","));
    assert(!posts.some(([pth]) => pth === "/api/autoapply/agent/park"), "nothing parked to carry on by itself");
    void progress;
  }
  {
    // a step only the person can do: the page stays, the bar says so, no park on the engine
    const { agent, posts, progress } = setup("Apply\n[7]<input label=\"Code\" value=\"\" required>",
      () => ok([], { done: { outcome: "blocked", reason: "phone-code" } }));
    const r = await agent.run(13, FINISH);
    assert(r.reason === "needs-you", "handed back: " + JSON.stringify(r));
    assert(!posts.some(([p]) => p === "/api/autoapply/agent/park"), "no park");
    const last = progress.filter((x) => x.record_id === 13).pop();
    assert(/Press Autofill again/.test(last.message), "says how to carry on: " + last.message);
  }

  // ---- 4. one cost figure, and the cap ------------------------------------------------------
  {
    const form = "Apply\n[7]<input label=\"Why us?\" value=\"\" required>\n[8]<input label=\"Salary\" value=\"\" required>\n[9]<button>Next</button>";
    let fills = 0;
    const { agent, progress } = setup(form, () => ok([{ op: "click", index: 9 }], { cost: 0.06 }), {
      runAutoApplyOnPane: async () => { fills++; return { ok: true, filled: 0, left: ["Why us?", "Salary"], cost: 0.004 }; },
    });
    const r = await agent.run(14, Object.assign({}, FINISH, { startCost: 0.004 }));
    assert(r.reason === "cost-cap", "stopped at the cap " + JSON.stringify(r));
    const last = progress.filter((x) => x.record_id === 14).pop();
    assert(/under \$0\.30/.test(last.message) && /Everything I filled stays/.test(last.message), "plain words: " + last.message);
    assert(/Left for you: Why us\?, Salary/.test(last.message), "what is left: " + last.message);
    const cost = last.activity && last.activity.cost;
    // the start (Autofill's plan) + each step's plan + each brain step, all in one figure
    assert(cost >= 0.30 && cost < 0.40, "the cost figure counts everything: " + cost);
    assert(fills >= 4, "it ran a few steps first: " + fills);
  }
  console.log("ok");
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
