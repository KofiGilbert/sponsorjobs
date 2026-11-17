// Review answers at Ready to send (shell-electron/agent.js), without Electron
// (tests/test_ready_answers.py runs it): every field the run filled with its origin, the lines that
// deserve a look flagged (an AI pick, an optional list filled by the closest match, a fallback, a
// resume tailored for another application), and self-identification values only in the app's
// local list: never on the activity feed, in the debug log, or in the Telegram text.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "sj-answers-"));
process.env.TMPDIR = TMP;
process.env.SJ_AGENT_DEBUG = "1";
const A = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const SELF = "Zyxwv Selfid Value";          // a self-identification value no other text contains

// ---- 1. the list: origins and flags -------------------------------------------------------
{
  const ans = A.createAnswers();
  A.addAnswer(ans, { page: 1, label: "Phone", value: "555 0100", from: "profile" });
  A.addAnswer(ans, { page: 2, label: "Field of Study", value: "Agricultural Economics", from: "closest" });
  A.addAnswer(ans, { page: 2, label: "Skills", value: "JavaScript", from: "skills" });
  A.addAnswer(ans, { page: 2, label: "Work Experience 1: Job Title", value: "Analyst", from: "history" });
  A.addAnswer(ans, { page: 2, label: "Work Experience 2: Job Title", value: "Intern", from: "history" });
  A.addAnswer(ans, { page: 3, label: "Gender", value: SELF, from: "library" });               // a saved self-ID answer
  A.addAnswer(ans, { page: 3, label: "Are you a protected veteran?", value: "I decline to answer", from: "eeo_decline" });
  A.addAnswer(ans, { page: 1, label: "Do you require sponsorship?", value: "Yes", from: "workauth" });
  A.addAnswer(ans, { page: 1, label: "Why this company?", value: "Because", from: "ai" });
  A.addAnswer(ans, { page: 1, label: "", value: "x", from: "ai" });                          // no question: dropped
  A.addFile(ans, { page: 2, kind: "resume", path: "/w/uploads/7/Sam Lee Resume.pdf", how: "attached" });
  A.addFile(ans, { page: 2, kind: "cover_letter", path: "/w/uploads/9/cover/Sam Lee Cover Letter.pdf", how: "attached" });
  const L = A.readyAnswers(ans, { recordId: 9 });
  const row = (label) => L.rows.find((r) => r.label === label);
  assert(L.rows.length === 9, "each filled field once " + JSON.stringify(L.rows));
  assert(L.rows.map((r) => r.page).join("") === "111222233", "page by page " + L.rows.map((r) => r.page).join(""));
  assert(row("Phone").from === "profile" && !row("Phone").flag, "profile, no flag");
  assert(row("Do you require sponsorship?").from === "workauth" && !row("Do you require sponsorship?").flag, "work authorization");
  assert(row("Why this company?").flag === "ai", "an AI pick is flagged");
  assert(row("Field of Study").flag === "closest" && row("Skills").flag === "skills", "closest-match picks are flagged");
  assert(row("Are you a protected veteran?").flag === "eeo_decline" && row("Are you a protected veteran?").self_id, "a fallback decline is flagged");
  assert(row("Gender").from === "eeo" && row("Gender").self_id && row("Gender").value === SELF, "a saved self-ID answer reads as self-identification, value kept locally");
  assert(row("Work Experience 1: Job Title") && row("Work Experience 2: Job Title"), "two sections, two lines");
  const res = L.files.find((f) => f.kind === "resume");
  assert(res.record_id === 7 && res.flag === "other_resume" && res.name === "Sam Lee Resume.pdf", "another application's resume is flagged " + JSON.stringify(L.files));
  const cl = L.files.find((f) => f.kind === "cover_letter");
  assert(cl.record_id === 9 && !cl.flag, "this job's own letter");
  assert(L.flagged === 5, "flagged count " + L.flagged);
  // this job's own resume: no flag; one the form already held under the same name: flagged
  const own = A.createAnswers();
  A.addFile(own, { kind: "resume", path: "/w/uploads/9/Sam Lee Resume.pdf", how: "attached" });
  assert(!A.readyAnswers(own, { recordId: 9 }).files[0].flag, "this job's resume is not flagged");
  const kept = A.createAnswers();
  A.addFile(kept, { kind: "resume", path: "/w/uploads/9/Sam Lee Resume.pdf", how: "listed" });
  assert(A.readyAnswers(kept, { recordId: 9 }).files[0].flag === "kept_file", "a file the form already held is flagged");
  assert(A.fileRecord("C:\\w\\uploads\\12\\a.pdf") === 12 && A.fileRecord("/w/cv-4.pdf") === 4 && A.fileRecord("/x/y.pdf") === null, "fileRecord");

  // the Telegram text: the same list, never a self-identification value
  const tg = A.answersTelegram(L, "Analyst at Acme");
  assert(!tg.includes(SELF) && !tg.includes("I decline to answer"), "no self-ID value on Telegram " + tg);
  assert(tg.includes("Gender: (self-identification, shown only in the app)"), "the question stays, the value does not");
  assert(tg.includes("Field of Study: Agricultural Economics") && tg.includes("\u26a0"), "flagged lines marked");
  assert(tg.includes("tailored for another application"), "the resume flag " + tg);
  assert(!/[\u2013\u2014]/.test(tg) && tg.length < 4096, "no dashes, fits a message");
  // the Ready to send notice the engine sends on Telegram: counts and the flagged lines only
  const n = A.readyNotice(L, 1763300000.5);
  assert(n.run === "1763300000.5" && n.filled === L.rows.length && n.flagged === 5, "notice counts " + JSON.stringify(n));
  assert(n.lines.length === 5 && n.lines.every((x) => x.includes("\u26a0")), "only flagged lines " + JSON.stringify(n.lines));
  assert(!JSON.stringify(n).includes(SELF) && !JSON.stringify(n).includes("555 0100"), "no self-ID value, no unflagged value");
  assert(n.lines.some((x) => x.startsWith("\u2022 Are you a protected veteran?: (self-identification, shown only in the app)")), "a flagged self-ID question keeps its question only");
  assert(n.lines.some((x) => x.includes("Field of Study: Agricultural Economics (closest match, check it)")), "same words as answersTelegram");
  const many = A.createAnswers();
  for (let i = 0; i < 20; i++) A.addAnswer(many, { page: 1, label: "Pick " + i, value: "v" + i, from: "ai" });
  const nm = A.readyNotice(A.readyAnswers(many, { recordId: 1 }), "");
  assert(nm.lines.length === 12 && nm.more === 8 && nm.flagged === 20, "capped " + JSON.stringify(nm));
  const big = A.createAnswers();
  for (let i = 0; i < 150; i++) A.addAnswer(big, { page: 1 + (i % 5), label: "Question number " + i + " " + "x".repeat(40), value: "v".repeat(60), from: "profile" });
  assert(A.answersTelegram(A.readyAnswers(big, { recordId: 1 }), "").length <= 3600, "a long list is cut");
}

