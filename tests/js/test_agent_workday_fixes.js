// The 2026-11-16 Workday and Lever fixes in the agent loop, without Electron
// (tests/test_agent_workday_lever.py runs it):
//   1. the brain saying "needs-answers" while nothing required is open: a firm nudge, then a park
//      with its reason; never ten repeats and a reload that starts the application over;
//   2. a fill that runs past its cap says WHERE it stood (the fill's phase), not just "timed out";
//   3. the page is read again before asking: a box the site filled by itself after the upload
//      (Lever's "Current company") is never asked;
//   4. a cross-origin frame gets the agent's page view, not only the fill engine.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "sj-wdfix-"));
process.env.TMPDIR = TMP;
process.env.SJ_AGENT_DEBUG = "1";
process.env.SJ_AGENT_PANE_MS = "300";
process.env.SJ_AGENT_FILL_MS = "300";
process.env.SJ_AGENT_HANG_S = "1";
process.env.SJ_AGENT_WAITFORM_S = "1";
process.env.SJ_AGENT_RETRY_MS = "20";
process.env.SJ_AGENT_ASK_REREAD_MS = "4000";
const { createAgent } = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const frames = require(path.join(__dirname, "..", "..", "shell-electron", "frames.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const never = () => new Promise(() => {});

const URL1 = "https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/Analyst_R1/apply/applyManually";
function fakePane(page) {
  const P = { page, reloads: 0, acts: [], leftover: null };
  const wc = {
    getURL: () => URL1, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
    reload: () => { P.reloads++; },
    navigationHistory: { canGoBack: () => false },
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet")) return "5000|complete|";
      if (expr.includes("pageSignature")) return { labels: ["Name", "Date"] };
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("pageShape")) return { fields: 3, buttons: 3, skeleton: false, words: 80 };
      if (expr.includes("TailorAutofill.leftover()")) return P.leftover;
      if (expr.includes("TailorAgent.view(")) {
        P.views = (P.views || 0) + 1;
        return { text: "URL: " + URL1 + "\n" + (typeof P.page === "function" ? P.page(P) : P.page) };
      }
      if (expr.includes("resumeInputOpen")) return false;
      if (expr.includes("TailorAgent.act(")) { P.acts.push(expr); return { ok: true }; }
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
    getPane: () => pane,
    openInPane: () => {},
    waitPaneLoad: async () => {},
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
      if (p === "/api/autoapply/agent/ask") return { ok: false, reason: "stopped" };
      return { ok: true };
    },
  }, extra || {});
  return { agent: createAgent(deps), posts, progress, P };
}
const steps = (posts) => posts.filter(([p]) => p === "/api/autoapply/agent/step").map(([, b]) => b);
const ok = (actions, extra) => Object.assign({ ok: true, summary: "Working on it", actions, done: null, cost: 0.01 }, extra || {});
const feedLines = (progress, rid) => {
  const hit = progress.filter((x) => x.record_id === rid && x.activity).pop();
  return ((hit && hit.activity.events) || []).map((e) => e.state + ": " + e.verb + (e.summary ? " " + e.summary : "") + (e.detail ? " (" + e.detail + ")" : ""));
};

