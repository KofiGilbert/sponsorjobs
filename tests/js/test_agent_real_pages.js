// The apply agent on the owner's REAL job pages of 2026-11-02 (tests/fixtures/forms/real/, the
// page views agent_view.js built in his app; no personal data): a closed job is caught before any
// model call (Oracle, US Bank's Workday, Amazon) and never on an open one; LinkedIn is never
// clicked (Eliassen's "Apply with LinkedIn"); and a run on a closed page stops as job-closed with
// zero brain calls and the record taken off the list, while a batch counts it apart and moves on.
"use strict";
const fs = require("fs");
const path = require("path");
const { createAgent, closedJob, socialRefused } = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const REAL = path.join(__dirname, "..", "fixtures", "forms", "real");
const page = (name) => fs.readFileSync(path.join(REAL, name + ".txt"), "utf8");

(async () => {
  // ---- 1. closed or open, from the page text alone ----------------------------------------
  const closed = {
    oracle_closed: /This job is no longer available/,
    usbank_workday_closed: /The page you are looking for doesn't exist/,
    amazon_closed: /Sorry, the job you're looking for isn't available/,
  };
  for (const [name, re] of Object.entries(closed)) {
    const got = closedJob(page(name));
    assert(re.test(got), name + " is closed: " + JSON.stringify(got));
  }
  for (const name of ["barclays_workday_job", "gartner_workday_job", "lam_phenom_job", "upgrade_greenhouse_job",
                      "eliassen_apply_options", "chewy_chatbot_job", "schneider_jibe_job"]) {
    assert(closedJob(page(name)) === "", name + " is open: " + closedJob(page(name)));
  }
  // a job description that talks about closing never reads as closed while Apply is on the page
  assert(closedJob("Analyst\n[1]<button>Apply</button>\nThis position has been filled by many great people before you.") === "", "Apply on the page");
  // a closed line in a form is not a closed job either
  assert(closedJob("[2]<input type=text label=\"Name\" value=\"\">\nThis job is no longer available.") === "", "a form");

  // ---- 2. LinkedIn is never clicked; other social sign-ins only without an email path -------
  const eli = page("eliassen_apply_options");
  assert(/Apply with LinkedIn/.test(eli) && /Apply with Email/.test(eli), "the real options are there");
  assert(/LinkedIn is never used/.test(socialRefused("Apply with LinkedIn", eli)), "LinkedIn refused");
  assert(socialRefused("Apply with Email", eli) === "", "email allowed");
  assert(/email or manual path/.test(socialRefused("Continue with Google", eli)), "Google refused while email exists");
  assert(socialRefused("Continue with Google", "[1]<button>Continue with Google</button>") === "", "Google allowed when it is the only path");
  assert(/LinkedIn/.test(socialRefused("Sign in with LinkedIn", "[1]<button>Sign in with LinkedIn</button>")), "LinkedIn refused even alone");

  // ---- 3. a run on Oracle's closed page: stopped, no brain call, taken off the list ---------
  const oracle = page("oracle_closed");
  const url = /^URL: (.*)$/m.exec(oracle)[1];
  const mk = (views, hooks) => {
    hooks = hooks || {};
    let at = 0;
    const wc = {
      getURL: () => hooks.url || url, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
      executeJavaScript: async (expr) => {
        if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
        if (expr.includes("__tailorQuiet.at")) return "5000|complete";
        if (expr.includes("pageSignature")) return { labels: [] };
        if (expr.includes("termsItems")) return [];
        if (expr.includes("crossOriginFrames")) return 0;
        if (expr.includes("hasResumeUpload")) return false;
        if (expr.includes("pageShape")) return { fields: 0, buttons: 19, skeleton: false, words: 102 };
        if (expr.includes("TailorAgent.view(")) return { text: views[Math.min(at++, views.length - 1)] };
        return true;
      },
    };
    const posts = [], progress = [];
    const pane = { webContents: wc };
    const agent = createAgent({
      noPolling: true, getPane: () => pane, openInPane: hooks.openInPane || (() => {}), waitPaneLoad: async () => {},
      autofillCore: () => "", agentViewSource: () => "", runAutoApplyOnPane: async () => ({ ok: false, reason: "no-fields" }),
      sendProgress: (i) => progress.push(JSON.parse(JSON.stringify(i))), acquireDebugger: () => null, frames: {},
      applyDecision: async () => ({ fill: true }), isNewPage: () => false, attachResume: async () => false,
      getApp: async () => ({ hosts: [], parked: [] }),
      postApp: async (p, b) => { posts.push([p, b]); const h = hooks.post && hooks.post(p, b); return h !== undefined ? h : { ok: true }; },
    });
    return { agent, posts, progress };
  };
  {
    const { agent, posts, progress } = mk([oracle]);
    const r = await agent.run(9, { url, company: "Oracle" });
    assert(r.reason === "job-closed" && r.closed, "stopped as job-closed " + JSON.stringify(r));
    assert(!posts.some(([p]) => p === "/api/autoapply/agent/step"), "no brain call: zero AI cost");
    assert(posts.some(([p, b]) => p === "/api/autoapply/agent/closed" && b.record_id === 9 && /no longer available/.test(b.phrase)), "the record is taken off the list");
    assert(!posts.some(([p]) => p === "/api/autoapply/agent/park"), "never parked as needs you");
    const st = progress.filter((x) => x.record_id === 9).pop();
    assert(st.state === "stopped" && st.reason === "job-closed" && /This job is closed/.test(st.message) && /Nothing was sent/.test(st.message), st.message);
  }
  {
    // a batch: the closed job is counted apart and the next one goes on
    const { agent, progress } = mk([oracle]);
    const res = await agent.batch([{ recordId: 9, url }, { recordId: 8, url }]);
    assert(res.ok && res.results.length === 2 && res.results.every((x) => x.closed), "both ran " + JSON.stringify(res.results));
    const done = progress.find((x) => x.state === "batch-done");
    assert(/2 closed by the employer/.test(done.message) && done.batch.closed === 2 && done.batch.stopped === 0, done.message);
  }
  {
    // ---- 4. the engine's free check first (sourcing/liveness.py, 2026-11-02) ------------------
    // US Bank's taken-down posting is caught by the plain HTTP check before the page opens, and
    // Gartner's (open in the pane) by the engine's check on the first step: neither costs a brain
    // call, the batch moves on, and its sum-up counts both as closed.
    const usbank = /^URL: (.*)$/m.exec(page("usbank_workday_closed"))[1];
    const gartnerView = page("gartner_workday_job");
    const gartner = /^URL: (.*)$/m.exec(gartnerView)[1];
    const opened = [];
    const { agent, posts, progress } = mk([gartnerView], {
      url: gartner,
      openInPane: (u) => opened.push(u),
      post: (p, b) => {
        if (p === "/api/autoapply/agent/precheck") return b.record_id === 7
          ? { ok: true, closed: true, status: "closed", phrase: "Workday: the posting was taken down" }
          : { ok: true, closed: false, status: "unknown" };
        if (p === "/api/autoapply/agent/step") return { ok: false, reason: "job-closed", closed: true, phrase: "Workday: the posting was taken down", cost: 0 };
        return undefined;
      },
    });
    const res = await agent.batch([{ recordId: 7, url: usbank }, { recordId: 6, url: gartner }]);
    assert(res.ok && res.results.length === 2 && res.results.every((x) => x.closed && x.reason === "job-closed"), "both closed " + JSON.stringify(res.results));
    assert(!opened.includes(usbank) && opened.includes(gartner), "the closed posting never opened " + JSON.stringify(opened));
    const steps = posts.filter(([p]) => p === "/api/autoapply/agent/step");
    assert(steps.length === 1 && steps[0][1].record_id === 6 && steps[0][1].history.length === 0, "one step, the engine said closed before any brain call");
    assert(!posts.some(([p]) => p === "/api/autoapply/agent/closed"), "the engine already marked both");
    const st7 = progress.filter((x) => x.record_id === 7).pop();
    assert(st7.reason === "job-closed" && /This job is closed\. Nothing was spent\./.test(st7.message), st7.message);
    const done = progress.find((x) => x.state === "batch-done");
    assert(/2 closed by the employer/.test(done.message) && done.batch.closed === 2 && done.batch.stopped === 0, done.message);
  }
  console.log("ok");
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
