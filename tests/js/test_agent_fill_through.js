// Fill-through (2026-11-17, the owner: "ours must never stop to ask questions" mid-application),
// under node with a fake pane and a fake brain (tests/test_agent_fill_through.py runs it):
//  1. a one-page form with 10 known fields and 3 unknown required questions (one with a neutral
//     choice, one free text, one legal fact): the 10 are filled, the neutral one takes "Prefer not
//     to say" (flagged), the other two are left for the person, the run reaches the Submit page,
//     and asks ONCE, as a batch, at the end; it never parks mid-form; nothing like "N/A" is typed;
//     Review answers flags the neutral pick and the optional box left empty;
//  2. a Workday-style page whose Next will not move on without the left question: everything
//     else on it is filled first, the Next is never held for the left question, and the ask comes
//     on that page only after the page refused, once, as a batch;
//  3. a saved answer that arrives while the run goes on (the setup, the library) is used: no ask.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "sj-ft-"));
process.env.TMPDIR = TMP;
process.env.SJ_AGENT_PANE_MS = "300";
process.env.SJ_AGENT_FILL_MS = "300";
process.env.SJ_AGENT_RETRY_MS = "20";
const A = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const { createAgent, DEFER_LINE, isFiller, deferNudge, errorNames, optionalEmpty, readyAnswers, readyNotice, answersTelegram } = A;
const assert = (c, m) => { if (!c) throw new Error(m); };

const NEUTRAL_Q = "Do you consider yourself a member of an underrepresented group?";
const TEXT_Q = "Describe a project you are proud of";
const LEGAL_Q = "Are you legally authorized to work in the United States?";
const KNOWN = ["First name", "Last name", "Email", "Phone", "City", "State", "ZIP code", "Country", "Current title", "School"];

// ---- a fake pane: the page is drawn from the state the actions change -----------------------
function fakePane(P, hooks) {
  const val = (i) => (P.vals[i] ? "(filled)" : "");
  const views = {
    form: () => ["Apply for Analyst", ...KNOWN.map((k, i) => `[${i + 1}]<input label="${k}" value="(filled)" required>`),
      `[21]<select label="${NEUTRAL_Q}" value="${val(21)}" options=3 first="Yes|No|Prefer not to say" required>`,
      `[22]<textarea label="${TEXT_Q}" value="${val(22)}" required>`,
      `[23]<radio label="${LEGAL_Q}" option="Yes" checked=${P.vals[23] === "Yes"} required>`,
      `[24]<radio label="${LEGAL_Q}" option="No" checked=${P.vals[23] === "No"} required>`,
      `[25]<input label="LinkedIn profile" value="${val(25)}">`,
      "[30]<button>Submit application</button>"].join("\n"),
    wd1: () => ["My Experience", "Step 2 of 3",
      ...(P.err ? [`Error: "The field ${TEXT_Q} is required"`] : []),
      '[7]<input label="Phone" value="(filled)" required>',
      `[9]<input label="Postal code" value="${val(9)}" required>`,
      `[22]<textarea label="${TEXT_Q}" value="${val(22)}" required>`,
      "[8]<button>Next</button>"].join("\n"),
    review: () => "Review\nStep 3 of 3\n[40]<button>Submit</button>",
  };
  const url = () => "https://acme.wd5.myworkdayjobs.com/apply/" + P.at;
  const wc = {
    getURL: url, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
    reload: () => {}, navigationHistory: { canGoBack: () => false, goBack: () => {}, getActiveIndex: () => 0, getEntryAtIndex: () => ({ url: url() }) },
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet.at")) return "5000|complete";
      if (expr.includes("pageSignature")) return { labels: [P.at] };
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("resumeInputOpen") || expr.includes("hasResumeUpload")) return false;
      if (expr.includes("pageShape")) return { fields: 6, buttons: 2, skeleton: false, words: 80 };
      if (expr.includes("TailorAgent.view(")) return { text: "URL: " + url() + "\n" + views[P.at]() };
      if (expr.includes("TailorAutofill.leftover()")) return P.left();
      if (expr.includes("selectOptions(21)")) return { options: ["Select...", "Yes", "No", "Prefer not to say"], partial: false };
      if (expr.includes("selectOptions(")) return null;
      if (expr.includes("chosenIs(")) return true;
      if (expr.includes("popupOpen(")) return false;
      if (expr.includes("pageCheck")) { P.checks++; return { required_empty: P.left(), errors: [] }; }
      if (expr.includes("innerText || el.value")) {
        const m = /element\((\d+)\)/.exec(expr);
        return { 8: "Next", 30: "Submit application", 40: "Submit" }[m && m[1]] || "";
      }
      if (expr.includes("TailorAgent.act(")) {
        const a = JSON.parse(expr.slice(expr.indexOf("(") + 1, expr.lastIndexOf(")")));
        P.acts.push(a);
        P.log.push("act " + a.op + " " + (a.index || "") + (a.text ? " " + a.text : "") + (a.option ? " " + a.option : ""));
        if (a.op === "select") P.vals[a.index] = a.option;
        if (a.op === "type") P.vals[a.index] = a.text;
        if (a.op === "click" && (a.index === 23 || a.index === 24)) P.vals[23] = a.index === 23 ? "Yes" : "No";
        if (hooks.onAct) hooks.onAct(P, a);
        return { ok: true };
      }
      return true;
    },
  };
  return { webContents: wc };
}