(async () => {
  // ---- 1. "needs-answers" with nothing required open: nudge, then park; never a reload --------
  {
    const page = "Voluntary Self-Identification of Disability\n[7]<input label=\"Name\" value=\"(filled)\" required>\n"
      + "[9]<checkbox label=\"Please check one of the boxes below:: I do not want to answer\" checked=hidden>\n[12]<button>Next</button>";
    const brain = () => ok([], { done: { outcome: "blocked", reason: "needs-answers" }, evaluation: "The disability box looks open." });
    const { agent, posts, P } = setup(page, brain);
    const r = await agent.run(1, { url: URL1, company: "Acme" });
    assert(r.parked && r.kind === "person", "parks with its reason " + JSON.stringify(r));
    const n = steps(posts).length;
    assert(n === 3, "three brain steps, not ten and a reload: " + n);
    assert(P.reloads === 0, "no reload (a Workday reload starts the application over): " + P.reloads);
    const second = steps(posts)[2];
    assert(JSON.stringify(second).includes("Do NOT report needs-answers again"), "the second time carries a firm nudge");
    assert(!posts.some(([p, b]) => p === "/api/autoapply/agent/ask" && !(b && b.known_only)), "nothing is asked");
  }

  // ---- 2. a fill past its cap names its phase -------------------------------------------------
  {
    let hung = 0;
    const page = "My Experience\n[3]<input label=\"Job Title\" value=\"\" required>\n[8]<button>Next</button>";
    const brain = () => ok([{ op: "wait", seconds: 1 }]);
    const { agent, progress } = setup(page, brain, {
      runAutoApplyOnPane: async () => { hung++; return never(); },
      fillPhase: () => "the AI planned 112 fields",
    });
    const p = agent.run(2, { url: URL1, company: "Acme" });
    setTimeout(() => agent.stop(), 2500);
    await p;
    assert(hung >= 1, "the fill ran");
    const log = fs.readFileSync(path.join(TMP, "sj-agent.log"), "utf8");
    assert(/hang: fill timed out while the AI planned 112 fields/.test(log), "the hang names the phase: " + log.split("\n").filter((l) => /hang/.test(l)).join(" | "));
    const f = feedLines(progress, 2);
    assert(f.some((l) => /the page did not answer/.test(l)), "the feed says so " + f.join(" | "));
  }

  // ---- 3. read the page again before asking ---------------------------------------------------
  {
    // The fill left "Current company" open; the site filled it from the resume a moment later.
    const before = "Submit your application\n[4]<input label=\"Current company\" value=\"\" required>\n[9]<button>Submit application</button>";
    const after = "Submit your application\n[4]<input label=\"Current company\" value=\"(filled)\" required>\n[9]<button>Submit application</button>";
    const brain = (P) => (P.views > 1 ? ok([], { done: { outcome: "blocked", reason: "needs-answers" } }) : ok([], { done: { outcome: "blocked", reason: "needs-answers" } }));
    const { agent, posts, P } = setup((Pp) => (Pp.views <= 1 ? before : after), brain, {
      runAutoApplyOnPane: async () => ({ ok: true, filled: 7, left: ["Current company"] }),
    });
    P.leftover = [];
    await agent.run(3, { url: URL1, company: "Acme" });
    const asked = posts.filter(([p, b]) => p === "/api/autoapply/agent/ask" && !(b && b.known_only));
    assert(!asked.some(([, b]) => JSON.stringify(b).includes("Current company")), "no false ask for a box the site filled: " + JSON.stringify(asked));
  }

  // ---- 3b. right after an upload the site is still reading the resume: look again, then ask ---
  {
    const before = "Submit your application\n[4]<input label=\"Current company\" value=\"\" required>\n[9]<button>Submit application</button>";
    const after = "Submit your application\n[4]<input label=\"Current company\" value=\"(filled)\" required>\n[9]<button>Submit application</button>";
    const brain = () => ok([], { done: { outcome: "blocked", reason: "needs-answers" } });
    const { agent, posts, P } = setup((Pp) => (Pp.views <= 3 ? before : after), brain, {
      runAutoApplyOnPane: async () => ({ ok: true, filled: 7, uploaded: 1, left: ["Current company"] }),
    });
    P.leftover = ["Current company"];
    const t0 = Date.now();
    await agent.run(4, { url: URL1, company: "Acme" });
    const asked = posts.filter(([p, b]) => p === "/api/autoapply/agent/ask" && !(b && b.known_only));
    assert(!asked.some(([, b]) => JSON.stringify(b).includes("Current company")), "waited for the site's own fill: " + JSON.stringify(asked));
    assert(P.views >= 4 && Date.now() - t0 < 30000, "a few more reads, not a long wait " + P.views);
  }

  // ---- 4. a cross-origin frame gets the agent's page view -------------------------------------
  {
    const sent = [];
    const world = { TailorAutofill: true, TailorAgent: false };
    const cdp = { send: async (method, params) => {
      sent.push(params.expression);
      const e = params.expression;
      if (e.startsWith("typeof window.TailorAutofill")) return { result: { value: world.TailorAutofill } };
      if (e.startsWith("typeof window.TailorAgent")) return { result: { value: world.TailorAgent } };
      if (e.includes("VIEW-SOURCE")) { world.TailorAgent = true; return { result: { value: 0 } }; }
      return { result: { value: undefined } };
    } };
    const ctx = { contextId: 1, sessionId: "s", prefix: "f1-" };
    await frames.injectEngine(cdp, ctx, "/* ENGINE */");
    assert(!sent.some((e) => e.includes("ENGINE")), "the engine already there is not loaded twice");
    const got = await frames.injectAgentView(cdp, ctx, "/* VIEW-SOURCE */");
    assert(got === true && sent.some((e) => e.includes("VIEW-SOURCE")), "the view is loaded although the engine was there: " + JSON.stringify(sent));
    const agentSrc = fs.readFileSync(path.join(__dirname, "..", "..", "shell-electron", "agent.js"), "utf8");
    assert(!/injectEngine\(cdp, ctx, deps\.agentViewSource\(\)\)/.test(agentSrc), "agent.js no longer loads the view through injectEngine");
    assert(/injectAgentView\(cdp, ctx, deps\.agentViewSource\(\)\)/.test(agentSrc), "agent.js loads the view through injectAgentView");
  }
  console.log("ok");
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
