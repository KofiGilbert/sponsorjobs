// The apply agent's engineering mindset without Electron (tests/test_agent_engineer.py runs it):
// every call into the pane is capped and Stop preempts it (the 2026-11-02 Nelnet hang), a page
// that never renders goes up the recovery ladder (wait for the form, reload, reopen the job)
// instead of freezing or parking, a bad model reply is retried and then escalated, an account
// problem with the AI stops the job AND the batch plainly (never "needs you"), the read-only
// tools come back as observations, the page check holds a Next with empty required fields, the
// deterministic ready gate refuses "ready" on a sign-in page, the critic gets one more round,
// the site's mail that never arrives parks with Continue and is looked for again on resume, a
// batch moves on from a job that takes too long, the plan rides along, and the debug log never
// carries a secret.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
// Short limits so the whole file runs in seconds; the debug log goes to a temp folder of its own.
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "sj-eng-"));
process.env.TMPDIR = TMP;
process.env.SJ_AGENT_DEBUG = "1";
process.env.SJ_AGENT_PANE_MS = "300";
process.env.SJ_AGENT_FILL_MS = "300";
process.env.SJ_AGENT_HANG_S = "1";
process.env.SJ_AGENT_WAITFORM_S = "1";
process.env.SJ_AGENT_RETRY_MS = "20";
process.env.SJ_AGENT_EMAIL_WAIT_S = "5";
process.env.SJ_AGENT_EMAIL_RECHECK_S = "1";
process.env.SJ_AGENT_COST_CAP_USD = "0.50";
const { createAgent, notReady, lineName, safeReply, MAX_STEPS, COST_CAP_USD } = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const never = () => new Promise(() => {});

