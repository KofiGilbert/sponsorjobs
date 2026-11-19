// The agent decides (2026-11-18, docs/apply-agent.md section 29): the shell's part, without
// Electron (tests/test_agent_first.py runs it).
//   1. An answer the agent chose itself (the engine attached its proof) is recorded with its
//      source and evidence, shown in Review answers as one to check, and kept on the application.
//   2. What the engine refused goes back to the brain as an observation; an answer refused again
//      for the same question becomes needs-answers, so the question is left for the person and
//      asked ONCE at the end (the usual road).
//   3. In Auto (a quiet batch) nothing is asked: an unanswerable legal question sets the job
//      aside with a plain reason that names the question.
//   4. A stand-in ("N/A") is no longer refused by the shell: the engine's answer protocol decides.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "sj-agent-first-"));
process.env.TMPDIR = TMP;
process.env.SJ_AGENT_DEBUG = "1";
process.env.SJ_AGENT_RETRY_MS = "5";
process.env.SJ_AGENT_ASK_REREAD_MS = "0";
process.env.SJ_AX = "0";
const A = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };

const EDU_Q = "What is the highest level of education you have completed?";
const VISA_Q = "Will you now or in the future require visa sponsorship?";
const FACT = "edu1: Master of Science, 2018";
const VIEW = [
  "URL: https://jobs.brightwater.example/apply/1",
  `[3]<select label="${EDU_Q}" value="" options=5 first="Select One|High School|Bachelor's Degree|Master's Degree" required>`,
  `[4]<radio label="${VISA_Q}" option="Yes" checked=false required>`,
  `[5]<radio label="${VISA_Q}" option="No" checked=false required>`,
  '[6]<input type=text label="Team name" value="" required>',
  "[9]<button>Submit application</button>"].join("\n");

// ---- 1. the record ---------------------------------------------------------------------------
{
  const proof = { source: "resume", origin: "history", evidence: FACT, class: "factual" };
  const b = A.brainAnswer(VIEW, { op: "select", index: 3, option: "Master's Degree", proof }, {}, {});
  assert(b && b.label === EDU_Q && b.value === "Master's Degree", "the answer " + JSON.stringify(b));
  assert(b.from === "history" && b.agent === true && b.evidence === FACT && b.klass === "factual", "its proof rides along " + JSON.stringify(b));
  // without a proof (a saved answer's placeholder) nothing changes
  const p = A.brainAnswer(VIEW, { op: "type", index: 6, text: "Blue team", placeholder: "answer_1", sensitive: true }, { "Team name": "Blue team" }, {});
  assert(p.from === "you" && !p.agent, "the person's own answer stays theirs " + JSON.stringify(p));
  // a saved value picked on a list is a saved answer, never "the AI chose this"
  const sv = A.brainAnswer(VIEW, { op: "select", index: 3, option: "Bachelor's Degree", placeholder: "answer_2", sensitive: true, answer: true }, {}, {});
  assert(sv.from === "library" && !sv.agent, "a saved value on a list " + JSON.stringify(sv));
  // an origin the shell does not know is never taken on trust
  const odd = A.brainAnswer(VIEW, { op: "select", index: 3, option: "x", proof: { origin: "made up", evidence: "e" } }, {}, {});
  assert(odd.from === "ai" && !odd.agent, "an unknown origin is the AI's pick " + JSON.stringify(odd));

  const ans = A.createAnswers();
  A.addAnswer(ans, Object.assign({ page: 2, via: "brain" }, b));
  A.addAnswer(ans, { page: 2, label: "Phone Device Type", value: "Mobile", from: "common_sense", via: "brain", agent: true, evidence: "a personal phone is a mobile", klass: "common_sense" });
  A.addAnswer(ans, { page: 1, label: "City", value: "Chicago", from: "profile" });
  const L = A.readyAnswers(ans, { recordId: 7 });
  const row = (label) => L.rows.find((r) => r.label === label);
  assert(row(EDU_Q).from === "history" && row(EDU_Q).flag === "agent" && row(EDU_Q).evidence === FACT, "Review answers: from your resume, chosen by the agent " + JSON.stringify(row(EDU_Q)));
  assert(row("Phone Device Type").from === "common_sense" && row("Phone Device Type").flag === "agent", "common sense, chosen by the agent");
  assert(!row("City").flag && !row("City").agent, "the engine's own fill is not flagged");
  assert(L.flagged === 2, "both are lines to check " + L.flagged);
  // kept on the application: the question, the value, the source and the evidence
  const kept = A.agentAnswers(L);
  assert(kept.length === 2 && kept[0].label === EDU_Q && kept[0].source === "history" && kept[0].evidence === FACT && kept[0].class === "factual",
         "the application's record " + JSON.stringify(kept));
  // Telegram says the agent chose it; the notice lists it as a line to check
  const tg = A.answersTelegram(L, "Analyst at Brightwater Freight");
  assert(/your resume, chosen by the agent\) ⚠/.test(tg) && /common sense, chosen by the agent/.test(tg), "the Telegram lines\n" + tg);
  assert(A.readyNotice(L, 1).flagged === 2, "the notice counts them");
}

