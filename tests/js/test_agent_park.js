// agent.js parking logic without Electron (tests/test_agent_park.py runs it): the chunked ask,
// Stop during it, the poller re-running an answered parked job (from the same page when the
// pane is still on it, else from the job's link), and a batch with its one digest.
"use strict";
const path = require("path");
const { createAgent, reviewSig } = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };

function fakeWc(url) {
  const wc = {
    url, destroyed: false,
    getURL: () => wc.url, isDestroyed: () => false, on: () => {}, removeListener: () => {},
    session: null,
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet.at")) return "5000|complete";
      if (expr.includes("pageSignature")) return { success: true };
      return true;
    },
  };
  return wc;
}

function setup(extra) {
  const posts = [], progress = [], opened = [];
  const wc = fakeWc("https://jobs.example.com/apply?id=1");
  const pane = { webContents: wc };
  const answers = extra.answers || {};
  const deps = Object.assign({
    noPolling: true,
    getPane: () => pane,
    openInPane: (u) => { opened.push(u); wc.url = u; },
    waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: [] }),
    sendProgress: (info) => progress.push(info),
    acquireDebugger: () => null, frames: {},
    applyDecision: async (u) => ({ fill: !/byhand/.test(u) }),
    isNewPage: () => false, attachResume: async () => false,
    getApp: async (p) => (p.startsWith("/api/autoapply/agent/parked") ? { ok: true, parked: extra.parked || [] } : { hosts: [] }),
    postApp: async (p, b) => {
      posts.push([p, b]);
      if (answers[p]) return answers[p](b);
      return { ok: true };
    },
  }, extra.deps || {});
  const agent = createAgent(deps);
  return { agent, posts, progress, opened, wc };
}

