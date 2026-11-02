// The apply agent's live activity feed without Electron (tests/test_activity_feed.py runs it):
// createFeed's rules, then a mock run through a fake pane (job page, sign-in, a form page that
// the engine fills and that opens a privacy agreement, a review page) with a scripted brain. The
// feed must show the shell's own steps and the brain's summaries with the right markers, every
// progress line must carry the snapshot, and the messages must stay what they were.
"use strict";
const path = require("path");
const { createAgent, createFeed, splitSummary } = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };

// ---- 1. createFeed -------------------------------------------------------------------------
{
  const f = createFeed({ autonomous: false });
  f.push("open", "Opening", "the job");
  f.push("read", "Reading", "the job page", { transient: true });
  f.push("act", "Clicking", "Apply to open the form");
  assert(f.events.length === 2 && f.events[0].state === "done", "a transient step is replaced " + JSON.stringify(f.events));
  f.mark("retry", "something on the page was covering it");
  f.push("read", "Reading", "the page", { transient: true });
  f.push("act", "Clicking", "Apply again");
  assert(f.events.length === 3 && f.events[1].state === "retry" && f.events[2].state === "running",
         "a retry stays a retry and the next action is its own line " + JSON.stringify(f.events));
  f.push("fit", "Waiting", "for the site's email");
  f.push("fit", "Waiting", "for the site's email", { detail: "still waiting" });
  assert(f.events.length === 4 && f.events[3].detail === "still waiting", "same phase updates in place");
  f.push("ask", "Asking you", "", { state: "asking", detail: "How many years of SQL?" });
  f.close();
  assert(f.events[4].state === "done", "close ends the current step");
  for (const e of f.events) for (const k of ["phase", "verb", "target", "summary", "state", "at"]) assert(k in e, "shape " + k);
  f.readPages("Apply\nStep 2 of 4\n[3]<button>Next</button>");
  f.page = 2;
  const s = f.snap();
  assert(s.page === 2 && s.pages === 4 && s.max_steps === 40 && s.autonomous === false, "snap " + JSON.stringify(s));
  f.readPages("no counter here");
  assert(f.snap().pages === 0, "no pages when the site shows none");
  const sp = splitSummary("Clicking Next to open page 3");
  assert(sp.verb === "Clicking" && sp.rest === "Next to open page 3", "split");
  const g = createFeed({});
  g.push("x", "Fitting — the page", "a–b");
  assert(!/[–—]/.test(g.events[0].verb + g.events[0].summary), "no dashes");
}

// ---- 2. a mock run ---------------------------------------------------------------------------
const PAGES = {
  job: { url: "https://jobs.example.com/job/1", sig: { labels: [] }, view: "Business Analyst\n[1]<button>Apply</button>" },
  auth: { url: "https://jobs.example.com/login", sig: { blocker: "auth", labels: ["Email", "Password"] },
          view: "Sign in\n[3]<input type=email label=\"Email\">\n[4]<input type=password label=\"Password\">\n[5]<button>Sign in</button>" },
  form: { url: "https://jobs.example.com/apply/2", sig: { labels: ["Phone", "Country"] },
          view: "My Information\nStep 2 of 3\n[7]<input label=\"Phone\" value=\"(filled)\">\n[8]<button>Next</button>" },
  review: { url: "https://jobs.example.com/apply/3", sig: { labels: ["Review"] },
            view: "Review\nStep 3 of 3\n[20]<button>Submit</button>" },
};
let at = "job";
let failNext = 0;
let termsShown = false;
const wc = {
  getURL: () => PAGES[at].url, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
  executeJavaScript: async (expr) => {
    if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
    if (expr.includes("__tailorQuiet.at")) return "5000|complete";
    if (expr.includes("pageSignature")) return PAGES[at].sig;
    if (expr.includes("termsItems")) {
      if (at === "form" && !termsShown) { termsShown = true; return [{ title: "Data Privacy Agreement", index: 9, kind: "dialog" }]; }
      return [];
    }
    if (expr.includes("crossOriginFrames")) return 0;
    if (expr.includes("hasResumeUpload")) return false;
    if (expr.includes("TailorAgent.view(")) return { text: "URL: " + PAGES[at].url + "\n" + PAGES[at].view };
    if (expr.includes("innerText || el.value")) {
      const m = /element\((\d+)\)/.exec(expr);
      return { 1: "Apply", 5: "Sign in", 8: "Next", 9: "Accept", 20: "Submit" }[m && m[1]] || "";
    }
    if (expr.includes("TailorAgent.act(")) {
      const a = JSON.parse(expr.slice(expr.indexOf("(") + 1, expr.lastIndexOf(")")));
      if (failNext > 0) { failNext--; return { ok: false, note: "covered" }; }
      if (a.op === "click" && a.index === 1) at = "auth";
      else if (a.op === "click" && a.index === 5) at = "form";
      else if (a.op === "click" && a.index === 8) at = "review";
      return { ok: true };
    }
    return true;
  },
};
const pane = { webContents: wc };
const BRAIN = {
  job: () => ({ ok: true, summary: "Clicking Apply to open the form", actions: [{ op: "click", index: 1 }], done: null }),
  auth: () => ({ ok: true, summary: "Signing in with your sign-in email",
                 actions: [{ op: "type", index: 3, text: "kofi@example.com", sensitive: true }, { op: "click", index: 5 }], done: null }),
  form: () => ({ ok: true, summary: "Clicking Next to reach the review page", actions: [{ op: "click", index: 8 }], done: null }),
  review: () => ({ ok: true, summary: "Stopping at the submit button for you", actions: [], done: { outcome: "ready_to_submit" } }),
};
const progress = [];
const deps = {
  noPolling: true,
  getPane: () => pane,
  openInPane: () => { at = "job"; },
  waitPaneLoad: async () => {},
  autofillCore: () => "", agentViewSource: () => "",
  runAutoApplyOnPane: async () => (at === "form" ? { ok: true, filled: 3, left: [] } : { ok: true, filled: 0, left: [] }),
  sendProgress: (info) => progress.push(JSON.parse(JSON.stringify(info))),
  acquireDebugger: () => null, frames: {},
  applyDecision: async () => ({ fill: true, click_submit: false }),
  isNewPage: () => true, attachResume: async () => false,
  getApp: async () => ({ hosts: [] }),
  postApp: async (p, b) => {
    if (p === "/api/autoapply/agent/step") {
      if (at === "auth" && b.step === 2) failNext = 1;      // the first type is covered: a retry
      return BRAIN[at]();
    }
    if (p === "/api/autoapply/agent/terms") return { ok: true, decision: "accept", via: "always" };
    return { ok: true };
  },
};