// ---- 2. counting refusals ----------------------------------------------------------------------
{
  const counts = new Map();
  let r = A.noteRejected(counts, [{ index: 6, label: "Team name", hard: "" }]);
  assert(!r.nudge.length && !r.force.length, "a first refusal: the observation is enough");
  r = A.noteRejected(counts, [{ index: 6, label: "Team name", hard: "" }]);
  assert(r.nudge.join() === "Team name" && !r.force.length, "a second: told to report needs-answers");
  r = A.noteRejected(counts, [{ index: 6, label: "Team name", hard: "" }]);
  assert(r.force.join() === "Team name", "a third: the shell reports it itself");
  // a hard limit is never worth a second try
  r = A.noteRejected(counts, [{ index: 5, label: VISA_Q, hard: "5" }]);
  assert(r.nudge.join() === VISA_Q, "a hard limit: told at once");
  r = A.noteRejected(counts, [{ index: 5, label: VISA_Q, hard: "5" }]);
  assert(r.force.join() === VISA_Q, "and forced the next time");
  const n = A.rejectNudge([VISA_Q, "Team name"]);
  assert(n.length <= 300 && /report needs-answers/.test(n) && /Team name/.test(n), "the nudge fits the step route " + n);
}

// ---- the loop, with a fake pane --------------------------------------------------------------
function setup(brain, hooks) {
  hooks = hooks || {};
  const P = { vals: {}, acts: [], log: [] };
  const val = (i) => (P.vals[i] ? "(filled)" : "");
  const view = () => ["URL: https://jobs.brightwater.example/apply/1",
    `[3]<select label="${EDU_Q}" value="${P.vals[3] || ""}" options=5 first="Select One|High School|Bachelor's Degree|Master's Degree" required>`,
    `[4]<radio label="${VISA_Q}" option="Yes" checked=${P.vals[4] === "Yes"} required>`,
    `[5]<radio label="${VISA_Q}" option="No" checked=${P.vals[4] === "No"} required>`,
    `[6]<input type=text label="Team name" value="${val(6)}">`,
    "[9]<button>Submit application</button>"].join("\n");
  P.left = () => [EDU_Q, VISA_Q].filter((q) => !(q === EDU_Q ? P.vals[3] : P.vals[4]));
  const url = () => "https://jobs.brightwater.example/apply/1";
  const wc = {
    getURL: url, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
    reload: () => {}, navigationHistory: { canGoBack: () => false, goBack: () => {}, getActiveIndex: () => 0, getEntryAtIndex: () => ({ url: url() }) },
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet.at")) return "5000|complete";
      if (expr.includes("pageSignature")) return { labels: ["q"] };
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("resumeInputOpen") || expr.includes("hasResumeUpload")) return false;
      if (expr.includes("pageShape")) return { fields: 4, buttons: 1, skeleton: false, words: 60 };
      if (expr.includes("TailorAgent.view(")) return { text: hooks.view ? hooks.view(P) : view() };
      if (expr.includes("TailorAgent.listOptions")) return hooks.listOptions ? hooks.listOptions(expr) : null;
      if (expr.includes("TailorAutofill.leftover()")) return P.left();
      if (expr.includes("selectOptions(")) return null;
      if (expr.includes("chosenIs(")) return true;
      if (expr.includes("popupOpen(")) return false;
      if (expr.includes("pageCheck")) return { required_empty: P.left(), errors: [] };
      if (expr.includes("innerText || el.value")) return /element\(9\)/.test(expr) ? "Submit application" : "";
      if (expr.includes("TailorAgent.act(")) {
        const a = JSON.parse(expr.slice(expr.indexOf("(") + 1, expr.lastIndexOf(")")));
        P.acts.push(a);
        if (hooks.act) { const r = hooks.act(P, a); if (r !== undefined) return r; }
        if (a.op === "select") P.vals[a.index] = a.option;
        if (a.op === "type") P.vals[a.index] = a.text;
        if (a.op === "click" && (a.index === 4 || a.index === 5)) P.vals[4] = a.index === 4 ? "Yes" : "No";
        return { ok: true };
      }
      return true;
    },
  };
  const posts = [], progress = [];
  const pane = { webContents: wc };
  const deps = {
    noPolling: true, getPane: () => pane, openInPane: () => {}, waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: P.left() }),
    sendProgress: (info) => { progress.push(JSON.parse(JSON.stringify(info))); },
    acquireDebugger: () => null, frames: {}, setQuiet: () => {},
    applyDecision: async () => (hooks.decision || { fill: true, click_submit: false }),
    isNewPage: () => true, attachResume: async () => false,
    getApp: async (p) => (p.startsWith("/api/autoapply/agent/parked") ? { ok: true, parked: [] } : { hosts: ["jobs.brightwater.example"] }),
    postApp: async (p, b) => {
      posts.push([p, JSON.parse(JSON.stringify(b || {}))]);
      if (p === "/api/autoapply/agent/step") return brain(P, b, posts.filter(([q]) => q === "/api/autoapply/agent/step").length);
      if (p === "/api/autoapply/agent/ask") {
        if (hooks.ask) return hooks.ask(P, b);
        if (b.known_only) return { ok: false, known: true, answers: {}, select: {}, open: (b.questions || []).map((q) => q.question) };
        return { ok: false, reason: "parked" };
      }
      if (p === "/api/autoapply/agent/critic") return { ok: true, issues: [] };
      return { ok: true };
    },
  };
  return { agent: A.createAgent(deps), posts, progress, P };
}
const ok = (actions, extra) => Object.assign({ ok: true, summary: "Answering the questions", actions, done: null, cost: 0.01 }, extra || {});
const READY = ok([], { done: { outcome: "ready_to_submit" } });
const steps = (posts) => posts.filter(([p]) => p === "/api/autoapply/agent/step").map(([, b]) => b);
const PROOF = { source: "resume", origin: "history", evidence: FACT, class: "factual" };
const REFUSED = { observations: ["Your click on [5] (" + VISA_Q.slice(0, 60) + ") was not done: hard limit 5 (legal and attestation questions are answered only from the person's saved answer): no saved answer is in <left> for this question."],
                  rejected: [{ index: 5, label: VISA_Q, hard: "5" }] };