// ---- a fake pane: pages by name, the state the test drives ----------------------------------
const PAGES = {
  job: { url: "https://jobs.example.com/job/1", sig: { labels: [] }, shape: { fields: 0, buttons: 4, skeleton: false, words: 80 },
         view: "Business Analyst\nWe need an analyst.\n[1]<button>Apply</button>\n[2]<a>Privacy</a>" },
  auth: { url: "https://jobs.example.com/login", sig: { blocker: "auth", labels: ["Email", "Password"] }, shape: { fields: 2, buttons: 3, skeleton: false, words: 30 },
          view: "Sign in\n[3]<input type=email label=\"Email\" value=\"\">\n[4]<input type=password label=\"Password\" value=\"\">\n[5]<button>Sign in</button>\n[6]<button>Create account</button>" },
  skeleton: { url: "https://jobs.example.com/apply/1", sig: { labels: [] }, shape: { fields: 0, buttons: 1, skeleton: true, words: 4 },
              view: "My Information\nLoading\n[1]<a>Skip to main content</a>" },
  form: { url: "https://jobs.example.com/apply/1", sig: { labels: ["Phone", "ZIP code"] }, shape: { fields: 2, buttons: 2, skeleton: false, words: 40 },
          view: "My Information\nStep 1 of 2\n[7]<input label=\"Phone\" value=\"(filled)\">\n[9]<input label=\"ZIP code\" value=\"\" required>\n[3]<select label=\"Country\" options=3 first=\"US|CA|GB\">\n[4]<combobox label=\"State\" value=\"\">\n[8]<button>Next</button>" },
  search: { url: "https://jobs.example.com/search?q=1", sig: { labels: [] }, shape: { fields: 1, buttons: 2, skeleton: false, words: 20 },
            view: "Search for Jobs\n[1]<input type=search label=\"Search for jobs\" value=\"\" search=true>\n[2]<button>Search</button>" },
  mailed: { url: "https://jobs.example.com/login", sig: { blocker: "auth", labels: ["Email", "Password"] }, shape: { fields: 2, buttons: 2, skeleton: false, words: 30 },
            view: "Error: \"An email has been sent to you. Please verify your account.\"\n[3]<input type=email label=\"Email\" value=\"\">\n[4]<input type=password label=\"Password\" value=\"\">\n[5]<button>Sign in</button>" },
  review: { url: "https://jobs.example.com/apply/2", sig: { labels: ["Review"] }, shape: { fields: 0, buttons: 2, skeleton: false, words: 30 },
            view: "Review\nStep 2 of 2\n[20]<button>Submit</button>" },
  // The sign-in wall's other pages (section 11 below).
  wall: { url: "https://jobs.example.com/login", sig: { blocker: "auth", labels: ["Email", "Password"] }, shape: { fields: 2, buttons: 4, skeleton: false, words: 30 },
          view: "Sign in\n[3]<input type=email label=\"Email\" value=\"\">\n[4]<input type=password label=\"Password\" value=\"\">\n[5]<button>Sign in</button>\n[6]<button>Create account</button>\n[7]<a>Forgot password?</a>" },
  wallErr: { url: "https://jobs.example.com/login", sig: { blocker: "auth", labels: ["Email", "Password"] }, shape: { fields: 2, buttons: 4, skeleton: false, words: 36 },
             view: "Sign in\nError: \"You may have entered the wrong email address or password.\"\n[3]<input type=email label=\"Email\" value=\"\">\n[4]<input type=password label=\"Password\" value=\"\">\n[5]<button>Sign in</button>\n[6]<button>Create account</button>\n[7]<a>Forgot password?</a>" },
  create: { url: "https://jobs.example.com/login#create", sig: { blocker: "auth", labels: ["Email", "Password"] }, shape: { fields: 3, buttons: 1, skeleton: false, words: 20 },
            view: "Create your account\n[3]<input type=email label=\"Email\" value=\"\">\n[4]<input type=password label=\"Create Password\" value=\"\">\n[5]<input type=password label=\"Verify Password\" value=\"\">\n[6]<button>Create Account</button>" },
  forgot: { url: "https://jobs.example.com/login#forgot", sig: { labels: ["Email"] }, shape: { fields: 1, buttons: 1, skeleton: false, words: 20 },
            view: "Reset your password\n[3]<input type=email label=\"Email Address\" value=\"\">\n[4]<button>Send reset link</button>" },
  resetSent: { url: "https://jobs.example.com/login#forgot", sig: { labels: [] }, shape: { fields: 0, buttons: 0, skeleton: false, words: 20 },
               view: "Check your email\nWe emailed you a link to reset your password. Click the link in the email to continue." },
  newpw: { url: "https://jobs.example.com/reset?token=t1", sig: { blocker: "auth", labels: ["New Password"] }, shape: { fields: 2, buttons: 1, skeleton: false, words: 10 },
           view: "Choose a new password\n[3]<input type=password label=\"New Password\" value=\"\">\n[4]<input type=password label=\"Confirm New Password\" value=\"\">\n[5]<button>Save password</button>" },
  activated: { url: "https://jobs.example.com/activate/tok", sig: { labels: [] }, shape: { fields: 0, buttons: 2, skeleton: false, words: 20 },
               view: "[1]<input type=search label=\"Search jobs\" value=\"\" search=true>\nYour account has been activated\nSign in to continue.\n[2]<button>Sign In</button>" },
  search: { url: "https://jobs.example.com/search", sig: { labels: [] }, shape: { fields: 0, buttons: 2, skeleton: false, words: 20 },
            view: "Search for Jobs\n[1]<input type=search label=\"Search for jobs\" value=\"\" search=true>\n[2]<button>Search</button>\nBrowse open roles at Acme Corp." },
};
function fakePane(start, hooks) {
  hooks = hooks || {};
  const P = { at: start, hang: new Set(), reloads: 0, backs: 0, destroyed: false, checks: 0, acts: [], navs: [], listeners: {}, hist: [] };
  const wc = {
    getURL: () => PAGES[P.at].url, isDestroyed: () => P.destroyed,
    on: (ev, fn) => { (P.listeners[ev] = P.listeners[ev] || []).push(fn); }, removeListener: () => {}, session: null,
    reload: () => { P.reloads++; if (hooks.onReload) hooks.onReload(P); },
    navigationHistory: { canGoBack: () => true, goBack: () => { P.backs++; if (hooks.onBack) hooks.onBack(P); },
                         getActiveIndex: () => P.hist.length - 1, getEntryAtIndex: (i) => ({ url: P.hist[i] || "" }) },
    executeJavaScript: async (expr) => {
      if (P.hang.has("all")) return never();
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet.at")) return "5000|complete";
      if (expr.includes("pageSignature")) return PAGES[P.at].sig;
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("hasResumeUpload")) return false;
      if (expr.includes("pageShape")) { if (P.hang.has("shape")) return never(); return PAGES[P.at].shape; }
      if (expr.includes("TailorAgent.view(")) {
        if (P.hang.has("view") && P.at === "skeleton") return never();
        if (P.hist[P.hist.length - 1] !== PAGES[P.at].url) P.hist.push(PAGES[P.at].url);
        return { text: "URL: " + PAGES[P.at].url + "\n" + PAGES[P.at].view };
      }
      if (expr.includes("readPage(")) return { text: "[5]<input label=\"Extra\" value=\"\">\nMore of the page", more: 0, next: 0 };
      if (expr.includes("TailorAgent.find(")) return { lines: ["[8]<button>Next</button>"] };
      if (expr.includes("TailorAgent.inspect(")) return { index: 3, tag: "select", label: "Country", required: true, value: "", errors: ["Country is required"] };
      if (expr.includes("readErrors()")) return ["ZIP code is required"];
      if (expr.includes("TailorAgent.has(")) return hooks.has ? hooks.has(expr) : false;
      if (expr.includes("selectOptions(")) return { options: ["US", "CA", "GB"], partial: false };
      if (expr.includes("pageCheck")) { P.checks++; return hooks.check ? hooks.check(P) : { required_empty: [], errors: [] }; }
      if (expr.includes("innerText || el.value")) {
        const m = /element\((\d+)\)/.exec(expr);
        return { 1: "Apply", 5: "Sign in", 6: "Create account", 8: "Next", 20: "Submit" }[m && m[1]] || "";
      }
      if (expr.includes("TailorAgent.act(")) {
        const a = JSON.parse(expr.slice(expr.indexOf("(") + 1, expr.lastIndexOf(")")));
        P.acts.push(a);
        if (hooks.onAct) hooks.onAct(P, a);
        return { ok: true };
      }
      return true;
    },
  };
  return { pane: { webContents: wc }, P };
}
function setup(start, brain, hooks) {
  const { pane, P } = fakePane(start, hooks);
  const posts = [], progress = [], opened = [];
  const deps = {
    noPolling: true,
    getPane: () => pane,
    openInPane: (u) => { opened.push(u); if (hooks && hooks.onOpen) hooks.onOpen(P, u); else P.at = "job"; },
    waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => { if (P.hang.has("fill")) return never(); return { ok: true, filled: P.at === "form" ? 2 : 0, left: P.at === "form" ? ["ZIP code"] : [] }; },
    sendProgress: (info) => progress.push(JSON.parse(JSON.stringify(info))),
    acquireDebugger: () => null, frames: {},
    applyDecision: async () => ({ fill: true, click_submit: false }),
    isNewPage: () => true, attachResume: async () => false,
    getApp: async (p) => (p.startsWith("/api/autoapply/agent/parked") ? { ok: true, parked: (hooks && hooks.parked) || [] } : { hosts: [] }),
    postApp: async (p, b) => {
      posts.push([p, b]);
      if (p === "/api/autoapply/agent/step") return brain(P, b, posts);
      if (hooks && hooks.post) { const r = hooks.post(p, b, P); if (r !== undefined) return r; }
      if (p === "/api/autoapply/agent/portal-account") return { ok: true, created: true };
      return { ok: true };
    },
  };
  const agent = createAgent(deps);
  return { agent, posts, progress, opened, P, pane };
}
const feedLines = (progress, rid) => {
  const hit = progress.filter((x) => x.record_id === rid && x.activity).pop();
  return ((hit && hit.activity.events) || []).map((e) => e.state + ": " + e.verb + (e.summary ? " " + e.summary : "") + (e.detail ? " (" + e.detail + ")" : ""));
};
const stepBodies = (posts) => posts.filter(([p]) => p === "/api/autoapply/agent/step").map(([, b]) => b);
const ok = (actions, extra) => Object.assign({ ok: true, summary: "Working on it", actions, done: null, cost: 0.01 }, extra || {});
const READY = ok([], { done: { outcome: "ready_to_submit" } });
// The usual way through: Apply, sign in, Next, ready.
function plainBrain(P, b) {
  if (P.at === "job") return ok([{ op: "click", index: 1 }]);
  if (P.at === "auth") return ok([{ op: "type", index: 3, text: "kofi@example.com", sensitive: true, placeholder: "portal_email" },
                                  { op: "type", index: 4, text: "s3cretPW!", sensitive: true, placeholder: "portal_password" }, { op: "click", index: 5 }]);
  if (P.at === "form") return ok([{ op: "click", index: 8 }], { plan: ["done: Job page", "done: Sign in", "now: My information", "todo: Review"] });
  if (P.at === "review") return READY;
  return ok([{ op: "wait", seconds: 1 }]);
}
const walk = (P, a) => {
  if (P.at === "job" && a.op === "click" && a.index === 1) P.at = "auth";
  else if (P.at === "auth" && a.op === "click" && a.index === 5) P.at = P.afterAuth || "form";
  else if (P.at === "form" && a.op === "click" && a.index === 8) P.at = "review";
};