// ---- 2. what a brain action answered ---------------------------------------------------------
{
  const view = [
    "[3]<input type=email label=\"Email\" value=\"\">",
    "[4]<input type=password label=\"Password\" value=\"\">",
    "[5]<input type=text label=\"How did you hear about us?\" value=\"\">",
    "[6]<radio label=\"Are you 18 or older?\" option=\"Yes\" checked=false>",
    "[7]<combobox label=\"Country\" value=\"\" expanded=true>",
    "[8]<option>Canada</option>",
    "[9]<button>Next</button>",
    "[10]<select label=\"State\" value=\"\" options=50 first=\"Alabama\">",
  ].join("\n");
  const b = (a, run) => A.brainAnswer(view, a, run || {});
  assert(b({ op: "type", index: 4, text: "pw", placeholder: "portal_password" }) === null, "never the portal password");
  assert(b({ op: "type", index: 3, text: "a@b.c", placeholder: "portal_email" }) === null, "never the sign-in email");
  const lib = b({ op: "type", index: 5, text: "Job board", placeholder: "answer_1" });
  assert(lib && lib.from === "library" && lib.label === "How did you hear about us?", "a saved answer " + JSON.stringify(lib));
  const you = b({ op: "type", index: 5, text: "Job board", placeholder: "answer_1" }, { "How did you hear about us?": "Job board" });
  assert(you.from === "you", "answered this run");
  const r = b({ op: "click", index: 6 });
  assert(r && r.label === "Are you 18 or older?" && r.value === "Yes" && r.from === "ai", "a radio the AI clicked " + JSON.stringify(r));
  const o = b({ op: "click", index: 8 });
  assert(o && o.label === "Country" && o.value === "Canada", "an option in the open list " + JSON.stringify(o));
  assert(b({ op: "click", index: 9 }) === null, "a Next is not an answer");
  const s = b({ op: "select", index: 10, option: "Ohio" });
  assert(s && s.label === "State" && s.value === "Ohio" && s.from === "ai", "a select");
  // the brain's later pick on the same question replaces its earlier word
  const ans = A.createAnswers();
  A.addAnswer(ans, Object.assign({ page: 1, via: "brain" }, b({ op: "type", index: 5, text: "Job" })));
  A.addAnswer(ans, Object.assign({ page: 1, via: "brain" }, b({ op: "type", index: 5, text: "Job board", placeholder: "answer_1" })));
  assert(ans.rows.length === 1 && ans.rows[0].value === "Job board", "one line per question " + JSON.stringify(ans.rows));
}

