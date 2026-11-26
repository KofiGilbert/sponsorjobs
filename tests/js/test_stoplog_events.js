// The stop log (autoapply/stoplog.py), the agent's side, without Electron
// (tests/test_stoplog.py runs it): the cost cap and a captcha each send ONE event to /api/autoapply/stoplog, with the public question and
// choices and never a value; the person's own Stop sends none.
// (scaffold from tests/js/test_autofill_finish.js)
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


const events = (posts) => posts.filter(([p]) => p === "/api/autoapply/stoplog").map(([, b]) => b.events[0]);

(async () => {
  // ---- the cost cap -----------------------------------------------------------------------
  {
    const form = "Apply\n[7]<input label=\"Why us?\" value=\"\" required>\n[9]<button>Next</button>";
    const { agent, posts } = setup(form, () => ok([{ op: "click", index: 9 }], { cost: 0.08, evaluation: "Next does not move on" }), {
      runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: ["Why us?"], cost: 0 }),
      version: "0.9.1",
    });
    const r = await agent.run(21, Object.assign({}, FINISH, { startCost: 0 }));
    assert(r.reason === "cost-cap", JSON.stringify(r));
    const e = events(posts);
    assert(e.length === 1 && e[0].type === "cost_cap", JSON.stringify(e));
    assert(e[0].question === "Why us?" && e[0].reason === "Next does not move on" && e[0].version === "0.9.1", JSON.stringify(e[0]));
    assert(e[0].steps >= 1 && e[0].cost >= 0.3 && /greenhouse/.test(e[0].url), JSON.stringify(e[0]));
  }
  // ---- a captcha and a closed posting -------------------------------------------------------
  {
    const { agent, posts } = setup("Apply\n[7]<input label=\"Name\" value=\"(filled)\" required>",
      () => ok([], { done: { outcome: "blocked", reason: "captcha" } }));
    await agent.run(22, FINISH);
    const e = events(posts);
    assert(e.some((x) => x.type === "captcha"), JSON.stringify(e));
  }
  // ---- the person's own Stop sends nothing ---------------------------------------------------
  {
    const { agent, posts } = setup("Apply\n[7]<input label=\"Why us?\" value=\"\" required>", () => ok([{ op: "wait", seconds: 1 }]));
    const p = agent.run(23, FINISH);
    setTimeout(() => agent.stop(), 600);
    await p;
    assert(!events(posts).length, "Stop is no event: " + JSON.stringify(events(posts)));
  }
  console.log("ok");
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