function setup(start, brain, hooks) {
  hooks = hooks || {};
  const P = { at: start, vals: {}, acts: [], log: [], checks: 0, err: false, left: () => [] };
  const pane = fakePane(P, hooks);
  const posts = [], progress = [];
  const deps = {
    noPolling: true,
    getPane: () => pane,
    openInPane: () => {},
    waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => (hooks.fill ? hooks.fill(P) : { ok: true, filled: 0, left: P.left() }),
    sendProgress: (info) => { progress.push(JSON.parse(JSON.stringify(info))); P.log.push("progress " + info.state + " " + (info.message || "")); },
    acquireDebugger: () => null, frames: {},
    applyDecision: async () => ({ fill: true, click_submit: false }),
    isNewPage: () => true, attachResume: async () => false,
    getApp: async (p) => (p.startsWith("/api/autoapply/agent/parked") ? { ok: true, parked: [] } : { hosts: ["acme.wd5.myworkdayjobs.com"] }),
    postApp: async (p, b) => {
      posts.push([p, JSON.parse(JSON.stringify(b || {}))]);
      if (p === "/api/autoapply/agent/step") { P.log.push("step " + b.step); return brain(P, b); }
      if (p === "/api/autoapply/agent/ask") {
        P.log.push("ask " + (b.known_only ? "known" : "batch=" + !!b.batch + " n=" + (b.questions || []).length));
        if (hooks.ask) return hooks.ask(P, b);
      }
      if (p === "/api/autoapply/agent/critic") return { ok: true, issues: [] };
      return { ok: true };
    },
  };
  return { agent: createAgent(deps), posts, progress, P };
}
const ok = (actions, extra) => Object.assign({ ok: true, summary: "Working on it", actions, done: null, cost: 0.01 }, extra || {});
const READY = ok([], { done: { outcome: "ready_to_submit" } });
const NEEDS = ok([], { done: { outcome: "blocked", reason: "needs-answers" } });
const stepBodies = (posts) => posts.filter(([p]) => p === "/api/autoapply/agent/step").map(([, b]) => b);
const realAsks = (posts) => posts.filter(([p, b]) => p === "/api/autoapply/agent/ask" && !b.known_only).map(([, b]) => b);

// The ask route as the engine answers it (autoapply/asks.py neutral_option, is_fact_question):
// the neutral question takes its "Prefer not to say"; the free text and the legal fact stay open.
function askRoute(answers) {
  return (P, b) => {
    if (b.known_only) {
      const out = { ok: false, answers: {}, origins: {}, select: {}, open: [] };
      for (const it of b.questions || []) {
        if (b.fill_through && it.question === NEUTRAL_Q && it.all_options.includes("Prefer not to say")) {
          out.answers[it.question] = "Prefer not to say"; out.origins[it.question] = "neutral"; out.select[it.question] = "Prefer not to say";
        } else if (!b.fill_through && answers.late && answers.late[it.question]) {
          out.answers[it.question] = answers.late[it.question]; out.origins[it.question] = "library";
        } else out.open.push(it.question);
      }
      return out;
    }
    // the ONE batch: the person answers everything on the bar
    const got = {};
    for (const it of b.questions || []) got[it.question] = answers.person[it.question];
    return { ok: true, id: b.id, answers: got };
  };
}