// ---- 2b. an answer from the person's saved facts (autoapply/grounded.py) is flagged with its fact
{
  const view = "[5]<input type=text label=\"Where in the US do you currently reside?\" value=\"\">\n"
    + "[6]<input type=text label=\"Current company\" value=\"\">";
  const runAnswers = { "Where in the US do you currently reside?": "Chicago, IL", "Current company": "Acme" };
  const runOrigins = A.noteOrigins({}, { origins: { "Where in the US do you currently reside?": "grounded", "Current company": "library" },
                                          facts: { "Where in the US do you currently reside?": "location" } });
  assert(Object.keys(runOrigins).length === 1, "only a grounded origin is kept " + JSON.stringify(runOrigins));
  const g = A.brainAnswer(view, { op: "type", index: 5, text: "Chicago, IL", placeholder: "answer_1" }, runAnswers, runOrigins);
  assert(g && g.from === "grounded" && g.fact === "location", "grounded with its fact " + JSON.stringify(g));
  const lib = A.brainAnswer(view, { op: "type", index: 6, text: "Acme", placeholder: "answer_2" }, runAnswers, runOrigins);
  assert(lib && lib.from === "you" && !lib.fact, "the rest as before " + JSON.stringify(lib));
  const ans = A.createAnswers();
  A.addAnswer(ans, Object.assign({ page: 1, via: "brain" }, g));
  const L = A.readyAnswers(ans, {});
  assert(L.rows[0].flag === "grounded" && L.rows[0].fact === "location" && L.flagged === 1, "flagged for a look " + JSON.stringify(L));
  assert(A.answersTelegram(L).includes("from your saved facts, check it"), "named on Telegram");
}

// ---- 3. a mock run: the list reaches the Ready to send line; self-ID stays out of the feed and log
const PAGES = {
  form: { url: "https://jobs.example.com/apply/2", sig: { labels: ["Phone", "City"] },
          view: "My Information\nStep 1 of 2\n[6]<input type=text label=\"City\" value=\"\">\n[7]<input label=\"Phone\" value=\"(filled)\">\n[8]<button>Next</button>" },
  review: { url: "https://jobs.example.com/apply/3", sig: { labels: ["Review"] }, view: "Review\nStep 2 of 2\n[20]<button>Submit</button>" },
};
let at = "form";
const fillOpts = [];
const wc = {
  getURL: () => PAGES[at].url, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
  executeJavaScript: async (expr) => {
    if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
    if (expr.includes("__tailorQuiet.at")) return "5000|complete";
    if (expr.includes("pageSignature")) return PAGES[at].sig;
    if (expr.includes("termsItems")) return [];
    if (expr.includes("crossOriginFrames")) return 0;
    if (expr.includes("resumeInputOpen")) return true;
    if (expr.includes("TailorAgent.view(")) return { text: "URL: " + PAGES[at].url + "\n" + PAGES[at].view };
    if (expr.includes("innerText || el.value")) return { 8: "Next", 20: "Submit" }[(/element\((\d+)\)/.exec(expr) || [])[1]] || "";
    if (expr.includes("TailorAgent.act(")) {
      const a = JSON.parse(expr.slice(expr.indexOf("(") + 1, expr.lastIndexOf(")")));
      if (a.op === "click" && a.index === 8) at = "review";
      return { ok: true };
    }
    return true;
  },
};
const pane = { webContents: wc };
const progress = [];
const posts = [];
const deps = {
  noPolling: true,
  getPane: () => pane,
  openInPane: () => { at = "form"; },
  waitPaneLoad: async () => {},
  autofillCore: () => "", agentViewSource: () => "",
  runAutoApplyOnPane: async (rid, o) => {
    fillOpts.push(o);
    if (at !== "form") return { ok: true, filled: 0, left: [], answers: [], files: [] };
    return { ok: true, filled: 3, left: [],
             answers: [{ label: "Phone", value: "555 0100", from: "profile" },
                       { label: "Gender", value: SELF, from: "eeo" },
                       { label: "Field of Study", value: "Agricultural Economics", from: "closest" }],
             files: [{ kind: "resume", path: "/w/uploads/4/Sam Lee Resume.pdf", how: "attached" }] };
  },
  sendProgress: (info) => progress.push(JSON.parse(JSON.stringify(info))),
  acquireDebugger: () => null, frames: {},
  applyDecision: async () => ({ fill: true, click_submit: false }),
  isNewPage: () => true, attachResume: async () => "listed",
  getApp: async () => ({ hosts: [] }),
  postApp: async (p, b) => {
    posts.push([p, JSON.stringify(b || {})]);
    if (p === "/api/autoapply/agent/step") {
      if (at === "form") return { ok: true, summary: "Typing your city", actions: [{ op: "type", index: 6, text: "Springfield", sensitive: true, placeholder: "city" }, { op: "click", index: 8 }], done: null };
      return { ok: true, summary: "Stopping at the submit button for you", actions: [], done: { outcome: "ready_to_submit" } };
    }
    return { ok: true };
  },
};