(async () => {
  // 1. askLong: "waiting" chunks until the answer
  {
    let n = 0;
    const { agent, posts } = setup({ answers: {
      "/api/autoapply/agent/ask": () => ({ ok: false, reason: "waiting", id: "a1" }),
      "/api/autoapply/agent/ask/wait": () => (++n < 3 ? { ok: false, reason: "waiting", id: "a1" } : { ok: true, id: "a1", answers: { Q: "5" } }),
    } });
    const r = await agent._test.askLong("/api/autoapply/agent/ask", { id: "a1", questions: [] }, { stop: false });
    assert(r.ok && r.answers.Q === "5", "askLong answer " + JSON.stringify(r));
    assert(posts.filter(([p]) => p === "/api/autoapply/agent/ask/wait").length === 3, "three chunks");
    assert(posts[0][1].chunk_s === 20, "chunk size");
  }
  // 2. Stop during the wait closes the ask
  {
    const st = { stop: false };
    const { agent, posts } = setup({ answers: {
      "/api/autoapply/agent/ask": () => { st.stop = true; return { ok: false, reason: "waiting", id: "a2" }; },
    } });
    const r = await agent._test.askLong("/api/autoapply/agent/ask", { id: "a2" }, st);
    assert(r.reason === "stopped", "stopped");
    assert(posts.some(([p, b]) => p === "/api/autoapply/agent/ask/close" && b.id === "a2"), "closed");
  }
  // 3. an answered parked job, the pane moved on (or the app restarted): the job opens again
  {
    const entry = { record_id: "7", state: "answered", kind: "terms", decision: "accept", via: "once",
                    company: "Acme", title: "Privacy", page_url: "https://jobs.example.com/apply?id=7",
                    run: { url: "https://jobs.example.com/job/7", resume_path: "", lane: "assisted", autonomous: false, company: "Acme" } };
    const { agent, posts, progress, opened } = setup({ parked: [entry], answers: {
      "/api/autoapply/agent/parked/7/take": () => ({ ok: true, entry }),
    } });
    const r = await agent.pollParked();
    assert(r.ok && r.resumed === 1, "resumed " + JSON.stringify(r));
    assert(opened[0] === "https://jobs.example.com/job/7", "opened again " + JSON.stringify(opened));
    assert(progress.some((x) => x.record_id === 7 && x.message === "Thanks. Opening the job again"), "said so");
    assert(progress.some((x) => x.record_id === 7 && x.state === "submitted"), "ran to the end");
    assert(!posts.some(([p, b]) => p.endsWith("/take") && b.drop), "a resume never drops its own park");
  }
  // 4. the pane is still on the parked page: it carries on there, without opening anything
  {
    const entry = { record_id: "8", state: "answered", kind: "questions", answers: { "Your ID?": "A-1" },
                    page_url: "https://jobs.example.com/apply?id=1", run: { url: "https://jobs.example.com/job/8" } };
    const s = setup({ parked: [entry], answers: { "/api/autoapply/agent/parked/8/take": () => ({ ok: true, entry }) } });
    s.agent._test.parked.set("8", { epoch: s.agent._test.epoch(), pageUrl: "https://jobs.example.com/apply?id=1", kind: "questions",
                                    opts: { url: "https://jobs.example.com/job/8" }, state: null });
    await s.agent.pollParked();
    assert(s.opened.length === 0, "nothing opened " + JSON.stringify(s.opened));
    assert(s.progress.some((x) => x.record_id === 8 && x.message === "Thanks. Carrying on where I paused"), "same page");
  }
  // 5. a job still waiting is left alone, a declined one says so
  {
    const s = setup({ parked: [{ record_id: "9", state: "waiting" }, { record_id: "10", state: "declined", company: "Acme", title: "Terms" }],
                      answers: { "/api/autoapply/agent/parked/10/take": () => ({ ok: true, entry: { record_id: "10", state: "declined", company: "Acme", title: "Terms" } }) } });
    const r = await s.agent.pollParked();
    assert(r.resumed === 0 && !s.posts.some(([p]) => p.includes("/parked/9/")), "waiting untouched");
    assert(s.progress.some((x) => x.record_id === 10 && /You chose not to accept Acme's Terms/.test(x.message)), "declined said");
  }
  // 6. a batch runs every job, ONE digest at the end, and says how it went
  {
    const s = setup({ answers: { "/api/autoapply/agent/digest": () => ({ ok: true, sent: true }) } });
    const r = await s.agent.batch([{ recordId: 1, url: "https://jobs.example.com/byhand/1" },
                                   { recordId: 2, url: "https://jobs.example.com/job/2" }]);
    assert(r.ok && r.results.length === 2, "both ran");
    assert(s.posts.filter(([p]) => p === "/api/autoapply/agent/digest").length === 1, "one digest");
    const done = s.progress.find((x) => x.state === "batch-done");
    assert(done && done.message === "Done with 2 applications: 1 sent, 1 stopped.", "summary " + (done && done.message));
    assert(s.progress.some((x) => x.record_id === 2 && /^Application 2 of 2: Opening the job$/.test(x.message)), "numbered");
    // while a batch runs, a single Apply for me waits its turn
    const s2 = setup({});
    const p = s2.agent.batch([{ recordId: 3, url: "https://jobs.example.com/job/3" }]);
    const busy = await s2.agent.run(4, { url: "https://jobs.example.com/job/4" });
    assert(busy.reason === "busy", "busy during a batch");
    await p;
  }
  // 7. Send on a ready job whose page is gone: one click fills it again ("resend") and submits;
  //    while another run holds the pane it waits its turn
  {
    const s = setup({});
    const r = await s.agent.send(21, { url: "https://jobs.example.com/job/21", sig: "x" });
    assert(s.opened[0] === "https://jobs.example.com/job/21", "opened again " + JSON.stringify(s.opened));
    assert(s.progress.some((x) => x.record_id === 21 && x.message === "Filling it in again to send it"), "said so");
    assert(r && r.submitted, "submitted " + JSON.stringify(r));
    assert(s.posts.some(([p, b]) => p === "/api/autoapply/agent/ready" && b.clear && b.record_id === 21), "ready cleared");
    const none = await s.agent.send(22, {});
    assert(none.reason === "not-ready", "no job, no send");
  }
  // 8. the review page's fingerprint ignores element numbers, never the values
  assert(reviewSig("[3]<dd>Kofi</dd> [4]<button>Submit</button>") === reviewSig("[7]<dd>Kofi</dd>\n[9]<button>Submit</button>"), "same page");
  assert(reviewSig("[3]<dd>Kofi</dd>") !== reviewSig("[3]<dd>Kofi T</dd>"), "a changed value differs");
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