(async () => {
  // ---- pure helpers ------------------------------------------------------------------------
  for (const t of ["N/A", "n/a", "NA", "Not provided", "not applicable", "In", "TBD", "-", "...", "None", "unknown"]) assert(isFiller(t), "filler: " + t);
  for (const t of ["Indeed", "Chicago, IL", "No", "Yes", "Internship", "I led a migration of our billing system"]) assert(!isFiller(t), "not filler: " + t);
  assert(deferNudge([{ question: TEXT_Q }, { question: LEGAL_Q }]).length <= 300, "the nudge fits the step route's 300 characters");
  assert(/never N\/A/.test(deferNudge([{ question: TEXT_Q }])), "the nudge forbids a stand-in");
  assert(errorNames(`Error: "The field ${TEXT_Q} is required"\n[22]<textarea label="${TEXT_Q}" value="" required>`, TEXT_Q), "the page's error names it");
  assert(!errorNames(`[22]<textarea label="${TEXT_Q}" value="" required>`, TEXT_Q), "no error, no block");
  const opt = optionalEmpty(`[1]<input label="LinkedIn profile" value="">\n[2]<input label="ZIP" value="" required>\n[3]<input type=search label="Search" value="" search=true>\n[4]<input label="Website" value="(filled)">\n[5]<input label="Twitter ✱" value="">`);
  assert(JSON.stringify(opt) === JSON.stringify(["LinkedIn profile"]), "only optional empty boxes " + JSON.stringify(opt));

  // ---- 1. one page, 10 known, 3 unknown required: fill through, ONE batch at the end ---------
  {
    const answers = { person: { [TEXT_Q]: "I rebuilt our billing pipeline", [LEGAL_Q]: "Yes" } };
    const brain = (P, b) => {
      const n = stepBodies(brainPosts).length;
      if (n === 1) return NEEDS;                                           // the planner left three open
      if (b.answers && b.answers[TEXT_Q] && !P.vals[22]) {
        // the person's answers came back as placeholders: fill them. The brain also tried a stand-in
        // ("N/A") on the optional box: since 2026-11-18 the ENGINE refuses that (the answer protocol,
        // autoapply/protocol.py; tests/test_agent_first.py) and says why, where the shell used to.
        return ok([{ op: "type", index: 22, text: b.answers[TEXT_Q], placeholder: "answer_1" },
                   { op: "click", index: 23 }],
                  { observations: ["Your type on [25] (LinkedIn profile) was not done: a stand-in such as N/A is an answer only where the question asks for it."],
                    rejected: [{ index: 25, label: "LinkedIn profile", hard: "" }] });
      }
      return READY;                                                        // the rest is filled: at Submit
    };
    let brainPosts = [];
    const s = setup("form", brain, {
      fill: (P) => ({ ok: true, filled: 10, left: P.left(),
                      answers: KNOWN.map((k) => ({ label: k, value: k === "Email" ? "kofi@example.com" : "v-" + k, from: "profile" })) }),
      ask: askRoute(answers),
    });
    brainPosts = s.posts;
    s.P.left = () => [NEUTRAL_Q, TEXT_Q, LEGAL_Q].filter((q) => !(q === NEUTRAL_Q ? s.P.vals[21] : q === TEXT_Q ? s.P.vals[22] : s.P.vals[23]));
    const r = await s.agent.run(11, { url: "https://acme.wd5.myworkdayjobs.com/apply/form", company: "Acme" });
    assert(r.ok && r.ready, "the run reaches Ready to send " + JSON.stringify(r) + "\n" + s.P.log.join("\n"));
    assert(!s.progress.some((x) => x.state === "parked"), "never parked");
    // the neutral pick: by the shell, on the page's own choice
    assert(s.P.acts.some((a) => a.op === "select" && a.index === 21 && a.option === "Prefer not to say"), "picked the neutral choice " + JSON.stringify(s.P.acts));
    // one ask, a batch of the two the person must answer, AFTER the brain reached the Submit page
    const asks = realAsks(s.posts);
    assert(asks.length === 1 && asks[0].batch === true, "ONE batch ask " + JSON.stringify(asks));
    assert(asks[0].questions.map((q) => q.question).sort().join("|") === [LEGAL_Q, TEXT_Q].sort().join("|"), "both left questions, nothing else " + JSON.stringify(asks[0].questions) + "\n" + s.P.log.join("\n"));
    const askAt = s.P.log.findIndex((l) => /^ask batch=true/.test(l));
    const readyStep = s.P.log.findIndex((l) => l === "step 2");
    assert(readyStep >= 0 && askAt > readyStep, "the ask came after the brain reported the Submit page\n" + s.P.log.join("\n"));
    // (2026-11-17, the guided ask) No question list ever goes on the bar: the person answers on
    // the page, one outlined field at a time (tests/js/test_agent_guide.js). Here the engine has
    // the answers at once, so nothing is shown at all.
    assert(!s.progress.some((x) => x.state === "ask"), "no question list on the bar");
    // the bar said so while it filled
    assert(s.progress.some((x) => x.state === "running" && x.message === DEFER_LINE && x.deferred === 2), "the honest line " + JSON.stringify(s.progress.map((x) => x.message)));
    // the brain was told: the step after the defer carries the two, and its nudge
    const b2 = stepBodies(s.posts)[1];
    assert(b2.deferred.length === 2 && b2.nudges.some((n) => /^Left for the person, asked at the end/.test(n)), "the brain hears what is left " + JSON.stringify(b2));
    // no placeholder text ever typed, and the brain heard why on its next step
    assert(!s.P.acts.some((a) => a.op === "type" && isFiller(a.text)), "no stand-in typed " + JSON.stringify(s.P.acts));
    assert(stepBodies(s.posts).some((b) => (b.observations || []).some((o) => /LinkedIn profile.*was not done/.test(o))),
           "the refusal went back to the brain as an observation");
    // Review answers: 10 known, the neutral pick flagged, the optional box left empty
    const ready = s.progress.find((x) => x.state === "ready");
    const rows = ready.answers.rows;
    for (const k of KNOWN) assert(rows.some((x) => x.label === k && x.from === "profile"), "known row " + k);
    const neu = rows.find((x) => x.label === NEUTRAL_Q);
    assert(neu && neu.flag === "neutral" && neu.value === "Prefer not to say", "the neutral pick is flagged " + JSON.stringify(neu));
    const li = rows.find((x) => x.label === "LinkedIn profile");
    assert(li && li.flag === "left_empty" && li.value === "", "the optional box left empty " + JSON.stringify(li));
    assert(rows.some((x) => x.label === TEXT_Q && x.from === "you"), "the person's answer " + JSON.stringify(rows));
    assert(!rows.some((x) => x.flag === "needs_you"), "nothing still needs them");
    assert(ready.answers.flagged === 1, "left empty is shown, not counted " + ready.answers.flagged);
  }

  // ---- 2. a Workday page that will not move on without the left question -----------------------
  {
    const answers = { person: { [TEXT_Q]: "I rebuilt our billing pipeline" } };
    const brain = (P, b) => {
      if (P.at === "review") return READY;
      if (b.answers && b.answers[TEXT_Q]) return ok([{ op: "type", index: 22, text: b.answers[TEXT_Q], placeholder: "answer_1" }, { op: "click", index: 8 }]);
      if (!b.deferred || !b.deferred.length) return NEEDS;                 // the planner left it open
      if (!P.vals[9]) return ok([{ op: "type", index: 9, text: "60601", placeholder: "postal_code" }, { op: "click", index: 8 }]);
      return NEEDS;                                                         // the page refused to move on
    };
    const s = setup("wd1", brain, {
      fill: (P) => ({ ok: true, filled: 1, left: P.left(), answers: [{ label: "Phone", value: "555", from: "profile" }] }),
      ask: askRoute(answers),
      onAct: (P, a) => {
        if (a.op === "click" && a.index === 8) {
          if (P.vals[22]) { P.at = "review"; P.err = false; } else P.err = true;
        }
      },
    });
    s.P.left = () => (s.P.at === "wd1" ? [TEXT_Q].concat(s.P.vals[9] ? [] : ["Postal code"]).filter((q) => q !== TEXT_Q || !s.P.vals[22]) : []);
    const r = await s.agent.run(12, { url: "https://acme.wd5.myworkdayjobs.com/apply/wd1", company: "Acme" });
    assert(r.ok && r.ready, "ready after the one page ask " + JSON.stringify(r) + "\n" + s.P.log.join("\n"));
    const asks = realAsks(s.posts);
    assert(asks.length === 1 && asks[0].batch && asks[0].questions.length === 1 && asks[0].questions[0].question === TEXT_Q, "one batch on that page " + JSON.stringify(asks));
    const L = s.P.log;
    const typedZip = L.findIndex((l) => /^act type 9 60601/.test(l));
    const firstNext = L.findIndex((l) => /^act click 8/.test(l));
    const askAt = L.findIndex((l) => /^ask batch=true/.test(l));
    assert(typedZip >= 0 && typedZip < firstNext && firstNext < askAt, "everything else filled, Next tried, THEN asked\n" + L.join("\n"));
    assert(!stepBodies(s.posts).some((b) => (b.observations || []).some((o) => /^Page check before Next/.test(o))), "the left question never held the Next");
    assert(!s.progress.some((x) => x.state === "ask"), "no question list on the bar");
  }

  // ---- 3. a saved answer that arrives meanwhile is used: no ask at all -------------------------
  {
    const answers = { person: {}, late: { [TEXT_Q]: "From my saved answers" } };
    let n = 0;
    const brain = (P, b) => {
      n++;
      if (n === 1) return NEEDS;
      if (b.answers && b.answers[TEXT_Q] && !P.vals[22]) return ok([{ op: "type", index: 22, text: b.answers[TEXT_Q], placeholder: "answer_1" }]);
      return READY;
    };
    const s = setup("form", brain, { ask: askRoute(answers),
      fill: (P) => ({ ok: true, filled: 10, left: P.left(), answers: [] }) });
    s.P.vals[21] = "Yes"; s.P.vals[23] = "Yes";
    s.P.left = () => (s.P.vals[22] ? [] : [TEXT_Q]);
    const r = await s.agent.run(13, { url: "https://acme.wd5.myworkdayjobs.com/apply/form", company: "Acme" });
    assert(r.ok && r.ready, "ready " + JSON.stringify(r) + "\n" + s.P.log.join("\n"));
    assert(realAsks(s.posts).length === 0, "no ask: the saved answer arrived " + JSON.stringify(realAsks(s.posts)));
    assert(s.P.acts.some((a) => a.op === "type" && a.index === 22 && a.text === "From my saved answers"), "filled from it");
  }

  // ---- 4. Review answers and the notice on what was left ----------------------------------------
  {
    const ans = { rows: [{ page: 1, label: "Gender", value: "Male", from: "eeo", via: "fill" },
                         { page: 1, label: NEUTRAL_Q, value: "Prefer not to say", from: "neutral", via: "fill" }],
                  files: [], needs: [{ page: 2, label: TEXT_Q }], optional: { 1: ["LinkedIn profile", "Gender"] } };
    const list = readyAnswers(ans, { recordId: 5 });
    const need = list.rows.find((x) => x.label === TEXT_Q);
    assert(need && need.flag === "needs_you" && need.value === "", "needs you " + JSON.stringify(list.rows));
    assert(list.rows.filter((x) => x.label === "Gender").length === 1, "a filled box is never also left empty");
    assert(list.flagged === 2, "neutral + needs you " + list.flagged);
    const tg = answersTelegram(list, "Analyst at Acme");
    assert(!/Male/.test(tg) && !/LinkedIn profile/.test(tg) && /needs you/.test(tg), "telegram: no self-identification value, no optional empties " + tg);
    const note = readyNotice(list, 1);
    assert(note.filled === 2 && note.lines.length === 2 && !note.lines.some((l) => /LinkedIn/.test(l)), "the notice " + JSON.stringify(note));
  }
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); }).finally(() => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* temp */ } });