(async () => {
  // ---- 3. the brain answers one itself, is refused on the legal one twice: asked once, at the end
  {
    const answers = { [VISA_Q]: "No" };
    const brain = (P, b, n) => {
      if (!P.vals[3]) return ok([{ op: "select", index: 3, option: "Master's Degree", proof: PROOF }], REFUSED);   // its own answer runs; the legal one is refused
      if (b.answers && b.answers[VISA_Q] && !P.vals[4]) return ok([{ op: "click", index: 5, placeholder: "answer_1" }]);
      if (!P.vals[4] && n <= 3) return ok([], REFUSED);                                 // it tries again: refused again
      return READY;
    };
    const s = setup(brain, { ask: (P, b) => (b.known_only ? { ok: false, known: true, answers: {}, select: {}, open: b.questions.map((q) => q.question) }
                                                         : { ok: true, answers }) });
    const r = await s.agent.run(7, { url: "https://jobs.brightwater.example/apply/1", company: "Brightwater Freight" });
    assert(r.ok && r.ready, "the run reaches Ready to send " + JSON.stringify(r));
    // the agent's own answer went on the page as it chose it
    assert(s.P.acts.some((a) => a.op === "select" && a.index === 3 && a.option === "Master's Degree"), "its own answer ran " + JSON.stringify(s.P.acts));
    // the refusal came back to the brain, with the hard limit named
    const bodies = steps(s.posts);
    assert((bodies[1].observations || []).some((o) => /hard limit 5/.test(o)), "the refusal is an observation on the next step " + JSON.stringify(bodies[1].observations));
    assert(bodies[1].nudges.some((n) => /Your own answer was refused/.test(n)), "and the brain is told to report needs-answers " + JSON.stringify(bodies[1].nudges));
    // refused again: the shell reported needs-answers itself; the question was asked ONCE, at the end
    const asks = s.posts.filter(([p, b]) => p === "/api/autoapply/agent/ask" && !b.known_only).map(([, b]) => b);
    assert(asks.length === 1 && asks[0].batch === true && asks[0].questions.length === 1 && asks[0].questions[0].question === VISA_Q,
           "ONE ask, for the legal question only " + JSON.stringify(asks));
    // Review answers: the agent's own answer with its evidence, the person's answer as theirs
    const ready = s.progress.find((x) => x.state === "ready");
    const edu = ready.answers.rows.find((x) => x.label === EDU_Q);
    assert(edu && edu.from === "history" && edu.flag === "agent" && edu.evidence === FACT, "Review answers shows where it came from " + JSON.stringify(edu));
    // and it is kept on the application
    const rp = s.posts.find(([p, b]) => p === "/api/autoapply/agent/ready" && Array.isArray(b.agent_answers));
    assert(rp && rp[1].agent_answers.length === 1 && rp[1].agent_answers[0].source === "history" && rp[1].agent_answers[0].evidence === FACT,
           "recorded on the application " + JSON.stringify(rp && rp[1].agent_answers));
    // the debug log never holds the answer or its evidence
    const logFile = path.join(TMP, "sj-agent.log");
    const log = fs.existsSync(logFile) ? fs.readFileSync(logFile, "utf8") : "";
    // (the page's own view is logged as before; the reply and act lines never carry the answer)
    const said = log.split("\n").filter((ln) => / (reply:|act |answers refused)/.test(ln)).join("\n");
    assert(said.length > 0 && !said.includes("Master's Degree") && !log.includes(FACT) && !log.includes("hard limit 5"),
           "no answer value, evidence or observation in the log\n" + said.slice(0, 600));
  }

  // ---- 4. Auto (a quiet batch): nothing is asked; the job is set aside and the question named
  {
    const brain = (P) => {
      if (!P.vals[3]) return ok([{ op: "select", index: 3, option: "Master's Degree", proof: PROOF }], REFUSED);
      return READY;                                  // it carries on to the Submit page with the legal one open
    };
    const s = setup(brain);
    const res = await s.agent.batch([{ recordId: 8, url: "https://jobs.brightwater.example/apply/1", company: "Brightwater Freight" }], { quiet: true });
    const parked = s.progress.filter((x) => x.state === "parked");
    const asks = s.posts.filter(([p, b]) => p === "/api/autoapply/agent/ask" && !b.known_only);
    const guided = s.progress.filter((x) => x.state === "ask");
    assert(!guided.length, "an Auto run shows no question");
    assert(asks.every(([, b]) => b.no_wait === true), "and never waits for an answer " + JSON.stringify(asks.map(([, b]) => b.no_wait)));
    assert(parked.length >= 1, "the job is set aside " + JSON.stringify({ res, states: s.progress.map((x) => x.state) }));
    const posted = s.posts.filter(([p]) => p === "/api/autoapply/agent/park").map(([, b]) => JSON.stringify(b)).join(" ") + " " + parked.map((x) => x.message || "").join(" ");
    assert(posted.includes(VISA_Q.slice(0, 40)), "and the reason names the question: " + posted.slice(0, 400));
    assert(!s.posts.some(([p, b]) => p === "/api/autoapply/agent/ready" && !b.clear) && !s.progress.some((x) => x.state === "ready"), "never left at Ready to send");
  }

  // ---- 5. a stand-in is the engine's call now: the shell types what the engine let through
  {
    assert(A.isFiller("N/A") && !A.isFiller("Mobile"), "the filler list is kept for the checks");
    const brain = (P) => {
      if (!P.vals[6]) return ok([{ op: "type", index: 6, text: "N/A", sensitive: false, proof: { source: "common_sense", origin: "common_sense", evidence: "the question asks for N/A when there is none", class: "common_sense" } },
                                 { op: "select", index: 3, option: "Master's Degree", proof: PROOF }, { op: "click", index: 5, placeholder: "answer_1" }]);
      return READY;
    };
    const s = setup(brain);
    const r = await s.agent.run(9, { url: "https://jobs.brightwater.example/apply/1", company: "Brightwater Freight" });
    assert(r.ok && r.ready, "ready " + JSON.stringify(r));
    assert(s.P.acts.some((a) => a.op === "type" && a.index === 6 && a.text === "N/A"), "an answer the protocol passed is typed " + JSON.stringify(s.P.acts));
  }
  // ---- 6. the brain's hands on a react-select: the page's own pick, never a real key that types nothing
  {
    const SCHOOL = "Harlow State";
    const cview = (P) => ["URL: https://jobs.brightwater.example/apply/1",
      `[14]<combobox label="School" value="${P.vals[14] || ""}" expanded=false required>`,
      `[16]<combobox label="Which type of employment are you seeking?" value="${P.vals[16] || ""}" expanded=${P.open16 ? "true" : "false"} required>`,
      ...(P.open16 ? ["*[39]<option>Full-time</option>", "*[40]<option>Part-time</option>"] : []),
      `[18]<combobox label="Team" value="${P.vals[18] || ""}" expanded=false>`,
      "[9]<button>Submit application</button>"].join("\n");
    const brain = (P, b, n) => {
      if (n === 1) return ok([{ op: "list_options", index: 16 }, { op: "type", index: 14, text: "Harlow State University", sensitive: false, proof: PROOF }]);
      if (n === 2) return ok([{ op: "click", index: 16 }]);
      if (n === 3) return ok([{ op: "click", index: 39, proof: { source: "posting", origin: "job", evidence: "post: employment type full-time", class: "common_sense" } }]);
      if (n === 4) return ok([{ op: "type", index: 18, text: "Blue", sensitive: false, proof: PROOF }]);
      return READY;
    };
    const s = setup(brain, {
      view: cview,
      listOptions: () => ({ options: ["Full-time", "Part-time"], partial: false }),
      act: (P, a) => {
        if (a.op === "type" && a.index === 14) { P.vals[14] = SCHOOL; return { ok: true, note: "picked", picked: SCHOOL, changed: true }; }
        if (a.op === "click" && a.index === 16) { P.open16 = true; return { ok: true, note: "", changed: true }; }
        if (a.op === "pick_option" && a.index === 39) { P.vals[16] = "Full-time"; P.open16 = false; return { ok: true, note: "picked", picked: "Full-time", changed: true }; }
        if (a.op === "type" && a.index === 18 && !a.plain) return { ok: false, note: "no list opened for this box", why: "no-list", changed: false };
        if (a.op === "type" && a.index === 18 && a.plain) { P.vals[18] = a.text; return { ok: true, note: "", changed: false }; }
        return undefined;
      },
    });
    s.P.left = () => ["School", "Which type of employment are you seeking?"].filter((q) => !(q === "School" ? s.P.vals[14] : s.P.vals[16]));
    const r = await s.agent.run(12, { url: "https://jobs.brightwater.example/apply/1", company: "Brightwater Freight" });
    assert(r.ok && r.ready, "ready " + JSON.stringify(r) + JSON.stringify(s.P.acts));
    const bodies = steps(s.posts);
    // list_options on a CLOSED combobox read its choices through the page's own read
    assert((bodies[1].observations || []).some((o) => /options for \[16\]: Full-time \| Part-time/.test(o)), "list_options read the closed list " + JSON.stringify(bodies[1].observations));
    // type on a combobox: ONE in-page pick, and the brain is told what was picked
    const typed = s.P.acts.filter((a) => a.index === 14);
    assert(typed.length === 1 && typed[0].op === "type" && !typed[0].plain, "one pick for the school " + JSON.stringify(typed));
    assert(bodies[1].history.some((h) => (h.results || []).some((x) => x === 'ok, picked "Harlow State"')), "the result names the pick " + JSON.stringify(bodies[1].history));
    // a click on an option: the page's pick_option, never a second click
    const on39 = s.P.acts.filter((a) => a.index === 39);
    assert(on39.length === 1 && on39[0].op === "pick_option", "the option was picked in the page " + JSON.stringify(on39));
    // a box that opened no list to the pick: typed the plain way, as before
    const on18 = s.P.acts.filter((a) => a.index === 18).map((a) => !!a.plain);
    assert(JSON.stringify(on18) === "[false,true]", "the pick first, then the plain type " + JSON.stringify(on18));
    // Review answers: the default cites the posting ("The job"), flagged as the agent's
    const rows = s.progress.find((x) => x.state === "ready").answers.rows;
    const emp = rows.find((x) => /type of employment/.test(x.label));
    assert(emp && emp.from === "job" && emp.flag === "agent" && emp.value === "Full-time" && /post: employment type/.test(emp.evidence), "the default is flagged " + JSON.stringify(emp));
    // Review answers holds what the list really took, not what was typed
    const sch = rows.find((x) => x.label === "School");
    assert(sch && sch.value === SCHOOL && sch.flag === "agent", "the picked value is the recorded one " + JSON.stringify(sch));
    // the log holds neither the pick nor the answer
    const log = fs.readFileSync(path.join(TMP, "sj-agent.log"), "utf8").split("\n").filter((ln) => / (reply:|act )/.test(ln)).join("\n");
    assert(!log.includes(SCHOOL) && log.includes("(picked)"), "the picked value is not logged");
  }

  // ---- 7. a written answer: recorded as drafted, and never sent by the agent alone
  {
    const ESSAY = "What interests you most about this opportunity?";
    const eview = (P) => ["URL: https://jobs.brightwater.example/apply/1",
      `[3]<textarea label="${ESSAY}" value="${P.vals[3] ? "(filled)" : ""}" required>`, "[9]<button>Submit application</button>"].join("\n");
    const run = async (drafted) => {
      const brain = (P) => (!P.vals[3]
        ? ok([{ op: "type", index: 3, text: "I enjoy turning data into decisions.", placeholder: "answer_1", sensitive: true }], drafted ? { origins: { [ESSAY]: "drafted" } } : {})
        : READY);
      const s = setup(brain, { view: eview, decision: { fill: true, click_submit: true } });
      s.P.left = () => (s.P.vals[3] ? [] : [ESSAY]);
      await s.agent.run(drafted ? 13 : 14, { url: "https://jobs.brightwater.example/apply/1", company: "Brightwater Freight", autonomous: true, lane: "auto" }).catch(() => null);
      return s;
    };
    const d = await run(true);
    const ready = d.progress.find((x) => x.state === "ready");
    assert(ready && !d.progress.some((x) => /^Sending/.test(x.message || "")), "a drafted answer waits at Ready to send " + JSON.stringify(d.progress.map((x) => x.state + ":" + (x.message || "").slice(0, 30))));
    const row = ready.answers.rows.find((x) => x.label === ESSAY);
    assert(row && row.from === "drafted" && row.flag === "drafted", "Review answers says drafted " + JSON.stringify(row));
    assert(ready.answers.flagged === 1, "and it is one to read");
    // the same application with the person's own saved answer is sent by the agent, as before
    const n = await run(false);
    assert(n.progress.some((x) => /^Sending/.test(x.message || "")), "without a draft the allowlisted Auto send goes on " + JSON.stringify(n.progress.map((x) => x.state + ":" + (x.message || "").slice(0, 30))));
  }
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