(async () => {
  const agent = A.createAgent(deps);
  const r = await agent.run(5, { url: PAGES.form.url, company: "Acme", lane: "assisted", autonomous: false, resumePath: "/w/uploads/5/Sam Lee Resume.pdf" });
  assert(r.ok && r.ready, "the mock run ends ready " + JSON.stringify(r));
  assert(fillOpts.length && fillOpts.every((o) => o && o.answers === true), "the agent asks the fill for its log");
  const last = progress[progress.length - 1];
  assert(last.state === "ready" && /^Ready to send\./.test(last.message), "the Ready line is unchanged " + last.message);
  const L = last.answers;
  assert(L && Array.isArray(L.rows), "the list rides on the Ready to send line");
  const row = (label) => L.rows.find((x) => x.label === label);
  assert(row("Phone").from === "profile", "the fill's answers " + JSON.stringify(L.rows));
  assert(row("City") && row("City").value === "Springfield" && row("City").from === "profile", "the brain's typed placeholder " + JSON.stringify(L.rows));
  assert(row("Gender").self_id && row("Gender").value === SELF, "self-ID shown in the local list");
  assert(row("Field of Study").flag === "closest", "closest match flagged");
  const res = L.files.filter((f) => f.kind === "resume");
  assert(res.some((f) => f.record_id === 4 && f.flag === "other_resume"), "the other application's resume is flagged " + JSON.stringify(L.files));
  assert(res.some((f) => f.record_id === 5 && f.how === "listed" && f.flag === "kept_file"), "the file the form kept is flagged " + JSON.stringify(L.files));
  // never on the feed, never in a post to the engine, never in the debug log
  for (const p of progress) assert(!JSON.stringify(p.activity || {}).includes(SELF), "self-ID on the feed");
  for (const [p, b] of posts) assert(!b.includes(SELF), "self-ID posted to " + p);
  // the engine hears Ready to send once, with the notice for Telegram
  const readyPosts = posts.filter(([p, b]) => p === "/api/autoapply/agent/ready" && !JSON.parse(b).clear);
  assert(readyPosts.length === 1, "one ready post " + readyPosts.length);
  const nb = JSON.parse(readyPosts[0][1]).notice;
  assert(nb && /^\d+(\.\d+)?$/.test(nb.run) && nb.filled === L.rows.length && nb.flagged === L.flagged, "notice on the ready post " + JSON.stringify(nb));
  assert(nb.lines.length === L.flagged && !nb.lines.some((x) => x.includes("555 0100")), "flagged lines only " + JSON.stringify(nb.lines));
  const log = fs.readFileSync(path.join(TMP, "sj-agent.log"), "utf8");
  assert(log.includes("(3 answers)"), "the log counts the fill's answers");
  assert(!log.includes(SELF) && !log.includes("Agricultural Economics"), "no answer value in the debug log");
  assert(!A.answersTelegram(L, "Analyst at Acme").includes(SELF), "no self-ID on Telegram");
  fs.rmSync(TMP, { recursive: true, force: true });
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