(async () => {
  const T0 = Date.now();
  // ---- 1. pure helpers --------------------------------------------------------------------
  assert(MAX_STEPS === 60 && COST_CAP_USD === 0.5, "limits from the environment " + MAX_STEPS + " " + COST_CAP_USD);
  assert(notReady(PAGES.auth.view) === "the page has a password box", "a password box is never the review page");
  assert(notReady("[3]<input type=email label=\"Email\" value=\"\">\n[5]<button>Sign in</button>") === "the page is a sign-in or create-account form", "a sign-in form");
  assert(notReady(PAGES.form.view) === "the page shows no Submit control", "no submit control");
  assert(notReady(PAGES.review.view) === "", "the review page passes " + notReady(PAGES.review.view));
  assert(lineName("[8]<button>Next</button>") === "Next" && lineName("[9]<input label=\"ZIP code\" value=\"\" required>") === "ZIP code", "lineName");
  assert(JSON.stringify(safeReply({ actions: [{ op: "type", text: "s3cret", sensitive: true }] })).includes("(secret)"), "safeReply hides a secret");

  console.log("section 2 at " + Math.round((Date.now() - T0) / 1000) + " s");
  // ---- 2. a hang: the page never answers; the ladder reloads it; Stop preempts a hang ------
  {
    const { agent, progress, P } = setup("job", plainBrain, {
      onAct: walk, onReload: (p) => { p.hang.delete("view"); p.at = "form"; } });
    P.afterAuth = "skeleton";
    P.hang.add("view");
    const t0 = Date.now();
    const r = await agent.run(1, { url: PAGES.job.url, company: "Acme" });
    assert(r.ok && r.ready, "the run ends ready after the reload " + JSON.stringify(r));
    assert(P.reloads === 1, "one reload " + P.reloads);
    const f = feedLines(progress, 1);
    assert(f.some((l) => /^retry: Trying another way (waiting for the form|reloading the page)/.test(l)), "the ladder shows on the feed " + f.join(" | "));
    assert(f.some((l) => /the page did not answer/.test(l)), "the hang is said in words " + f.join(" | "));
    assert(Date.now() - t0 < 15000, "no frozen loop: " + (Date.now() - t0) + " ms");
  }
  {
    const { agent, P } = setup("job", plainBrain, { onAct: walk });
    P.hang.add("all");
    const t0 = Date.now();
    const p = agent.run(2, { url: PAGES.job.url });
    setTimeout(() => agent.stop(), 100);
    const r = await p;
    assert(r.reason === "stopped" && Date.now() - t0 < 2000, "Stop takes effect inside a hung call: " + JSON.stringify(r) + " after " + (Date.now() - t0) + " ms");
  }
  {
    // the fill engine hangs: capped, then the ladder (not a frozen loop)
    const { agent, P, progress } = setup("job", plainBrain, { onAct: walk, onReload: (p) => { p.hang.delete("fill"); } });
    P.hang.add("fill");
    const r = await agent.run(3, { url: PAGES.job.url });
    assert(r.ok && r.ready && P.reloads >= 1, "a hung fill is capped and recovered " + JSON.stringify(r) + " reloads=" + P.reloads);
    assert(feedLines(progress, 3).some((l) => /fill timed out|did not answer/.test(l)), "says the fill did not answer");
  }

  console.log("section 3 at " + Math.round((Date.now() - T0) / 1000) + " s");
  // ---- 3. a skeleton page with the brain waiting: three waits, then the shell escalates ----
  {
    const { agent, P, posts } = setup("job", (P, b) => (P.at === "skeleton" ? ok([{ op: "wait", seconds: 1 }]) : plainBrain(P, b)),
                                      { onAct: walk, onReload: (p) => { p.at = "form"; } });
    P.afterAuth = "skeleton";
    const r = await agent.run(4, { url: PAGES.job.url });
    assert(r.ok && r.ready, "the skeleton run ends ready " + JSON.stringify(r));
    assert(P.reloads === 1, "waits capped, then wait_for form, then reload " + P.reloads);
    const waits = stepBodies(posts).filter((b) => b.url === PAGES.skeleton.url).length;
    assert(waits <= 8, "not endless waiting: " + waits + " brain calls on the skeleton");
  }
  {
    // a blank page for longer than SJ_AGENT_HANG_S, whatever the brain does: the ladder
    const { agent, P } = setup("job", (P, b) => (P.at === "skeleton" ? ok([{ op: "read_errors" }]) : plainBrain(P, b)),
                               { onAct: walk, onReload: (p) => { p.at = "form"; } });
    P.afterAuth = "skeleton";
    const r = await agent.run(5, { url: PAGES.job.url });
    assert(r.ok && r.ready && P.reloads === 1, "hang detection by time " + JSON.stringify(r) + " reloads=" + P.reloads);
  }

  console.log("section 4 at " + Math.round((Date.now() - T0) / 1000) + " s");
  // ---- 4. a bad model reply: retried by the engine, then the ladder here; an account problem stops the batch
  {
    let bad = 0;
    const { agent, posts, progress } = setup("job", (P, b) => {
      if (P.at === "auth" && bad < 4) { bad++; return { ok: false, reason: "no-reply", error: "bad reply: {oops", tries: 3, cost: 0.002 }; }
      return plainBrain(P, b);
    }, { onAct: walk });
    const r = await agent.run(6, { url: PAGES.job.url });
    assert(r.ok && r.ready, "four bad replies do not park the job " + JSON.stringify(r));
    const nudged = stepBodies(posts).find((b) => (b.nudges || []).some((n) => /not making progress/.test(n)));
    assert(nudged, "the ladder's first rung is a hint to the AI");
    const f = feedLines(progress, 6);
    assert(f.some((l) => /Asking the AI again \(bad reply: \{oops\)/.test(l)), "the real error text is on the feed " + f.join(" | "));
    assert(f.some((l) => /with a hint about what went wrong/.test(l)), "the hint shows as trying another way");
  }
  {
    const fatal = { ok: false, reason: "no-reply", fatal: "no_credit", error: "no_credit: out of usage credits",
                    message: "SponsorJobs' AI is unavailable right now (credits). Nothing was changed; try again later." };
    const { agent, posts, progress } = setup("job", () => fatal, { onAct: walk });
    const r = await agent.batch([{ recordId: 7, url: PAGES.job.url }, { recordId: 8, url: PAGES.job.url }, { recordId: 9, url: PAGES.job.url }]);
    assert(r.ok && r.results.length === 1 && r.results[0].reason === "ai-unavailable", "the batch stops after the first account error " + JSON.stringify(r.results));
    assert(!posts.some(([p]) => p === "/api/autoapply/agent/park"), "never parked as needs you");
    assert(posts.some(([p, b]) => p === "/api/autoapply/agent/unavailable" && b.record_id === 7 && /credits/.test(b.message)), "the row and Telegram are told");
    const done = progress.find((x) => x.state === "batch-done");
    assert(done && /Stopped after 1 application/.test(done.message) && /credits/.test(done.message), "the batch says why " + (done && done.message));
    const st = progress.find((x) => x.record_id === 7 && x.state === "stopped");
    assert(st && st.reason === "ai-unavailable" && /Nothing was changed/.test(st.message), "the bar says it plainly " + JSON.stringify(st));
  }

  console.log("section 5 at " + Math.round((Date.now() - T0) / 1000) + " s");
  // ---- 5. the read-only tools come back as observations; the page tools change the page ----
  {
    let toolsDone = false;
    const { agent, posts, P, opened } = setup("job", (P, b) => {
      if (P.at === "form" && !toolsDone) {
        toolsDone = true;
        return ok([{ op: "read_page", offset: 5 }, { op: "find", text: "Next" }, { op: "inspect", index: 3 }, { op: "list_options", index: 3 },
                   { op: "read_errors" }], { plan: ["done: Job page", "now: My information"] });
      }
      if (P.at === "form" && !P.waited) { P.waited = true; return ok([{ op: "wait_for", text: "Hello", seconds: 1 }, { op: "screenshot" }, { op: "back" }]); }
      if (P.at === "form" && !P.reopened) { P.reopened = true; return ok([{ op: "reopen_job" }]); }
      return plainBrain(P, b);
    }, { onAct: walk, onBack: (p) => { p.at = "form"; }, onOpen: (p) => { p.opens = (p.opens || 0) + 1; p.at = p.opens === 1 ? "job" : "form"; } });
    const r = await agent.run(10, { url: PAGES.job.url });
    assert(r.ok && r.ready, "the tools run ends ready " + JSON.stringify(r));
    const bodies = stepBodies(posts);
    const withObs = bodies.find((b) => (b.observations || []).length >= 5);
    assert(withObs, "the observations reach the next brain call " + JSON.stringify(bodies.map((b) => (b.observations || []).length)));
    const o = withObs.observations.join("\n");
    assert(/read_page from \[5\]:\n\[5\]<input label="Extra"/.test(o) && /find "Next": \n\[8\]<button>Next/.test(o) && /inspect \[3\]: \{"index":3/.test(o)
           && /options for \[3\]: US \| CA \| GB/.test(o) && /errors on the page: ZIP code is required/.test(o), "each tool's observation " + o);
    const later = bodies.find((b) => (b.observations || []).some((x) => /wait_for "Hello": did not appear within 1 s/.test(x)));
    assert(later && later.observations.some((x) => /screenshot: the pane could not be captured/.test(x)) && later.observations.some((x) => /back: went back a page/.test(x)), "wait_for, screenshot and back " + JSON.stringify(later && later.observations));
    assert(P.backs === 1 && opened.filter((u) => u === PAGES.job.url).length === 2, "back (to a page of this application) and reopen_job drove the pane " + P.backs + " " + JSON.stringify(opened));
    assert(bodies.some((b) => (b.plan || []).includes("now: My information")), "the plan rides along");
    const planned = bodies[bodies.length - 1];
    assert(planned.plan && planned.plan.length, "the last brain call still carries the plan");
  }

  console.log("section 6 at " + Math.round((Date.now() - T0) / 1000) + " s");
  // ---- 6. the page check holds Next once when required fields are empty -------------------
  {
    const { agent, posts, P } = setup("job", plainBrain, { onAct: walk, check: (p) => ({ required_empty: ["ZIP code"], errors: [] }) });
    const r = await agent.run(11, { url: PAGES.job.url });
    assert(r.ok && r.ready && P.checks === 1, "the check runs once per page " + P.checks + " " + JSON.stringify(r));
    const held = stepBodies(posts).find((b) => (b.observations || []).some((x) => /Page check before Next: required fields still empty: ZIP code/.test(x)));
    assert(held, "the held click comes back as an observation");
    assert(P.acts.filter((a) => a.op === "click" && a.index === 8).length === 1, "Next was clicked once, after the check");
  }

  console.log("section 7 at " + Math.round((Date.now() - T0) / 1000) + " s");
  // ---- 7. the ready gate refuses "ready" on a sign-in page; the critic gets one more round --
  {
    let saidReady = false, critics = 0;
    const { agent, posts, progress } = setup("job", (P, b) => {
      if (P.at === "auth" && !saidReady) { saidReady = true; return READY; }
      return plainBrain(P, b);
    }, { onAct: walk, post: (p) => { if (p === "/api/autoapply/agent/critic") { critics++; return { ok: false, issues: ["required and still empty: Country"], cost: 0.003 }; } } });
    const r = await agent.run(12, { url: PAGES.job.url });
    assert(r.ok && r.ready, "ends ready " + JSON.stringify(r));
    const refused = stepBodies(posts).find((b) => (b.nudges || []).some((n) => /ready_to_submit but the page has a password box/.test(n)));
    assert(refused, "the gate refused ready on the sign-in page and told the brain");
    assert(critics === 1, "one critic call " + critics);
    const round = stepBodies(posts).find((b) => (b.nudges || []).some((n) => /Before I hand over, a check found: required and still empty: Country/.test(n)));
    assert(round, "the critic's issues gave the brain one more round");
    const last = progress[progress.length - 1];
    assert(last.state === "ready" && /A check noted: required and still empty: Country/.test(last.message), "the hand-over names what was noted " + last.message);
    const snap = last.activity;
    assert(snap.cost > 0 && snap.cost_cap === 0.5 && Array.isArray(snap.plan) && snap.plan.length === 4, "the snapshot carries the spend and the plan " + JSON.stringify({ c: snap.cost, p: snap.plan }));
  }

  console.log("section 8 at " + Math.round((Date.now() - T0) / 1000) + " s");
  // ---- 8. the site's mail never arrives: one more look, then park; resume looks again -------
  {
    let awaits = 0;
    const hooks = { onAct: walk, post: (p, b) => { if (p === "/api/inbox/await") { awaits++; return awaits <= 2 ? { ok: false, reason: "timeout" } : { ok: true, kind: "verify", link: "https://jobs.example.com/activate/tok" }; } } };
    const brain = (P, b) => {
      if (P.at === "mailed" && !(b.history || []).some((h) => (h.results || []).includes("opened the emailed link"))) return ok([], { done: { outcome: "await_email", reason: "link" } });
      if (P.at === "mailed") return ok([{ op: "click", index: 5 }]);
      return plainBrain(P, b);
    };
    const s = setup("job", brain, hooks);
    s.P.afterAuth = "mailed";
    s.hooks = hooks;
    const r = await s.agent.run(13, { url: PAGES.job.url, company: "Acme" });
    assert(r.parked && r.kind === "person", "parks when the mail does not come " + JSON.stringify(r));
    assert(awaits === 2, "the inbox was asked twice (the wait, then one more look) " + awaits);
    const parkedMsg = s.progress.find((x) => x.record_id === 13 && x.state === "parked");
    assert(/The Acme email \(a link\) did not arrive/.test(parkedMsg.message) && /press Continue/.test(parkedMsg.message), parkedMsg.message);
    const mem = s.agent._test.parked.get("13");
    assert(mem && mem.state && mem.state.await && mem.state.await.kind === "link", "the park remembers the mail it waited on");
    // Continue: the resumed run looks in the inbox once more first, opens the link, signs in, carries on
    const entry = { record_id: "13", state: "answered", kind: "person", page_url: PAGES.mailed.url, run: { url: PAGES.job.url, company: "Acme" } };
    const post2 = (p, b, P) => { if (p === "/api/autoapply/agent/parked/13/take") return { ok: true, entry }; return hooks.post(p, b, P); };
    const sealed = setup("mailed", brain, { onAct: walk, post: post2, parked: [entry],
                                            onOpen: (P, u) => { if (/activate/.test(u)) P.at = "mailed"; else P.at = "job"; } });
    sealed.agent._test.parked.set("13", Object.assign({}, mem, { epoch: sealed.agent._test.epoch(), pageUrl: PAGES.mailed.url, opts: { url: PAGES.job.url, company: "Acme" } }));
    sealed.P.afterAuth = "form";
    const pr = await sealed.agent.pollParked();
    assert(pr.resumed === 1, "the parked job resumed " + JSON.stringify(pr));
    assert(sealed.opened.some((u) => /activate\/tok/.test(u)), "the late link was opened on resume " + JSON.stringify(sealed.opened));
    const end = sealed.progress.filter((x) => x.record_id === 13).pop();
    assert(end.state === "ready", "and the run went on to Ready to send: " + end.state + " " + end.message);
  }

  console.log("section 9 at " + Math.round((Date.now() - T0) / 1000) + " s");
  // ---- 9. a batch moves on from a job that takes too long -----------------------------------
  {
    process.env.SJ_AGENT_JOB_CAP_MS = "1200";
    delete require.cache[require.resolve(path.join(__dirname, "..", "..", "shell-electron", "agent.js"))];
    const fresh = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
    const { pane, P } = fakePane("job", { onAct: walk });
    const posts = [], progress = [];
    const agent = fresh.createAgent({
      noPolling: true, getPane: () => pane, openInPane: () => { P.at = "job"; }, waitPaneLoad: async () => {},
      autofillCore: () => "", agentViewSource: () => "", runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: [] }),
      sendProgress: (i) => progress.push(JSON.parse(JSON.stringify(i))), acquireDebugger: () => null, frames: {},
      applyDecision: async () => ({ fill: true }), isNewPage: () => false, attachResume: async () => false,
      getApp: async () => ({ hosts: [], parked: [] }),
      postApp: async (p, b) => { posts.push([p, b]); if (p === "/api/autoapply/agent/step") return ok([{ op: "wait", seconds: 1 }]); return { ok: true }; },
    });
    const r = await agent.batch([{ recordId: 14, url: PAGES.job.url }]);
    assert(r.ok && r.results[0].parked, "the slow job parked " + JSON.stringify(r.results));
    const pk = progress.find((x) => x.record_id === 14 && x.state === "parked");
    assert(/took too long \(0 minutes\): /.test(pk.message) && /Moving on/.test(pk.message), "took too long, with the last state " + pk.message);
    assert(posts.some(([p, b]) => p === "/api/autoapply/agent/park" && b.record_id === 14), "parked on the engine too");
    delete process.env.SJ_AGENT_JOB_CAP_MS;
  }

  console.log("section 11 at " + Math.round((Date.now() - T0) / 1000) + " s");
  // ---- 11. the sign-in wall, deterministically (the e2e of 2026-11-02) --------------------
  // A brain that signs in again and again (the loop the e2e saw): the shell counts the failed
  // sign-ins, opens Create account after two, Forgot password after the next failure, sees the
  // "check your email" page and waits for the mail itself, opens the reset link, and carries on.
  {
    let inbox = 0;
    const brain = (P, b) => {
      if (P.at === "job") return ok([{ op: "click", index: 1 }]);
      if (P.at === "wall" || P.at === "wallErr") return ok([{ op: "type", index: 3, text: "kofi@example.com", sensitive: true, placeholder: "portal_email" },
                                                            { op: "type", index: 4, text: "s3cretPW!", sensitive: true, placeholder: "portal_password" }, { op: "click", index: 5 }]);
      if (P.at === "create") return ok([{ op: "type", index: 3, text: "kofi@example.com", sensitive: true }, { op: "type", index: 4, text: "s3cretPW!", sensitive: true },
                                        { op: "type", index: 5, text: "s3cretPW!", sensitive: true }, { op: "click", index: 6 }]);
      if (P.at === "forgot") return ok([{ op: "type", index: 3, text: "kofi@example.com", sensitive: true }, { op: "click", index: 4 }]);
      if (P.at === "resetSent") return ok([{ op: "scroll", direction: "down" }]);       // the shell must not wait for the brain here
      if (P.at === "newpw") return ok([{ op: "type", index: 3, text: "s3cretPW!", sensitive: true }, { op: "type", index: 4, text: "s3cretPW!", sensitive: true }, { op: "click", index: 5 }]);
      return plainBrain(P, b);
    };
    const s = setup("job", brain, {
      onAct: (P, a) => {
        if (P.at === "job" && a.op === "click" && a.index === 1) P.at = "wall";
        else if ((P.at === "wall" || P.at === "wallErr") && a.op === "click" && a.index === 5) P.at = P.resetDone ? "form" : "wallErr";
        else if ((P.at === "wall" || P.at === "wallErr") && a.op === "click" && a.index === 6) P.at = "create";
        else if ((P.at === "wall" || P.at === "wallErr") && a.op === "click" && a.index === 7) P.at = "forgot";
        else if (P.at === "create" && a.op === "click" && a.index === 6) P.at = "wallErr";        // "this email is already registered"
        else if (P.at === "forgot" && a.op === "click" && a.index === 4) P.at = "resetSent";
        else if (P.at === "newpw" && a.op === "click" && a.index === 5) { P.resetDone = true; P.at = "form"; }
        else walk(P, a);
      },
      onOpen: (P, u) => { if (/reset\?token/.test(u)) P.at = "newpw"; else P.at = "job"; },
      post: (p) => { if (p === "/api/inbox/await") { inbox++; return { ok: true, kind: "reset", link: "https://jobs.example.com/reset?token=t1" }; } },
    });
    const r = await s.agent.run(20, { url: PAGES.job.url, company: "Acme" });
    assert(r.ok && r.ready, "the sign-in wall run ends ready " + JSON.stringify(r));
    const clicks = s.P.acts.filter((a) => a.op === "click").map((a) => a.index);
    const signins = s.P.acts.filter((a) => a.op === "click" && a.index === 5).length;
    assert(clicks.includes(6) && clicks.includes(7), "the shell opened Create account and Forgot password " + JSON.stringify(clicks));
    assert(signins <= 4, "never the same sign-in over and over: " + signins + " sign-in clicks");
    assert(clicks.indexOf(6) < clicks.indexOf(7), "Create account before Forgot password");
    assert(inbox === 1 && s.opened.some((u) => /reset\?token=t1/.test(u)), "the shell waited for the reset mail itself and opened its link " + inbox);
    const f = feedLines(s.progress, 20);
    assert(f.some((l) => /Trying another way creating the account/.test(l)) && f.some((l) => /Trying another way resetting the password/.test(l)), "the feed says so " + f.join(" | "));
    assert(f.some((l) => /Waiting for the site's email \(the link arrived\)/.test(l)), "the email wait shows " + f.join(" | "));
    const shellSteps = stepBodies(s.posts).length;
    assert(shellSteps < 25, "no step budget burnt: " + shellSteps + " brain calls");
  }
  // Workday's "An email has been sent to you. Please verify your account." over the sign-in form:
  // the shell waits for the mail without asking the brain, opens the activate link, the brain
  // signs in from the "activated" page, the sign-in lands on the job SEARCH page (a header search
  // box on every page), the shell opens the job again, and the run carries on to Ready to send.
  {
    let inbox = 0;
    const brain = (P, b) => {
      if (P.at === "mailed") return ok([{ op: "wait", seconds: 1 }]);           // the shell, not the brain, must act on the banner
      if (P.at === "activated") return ok([{ op: "click", index: 2 }]);
      if (P.at === "search") return ok([{ op: "type", index: 1, text: "Analyst", sensitive: false }]);
      return plainBrain(P, b);
    };
    const s = setup("job", brain, {
      onAct: (P, a) => {
        if (P.at === "job" && a.op === "click" && a.index === 1) P.at = P.activated ? "auth" : "mailed";
        else if (P.at === "activated" && a.op === "click" && a.index === 2) P.at = "auth";
        else if (P.at === "auth" && a.op === "click" && a.index === 5) { P.at = P.searched ? "form" : "search"; P.searched = true; }
        else walk(P, a);
      },
      onOpen: (P, u) => { if (/activate\/tok/.test(u)) { P.activated = true; P.at = "activated"; } else P.at = "job"; },
      post: (p) => { if (p === "/api/inbox/await") { inbox++; return { ok: true, kind: "verify", link: "https://jobs.example.com/activate/tok" }; } },
    });
    const r = await s.agent.run(21, { url: PAGES.job.url, company: "Acme" });
    assert(r.ok && r.ready, "the activation run ends ready " + JSON.stringify(r));
    assert(inbox === 1, "one wait for the verification mail " + inbox);
    assert(s.opened.filter((u) => u === PAGES.job.url).length === 2 && s.opened.some((u) => /activate\/tok/.test(u)), "the link, then the job again " + JSON.stringify(s.opened));
    assert(!s.P.acts.some((a) => a.op === "type" && a.text === "Analyst"), "the search page was left to the shell, never searched by hand");
    const f = feedLines(s.progress, 21);
    assert(f.some((l) => /Waiting for the site's email \(the link arrived\)/.test(l)) && f.some((l) => /opening the job again/.test(l)), "the feed " + f.join(" | "));
  }
  // The search-page reopen never fires on a page of the form under a header search box, nor on
  // the activated page; and back never leaves this application (Electron's history holds the
  // previous job's pages).
  {
    const { agent, P, posts, pane } = setup("job", (P, b) => (P.at === "form" && !P.backed ? (P.backed = true, ok([{ op: "back" }])) : plainBrain(P, b)), { onAct: walk });
    pane.webContents.navigationHistory.getActiveIndex = () => 3;
    pane.webContents.navigationHistory.getEntryAtIndex = () => ({ url: "https://jobs.example.com/other-job?skeleton=1" });
    const r = await agent.run(22, { url: PAGES.job.url });
    assert(r.ok && r.ready && P.backs === 0, "back refused when it would leave the application " + P.backs + " " + JSON.stringify(r));
    assert(stepBodies(posts).some((b) => (b.observations || []).some((o) => /no earlier page of this application/.test(o))), "and the brain is told why");
  }

  console.log("section 10 at " + Math.round((Date.now() - T0) / 1000) + " s");
  // ---- 10. the debug log never carries a secret --------------------------------------------
  {
    const log = fs.readFileSync(path.join(TMP, "sj-agent.log"), "utf8");
    assert(log.length > 0 && /reply:/.test(log), "the debug log was written");
    assert(!/s3cretPW!|kofi@example\.com/.test(log), "no secret in the log");
    assert(/\(secret\)/.test(log), "secrets read (secret)");
    assert(/ladder reload|ladder wait_for form/.test(log), "the ladder is logged");
  }
  fs.rmSync(TMP, { recursive: true, force: true });
  console.log("ok");
  // The fake panes' never-resolving promises keep guard timers armed; nothing else is pending.
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (x) { /* gone */ } process.exit(1); });