(async () => {
  const agent = createAgent(deps);
  const r = await agent.run(5, { url: PAGES.job.url, company: "Acme", lane: "assisted", autonomous: false });
  assert(r.ok && r.ready, "the mock run ends ready " + JSON.stringify(r));
  const last = progress[progress.length - 1];
  assert(last.state === "ready" && /^Ready to send\./.test(last.message), "ready message unchanged " + last.message);
  assert(progress.every((p) => p.activity && Array.isArray(p.activity.events)), "every line carries the snapshot");
  const evs = last.activity.events;
  const line = (e) => e.verb + (e.summary ? " " + e.summary : "");
  const lines = evs.map((e) => e.state + ": " + line(e) + (e.detail ? " (" + e.detail + ")" : ""));
  const has = (re, state) => evs.some((e) => re.test(line(e)) && (!state || e.state === state));
  assert(has(/^Opening the job$/, "done"), "Opening the job " + lines.join(" | "));
  assert(has(/^Clicking Apply to open the form$/, "done"), "the brain's summary " + lines.join(" | "));
  assert(has(/^Signing in with your account for this site$/, "done"), "Signing in " + lines.join(" | "));
  assert(has(/^Signing in with your sign-in email$/, "retry"), "a failed action is a retry " + lines.join(" | "));
  assert(evs.some((e) => e.state === "retry" && e.detail === "something on the page was covering it"), "the retry says why");
  assert(has(/^Filled 3 fields from your memory$/, "done"), "the engine's fill " + lines.join(" | "));
  assert(evs.some((e) => line(e) === "Accepted the Data Privacy Agreement" && e.detail === "you said yes to this in setup"), "Accepted " + lines.join(" | "));
  assert(has(/^Clicking Next to reach the review page$/, "done"), "Next " + lines.join(" | "));
  assert(has(/^Ready to send$/, "done") && evs[evs.length - 1].verb === "Ready to send", "Ready to send last " + lines.join(" | "));
  assert(!evs.some((e) => e.transient), "no transient step is left");
  assert(!evs.some((e) => /kofi@example\.com/.test(JSON.stringify(e))), "no value on the feed");
  assert(evs.filter((e) => /^fill/.test(e.phase)).length === 1, "one fill line per page");
  assert(last.activity.step === 4 && last.activity.max_steps === 40 && last.activity.autonomous === false, "limits " + JSON.stringify(last.activity));
  // A live line while it ran: some running update showed the brain's verb with the site's page counter.
  assert(progress.some((p) => p.state === "running" && p.activity.pages === 3 && p.activity.page === 2), "page 2 of 3 while on the form");
  // The messages the bar and the e2e read are the same set as before (feed updates repeat the last one).
  const msgs = new Set(progress.map((p) => p.message));
  for (const m of msgs) assert(/^(Opening the job|Reading the job page|Signing in|Page \d: deciding what to click|Ready to send\. .*)$/.test(m), "message " + m);
  if (process.env.SHOW) console.log(lines.join("\n"));
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
