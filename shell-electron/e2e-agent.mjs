/* End-to-end test of the apply agent ("Apply for me", docs/apply-agent.md), driving the REAL
 * Electron app against a mock application system (tests/fixtures/forms/mock_ats.html): a job
 * page, an account wall, three form pages, a review page, and the "received" page, all with no
 * URL change (like Workday).
 *
 *   node e2e-agent.mjs
 *
 * Runs the engine with the test model (RESUME_AGENT_FAKE_LLM=1, a scripted brain), in a
 * throwaway data folder, so nothing real is touched and nothing is sent anywhere. The dev app
 * must be closed first (one engine port, one CDP port). Asserts on what the window shows:
 *  1. one click on Apply for me ends on "Ready to send", with the mock having seen Apply,
 *     the sign-in, and every page, and the resume attached;
 *  2. Send presses the mock's Submit and the bar says Sent;
 *  3. a ?captcha=1 job stops at the review page with the captcha message and nothing submitted.
 * The seed also writes the LEGACY answer stores (app_prefs.json: US authorized, needs
 * sponsorship; one answers.db row) before the engine starts, so the run proves the applicant
 * brain's migration (docs/applicant-brain.md, Phase 1): the brain reports the migrated counts,
 * the legacy files stay, the agent never asks what the migrated prefs answer, and an answer
 * given on the bar is reused on the next application without asking.
 *  ... and (section 10) a ?phenom=1 job, Lam Research's Phenom form: attaching the resume opens a
 *  "Data Privacy Agreement" the person is asked to accept ("Just this one"), the four Phenom
 *  self-identification dropdowns are filled from the saved answers without asking, and the
 *  Ready to send summary names the accepted notice.
 *  ... and (section 12, the applicant brain's Phase 3) an address typed in the intake chat is
 *  remembered: a ?address=1 job's contact page fills City, State and the required ZIP code from
 *  memory, and the run never asks for them.
 *  ... and (section 13, Phase 4) a ?pay=1 job asks "What base pay are you targeting for this
 *  position?", a wording no rule knows: the saved salary fills it after one label-mapping call,
 *  and a second such job makes no call (the wording is cached).
 *  ... and (section 14, Phase 6, the memory page) Export is one zip holding the seeded salary and
 *  the chat diary; the salary forgotten on the memory page is gone everywhere, and the next
 *  base pay job asks for it.
 *  ... and (sections 15 and 16, 2026-10-31: asks no longer die after 5 minutes) the engine's ask
 *  wait is cut to SJ_AGENT_ASK_WAIT_S=20 seconds. Section 14's unanswered question PARKS ("Paused:
 *  needs you" on the row, "Answer and continue"); a ?phenom=1 job whose privacy agreement nobody
 *  answers parks on its page, and "Just this one" given afterwards carries it on from the same
 *  page to Ready to send. Then a 3-job BATCH: job 1 needs consent that is not given, job 2 has an
 *  unknown required question; both park without waiting, job 3 reaches Ready to send, ONE digest
 *  lists the parked jobs, and answering both re-runs each of them to Ready to send.
 *  The first Apply for me asks the standing choice on terms once, before the run (section 4).
 */
import { _electron as electron } from "playwright";
import { spawn, spawnSync } from "child_process";
import http from "http";
import fs from "fs";
import os from "os";
import path from "path";
import { fileURLToPath } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.join(HERE, "..");
const PY = process.env.TAILOR_PYTHON || "/Users/kofigilbert/code/resume-agent/.venv/bin/python";
const FIX_PORT = 57434;
const results = [];
const check = (name, pass, detail = "") => {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  - " + detail : ""}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- 1. the mock ATS, served from the fixtures folder ------------------------------------
const FIX = path.join(REPO, "tests", "fixtures", "forms");
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
const server = http.createServer((req, res) => {
  const f = path.join(FIX, path.basename(new URL(req.url, "http://x").pathname) || "mock_ats.html");
  if (!fs.existsSync(f)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { "Content-Type": TYPES[path.extname(f)] || "text/plain" });
  res.end(fs.readFileSync(f));
});
await new Promise((r) => server.listen(FIX_PORT, "127.0.0.1", r));
const JOB = `http://127.0.0.1:${FIX_PORT}/mock_ats.html`;

// ---- 2. a throwaway data folder with a profile, two queued jobs and a resume ---------------
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "sj-agent-"));
const seed = spawnSync(PY, ["-c", `
import os, sys, json, shutil
sys.path.insert(0, ${JSON.stringify(REPO)})
os.environ["RESUME_AGENT_DATA_DIR"] = ${JSON.stringify(DATA)}
from pathlib import Path
from ui.records import CVRecords
from intake.memory import ConversationMemory
data = Path(${JSON.stringify(DATA)}); (data / "cv_build").mkdir(parents=True, exist_ok=True)
db = str(data / "resume_agent.db")
prof = {"identity": {"name": "Kofi Test", "email": "kofi.test@example.com", "phone": "312-555-0100",
                     "location": "Chicago, IL"},
        "skills": ["SQL", "Python"], "experience": [{"title": "Analyst", "company": "Acme", "bullets": ["Did things"]}]}
ConversationMemory(db).save("default", prof, {"name": "Kofi Test"}, [])
# The legacy answer stores, as an older build left them: the applicant brain migrates them.
(data / "app_prefs.json").write_text(json.dumps({"work_authorized": "Yes", "work_auth_us": "Yes",
                                                 "needs_sponsorship": "Yes"}, indent=2), encoding="utf-8")
from autoapply.answers import AnswerLibrary
lib = AnswerLibrary(str(data / "answers.db"))
lib.save("How did you hear about us?", "LinkedIn", "you")
lib.close()
recs = CVRecords(db)
ids = []
for role, url in [("Business Intelligence Analyst", ${JSON.stringify(JOB)}),
                  ("Analyst (captcha)", ${JSON.stringify(JOB + "?captcha=1")}),
                  ("Analyst (emailed code)", ${JSON.stringify(JOB + "?verify=1")}),
                  ("Analyst (existing account)", ${JSON.stringify(JOB + "?signin=fail")}),
                  ("Analyst (asks a question)", ${JSON.stringify(JOB + "?mustanswer=1")}),
                  ("Analyst (answer remembered)", ${JSON.stringify(JOB + "?mustanswer=1&again=1")}),
                  ("Analyst (Phenom)", ${JSON.stringify(JOB + "?phenom=1")}),
                  ("Analyst (address)", ${JSON.stringify(JOB + "?address=1")}),
                  ("Analyst (base pay)", ${JSON.stringify(JOB + "?pay=1")}),
                  ("Analyst (base pay again)", ${JSON.stringify(JOB + "?pay=1&again=1")}),
                  ("Analyst (salary forgotten)", ${JSON.stringify(JOB + "?pay=1&again=2")}),
                  ("Analyst (nobody answers)", ${JSON.stringify(JOB + "?phenom=1&again=4")}),
                  ("Analyst (batch, consent)", ${JSON.stringify(JOB + "?phenom=1&again=5")}),
                  ("Analyst (batch, a new question)", ${JSON.stringify(JOB + "?pay=1&again=6")}),
                  ("Analyst (batch, plain)", ${JSON.stringify(JOB + "?again=7")})]:
    rid = recs.add(role, "Acme Corp", 70, "", jd_label=role,
                   data={"status": "approved", "fit": 85, "render_profile": prof,
                         "source_job": {"url": url, "source_id": "mock:" + role, "location": "Bloomington, IL"},
                         "coverage": {"ratio": 80, "present": ["SQL"], "missing": [], "missing_supported": []},
                         "screening": [{"question": "Earliest start?", "answer": "November"}]})
    ids.append(rid)
    pdf = data / "cv_build" / f"cv-{rid}.pdf"
    pdf.write_bytes(b"%PDF-1.4\\n1 0 obj<</Type/Catalog>>endobj\\ntrailer<</Root 1 0 R>>\\n%%EOF\\n")
recs.close()
print(json.dumps(ids))
`], { encoding: "utf8" });
if (seed.status !== 0) { console.error(seed.stderr); process.exit(1); }
const [RID, RID_CAPTCHA, RID_CODE, RID_RESET, RID_ASK, RID_AGAIN, RID_PHENOM, RID_ADDRESS, RID_PAY, RID_PAY2, RID_FORGOT,
       RID_PAUSE, RID_B1, RID_B2, RID_B3] = JSON.parse(seed.stdout.trim().split("\n").pop());
check("seeded fifteen queued jobs", RID > 0 && RID_AGAIN > 0 && RID_PHENOM > 0 && RID_ADDRESS > 0 && RID_PAY2 > 0 && RID_FORGOT > 0
      && RID_PAUSE > 0 && RID_B3 > 0, `ids ${RID}..${RID_B3}`);

// ---- 3. the engine on the test model, then the real app ---------------------------------
// The shell strips the test-model flags from any engine it spawns (real model only, CLAUDE.md
// section 13), so the test starts the engine itself and the shell reuses it on :57000.
// SJ_AGENT_ASK_WAIT_S: an ask waits 20 seconds here (an hour in the app), then the job parks.
const ASK_WAIT_S = 20;
const engineEnv = { ...process.env, RESUME_AGENT_DATA_DIR: DATA, RESUME_AGENT_DEV: "1", RESUME_AGENT_FAKE_LLM: "1",
                    RESUME_AGENT_FAKE_MAILBOX: "1", SJ_AGENT_ASK_WAIT_S: String(ASK_WAIT_S),
                    RESUME_AGENT_CRED_FILE: path.join(DATA, "credentials.env"), TAILOR_SERVER_ONLY: "1" };
const engine = spawn(PY, ["-m", "ui.app"], { cwd: REPO, env: engineEnv, stdio: ["ignore", "ignore", "pipe"] });
let engineErr = "";
engine.stderr.on("data", (d) => { engineErr += d.toString(); });
let up = false;
for (let i = 0; i < 60 && !up; i++) {
  try { up = (await fetch("http://127.0.0.1:57000/api/health").catch(() => null))?.ok || (await fetch("http://127.0.0.1:57000/").catch(() => null))?.ok; } catch { up = false; }
  if (!up) await sleep(1000);
}
check("engine up on the test model", up, up ? "" : engineErr.slice(-400));
const stopEngine = () => { try { engine.kill(); } catch {} };
process.on("exit", stopEngine);

// The applicant brain migrated the legacy answer stores at start-up (counts only), and kept them.
const brain = await fetch("http://127.0.0.1:57000/api/brain/status").then((r) => r.json()).catch(() => null);
const rep = (brain && brain.report) || {};
check("the brain migrated the legacy answers",
      !!(brain && brain.active && (rep["answers.db"] || {}).active >= 1 && (rep["app_prefs.json"] || {}).active >= 1),
      JSON.stringify({ active: brain && brain.active, report: rep }));
const prefsKept = fs.existsSync(path.join(DATA, "app_prefs.json"))
  && JSON.parse(fs.readFileSync(path.join(DATA, "app_prefs.json"), "utf8")).needs_sponsorship === "Yes";
check("the legacy files are still there", prefsKept && fs.existsSync(path.join(DATA, "answers.db"))
      && fs.readdirSync(DATA).some((f) => f.startsWith("brain-backup-")), fs.readdirSync(DATA).join(", "));

const app = await electron.launch({
  args: [HERE],
  cwd: HERE,
  env: { ...process.env, ELECTRON_RUN_AS_NODE: undefined, PATH: "/opt/homebrew/bin:" + process.env.PATH,
         TAILOR_PYTHON: PY, RESUME_AGENT_DATA_DIR: DATA, RESUME_AGENT_DEV: "1", RESUME_AGENT_FAKE_LLM: "1",
         RESUME_AGENT_CRED_FILE: path.join(DATA, "credentials.env"), SJ_AGENT_PARK_POLL_MS: "1500" },
});
const win = await app.firstWindow();
win.on("console", (m) => { if (/apply agent|agent/i.test(m.text())) console.log("  [app]", m.text().slice(0, 300)); });
await win.waitForLoadState("domcontentloaded");
await win.waitForFunction(() => !!document.querySelector(".sidebar"), null, { timeout: 90_000 });
check("app window opens", true, await win.title());

const paneEval = async (expr) => {
  // The pane is a separate CDP page target on 9223; evaluate there.
  const list = await fetch("http://127.0.0.1:9223/json/list").then((r) => r.json());
  const p = list.find((t) => t.type === "page" && !t.url.startsWith("http://127.0.0.1:57000"));
  if (!p) return null;
  const ws = new WebSocket(p.webSocketDebuggerUrl);
  return new Promise((resolve) => {
    ws.onopen = () => ws.send(JSON.stringify({ id: 1, method: "Runtime.evaluate", params: { expression: expr, returnByValue: true } }));
    ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id === 1) { ws.close(); resolve(d.result && d.result.result && d.result.result.value); } };
    ws.onerror = () => resolve(null);
  });
};
// Every progress event the shell sends, kept in the page (the bar shows only the latest).
await win.evaluate(() => { window.__agentLog = []; window.tailorShell.onAutoApplyProgress((i) => {
  if (i && i.agent) window.__agentLog.push({ s: i.state, r: i.record_id, m: i.message }); }); });
const agentLog = () => win.evaluate(() => window.__agentLog.slice());
const api = (url, body) => fetch("http://127.0.0.1:57000" + url, body === undefined ? { headers: { "X-Tailor-Extension": "1" } }
  : { method: "POST", headers: { "Content-Type": "application/json", "X-Tailor-Extension": "1" }, body: JSON.stringify(body) })
  .then((r) => r.json()).catch(() => null);
const rowOf = async (id) => {
  const b = await api("/api/autoapply/board");
  for (const t of Object.values((b && b.tiles) || {})) for (const it of t) if (it.id === id) return it;
  return null;
};
const barText = () => win.evaluate(() => { const b = document.querySelector("#bbFill"); return b && !b.hidden ? b.textContent.trim() : ""; });
const waitBar = async (re, ms) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { const t = await barText(); if (re.test(t)) return t; await sleep(500); }
  return await barText();
};

// ---- 4. one click: the whole application ------------------------------------------------
await win.click("[data-nav=autoapply]");
await win.waitForSelector(`.aa-row[data-id="${RID}"]`, { timeout: 30_000 });
const label = await win.locator(`.aa-row[data-id="${RID}"] [data-act]`).first().textContent();
check("the row button says Apply for me", /apply for me/i.test(label || ""), label);
await win.click(`.aa-row[data-id="${RID}"] [data-act]`);
// Answers every form asks are missing in the fresh data folder: the bar asks once first.
const gate = await waitBar(/before i apply/i, 30_000);
check("the bar asks for the form answers first", /before i apply/i.test(gate), gate);
await win.locator("#bbFill button", { hasText: /apply anyway/i }).first().click();
// The standing choice on terms a site requires: asked once, before the run, never made for the person.
const termsGate = await waitBar(/privacy notice or terms/i, 30_000);
check("the first run asks the standing choice on terms, before it starts", /should i accept those for you/i.test(termsGate), termsGate);
await win.locator("#bbFill button", { hasText: /no, ask me first/i }).first().click();
const ready = await waitBar(/ready to send|stopped|could not|needs you|have a look/i, 120_000);
check("the run ends on Ready to send", /ready to send/i.test(ready), ready);
const log1 = (await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null";
const l1 = JSON.parse(log1) || [];
check("the agent clicked Apply itself", l1.includes("apply"), log1);
check("the agent got past the sign-in wall", l1.includes("signin") || l1.includes("account-created"), log1);
const contact = l1.find((x) => String(x).startsWith("contact:")) || "";
check("the contact page was filled and the resume attached", /contact:4:cv/.test(contact), contact || "no contact line");
check("every page was walked to Review", l1.some((x) => String(x).startsWith("questions")) && l1.includes("disclosures"), log1);
check("nothing was submitted by the agent", !l1.includes("submitted"), log1);
const onReview = await paneEval("!!document.getElementById('submit')");
check("the pane shows the review page with the mock's Submit", onReview === true);

// ---- 5. Send: the shell presses Submit -------------------------------------------------------
await win.click("#bbFill .bb-send");
const sent = await waitBar(/sent|could not find/i, 30_000);
check("Send submits and the bar says Sent", /^sent/i.test(sent), sent);
const log2 = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("the mock received the submission", log2.includes("submitted"), JSON.stringify(log2));
await sleep(1500);
const tile = await win.evaluate(() => (document.querySelector("#view-autoapply")?.textContent || "").includes("Nothing sent yet"));
check("the Submitted tile moved off zero", tile === false);

// ---- 6. the captcha variant stops, and never submits -------------------------------------
await win.waitForSelector(`.aa-row[data-id="${RID_CAPTCHA}"]`, { timeout: 30_000 });
await win.click(`.aa-row[data-id="${RID_CAPTCHA}"] [data-act]`);
const stopped = await waitBar(/captcha|ready to send|stopped|have a look/i, 120_000);
check("the captcha run stops with the captcha message", /captcha/i.test(stopped), stopped);
const log3 = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("nothing submitted on the captcha run", !log3.includes("submitted"), JSON.stringify(log3));

// ---- 7. the site emails a code: the mailbox hands it over, the run carries on ---------------
// The engine runs a fake mailbox (RESUME_AGENT_FAKE_MAILBOX=1); the test plays the site's mail
// server, delivering the message while the agent is waiting on /api/inbox/await.
const deliver = (msg) => fetch("http://127.0.0.1:57000/api/inbox/dev/deliver", { method: "POST",
  headers: { "Content-Type": "application/json", "X-Tailor-Extension": "1" }, body: JSON.stringify(msg) }).then((r) => r.ok).catch(() => false);
await win.waitForSelector(`.aa-row[data-id="${RID_CODE}"]`, { timeout: 30_000 });
await win.click(`.aa-row[data-id="${RID_CODE}"] [data-act]`);
const waiting = await waitBar(/waiting for the site|ready to send|stopped|have a look|connect your email/i, 120_000);
check("the code run waits for the site's email", /waiting for the site/i.test(waiting), waiting);
const d1 = await deliver({ from: "Acme Careers <no-reply@acme-careers.test>", subject: "Acme Corp: your verification code",
                           body: "Your verification code is 482913. It expires in 10 minutes." });
check("the test delivered the code email", d1 === true);
const codeDone = await waitBar(/ready to send|stopped|have a look|did not arrive/i, 120_000);
check("the run typed the code and reached Ready to send", /ready to send/i.test(codeDone), codeDone);
const log4 = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("the mock accepted the emailed code", log4.includes("code-ok") && !log4.includes("code-wrong"), JSON.stringify(log4));

// ---- 8. the account already exists: forgot password, emailed reset link, new password ------
await win.waitForSelector(`.aa-row[data-id="${RID_RESET}"]`, { timeout: 30_000 });
await win.click(`.aa-row[data-id="${RID_RESET}"] [data-act]`);
const waiting2 = await waitBar(/waiting for the site|ready to send|stopped|have a look|connect your email/i, 120_000);
check("the existing-account run asks for a reset and waits for the email", /waiting for the site/i.test(waiting2), waiting2);
const d2 = await deliver({ from: "Acme Careers <no-reply@acme-careers.test>", subject: "Reset your Acme Corp password",
                           body: `Click here to reset your password: ${JOB}?reset=tok1\nIf you did not ask for this, ignore it.` });
check("the test delivered the reset email", d2 === true);
const resetDone = await waitBar(/ready to send|stopped|have a look|did not arrive/i, 150_000);
check("the run reset the password and reached Ready to send", /ready to send/i.test(resetDone), resetDone);
const log5 = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("the mock saw the reset link opened and the password saved", log5.includes("reset-link-opened") && log5.includes("password-reset"), JSON.stringify(log5));

// ---- 9. a required question nobody answered: the agent asks on the bar, carries on, remembers --
await win.waitForSelector(`.aa-row[data-id="${RID_ASK}"]`, { timeout: 30_000 });
await win.click(`.aa-row[data-id="${RID_ASK}"] [data-act]`);
// Work authorization and sponsorship came from the migrated prefs, so they are never asked;
// answer whatever does come, up to four questions, the way a person on the bar would.
let asked = await waitBar(/the application asks|ready to send|stopped|have a look|needs/i, 120_000);
check("the agent asks a question on the bar", /the application asks:/i.test(asked), asked);
let askedYears = false;
let askedWorkAuth = false;
for (let i = 0; i < 4 && /the application asks:/i.test(asked); i++) {
  if (/how many years/i.test(asked)) askedYears = true;
  // Only the question itself counts: the bar's lead-in quotes the question before it on the
  // form ("After 'Will you ... require visa sponsorship?', the application asks: ...").
  const q = asked.split(/the application asks:/i).pop() || "";
  if (/authori[sz]ed|sponsorship/i.test(q)) askedWorkAuth = true;
  const hasBox = await win.locator("#bbFill .bb-answer").count();
  if (hasBox) {
    await win.fill("#bbFill .bb-answer", /how many years/i.test(asked) ? "5" : "Yes");
    await win.click("#bbFill .bb-send");
  } else {
    const first = win.locator("#bbFill .bb-opt").first();
    const label = (await first.textContent()) || "";
    if (/sponsorship/i.test(asked)) { const no = win.locator("#bbFill .bb-opt", { hasText: /^No$/ }); if (await no.count()) await no.first().click(); else await first.click(); }
    else await first.click();
    void label;
  }
  await sleep(1500);
  asked = await waitBar(/the application asks|ready to send|stopped|have a look|needs/i, 120_000);
}
check("the years question was among them", askedYears, asked);
check("work authorization and sponsorship were never asked (they came from the migrated prefs)", !askedWorkAuth, asked);
const askDone = asked;
check("the run carries on with the answer and reaches Ready to send", /ready to send/i.test(askDone), askDone);
const log6 = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("the mock saw the answer typed in", log6.includes("questions:5"), JSON.stringify(log6));
const lib = await win.evaluate(() => fetch("/api/answers").then((r) => r.json()));
const saved = JSON.stringify(lib).includes("5");
check("the answer is saved for next time", saved, JSON.stringify(lib).slice(0, 200));

// ---- 10. the next application with the same required question: answered from memory ---------
await win.waitForSelector(`.aa-row[data-id="${RID_AGAIN}"]`, { timeout: 30_000 });
await win.click(`.aa-row[data-id="${RID_AGAIN}"] [data-act]`);
const again = await waitBar(/the application asks|ready to send|stopped|have a look|needs/i, 120_000);
const logAgain = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("the years answer is reused on the next application without asking",
      /ready to send/i.test(again) && !/how many years/i.test(again) && logAgain.includes("questions:5"),
      `${again} | ${JSON.stringify(logAgain)}`);

// ---- 11. Phenom (Lam Research, 2026-10-30): a privacy agreement to accept, saved EEO answers ----
// The answers every form asks, saved the way the card saves them; then the run must never ask
// Gender, Ethnicity, Disability or Veteran, must ask once about the Data Privacy Agreement, and
// must name it in the Ready to send summary.
const savedCard = await win.evaluate(() => fetch("/api/answers/basics", { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ fields: { gender: "male", hispanic: "no", race: "black", veteran: "not", disability: "no" } }) }).then((r) => r.json()));
check("the form answers card is saved", (savedCard.values || {}).gender === "male", JSON.stringify(savedCard.values || savedCard));
await win.waitForSelector(`.aa-row[data-id="${RID_PHENOM}"]`, { timeout: 30_000 });
await win.click(`.aa-row[data-id="${RID_PHENOM}"] [data-act]`);
const seen = [];
const watch = async (re, ms) => {
  const t0 = Date.now();
  let t = "";
  while (Date.now() - t0 < ms) {
    t = await barText();
    if (t && seen[seen.length - 1] !== t) seen.push(t);
    if (re.test(t)) return t;
    await sleep(400);
  }
  return t;
};
const termsAsk = await watch(/needs you to accept|ready to send|stopped|have a look|the application asks/i, 120_000);
check("the run asks to accept the Data Privacy Agreement", /Acme Corp needs you to accept its Data Privacy Agreement to apply\. Accept it\?/.test(termsAsk), termsAsk);
const once = win.locator("#bbFill .bb-opt", { hasText: /^Just this one$/ });
check("the bar offers Accept for all applications, Just this one, Stop",
  (await win.locator("#bbFill .bb-opt").allTextContents()).join("|") === "Accept for all applications|Just this one|Stop");
if (await once.count()) await once.first().click();
const phenomDone = await watch(/ready to send|stopped|have a look|could not|the application asks/i, 150_000);
check("the Phenom run reaches Ready to send", /ready to send/i.test(phenomDone), phenomDone);
check("Ready to send names the accepted notice", /Accepted Acme Corp's Data Privacy Agreement for you\./.test(phenomDone), phenomDone);
check("no self-identification question was asked", !seen.some((t) => /gender|ethnicity|veteran|disability/i.test(t)), JSON.stringify(seen));
const log7 = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("the mock saw the agreement acknowledged, never cancelled", log7.includes("privacy-ack") && !log7.includes("privacy-cancel"), JSON.stringify(log7));
check("all four Phenom dropdowns were filled", log7.includes("eeo:4"), JSON.stringify(log7));
check("nothing was submitted on the Phenom run", !log7.includes("submitted"), JSON.stringify(log7));
const pkg = await win.evaluate((id) => fetch(`/api/record/${id}`).then((r) => r.json()), RID_PHENOM);
const acc = (pkg.terms_accepted || []).map((x) => x.title + "|" + x.company);
check("the acceptance is recorded on the application", acc.length === 1 && acc[0] === "Data Privacy Agreement|Acme Corp", JSON.stringify(pkg.terms_accepted));

// ---- 12. the applicant brain, Phase 3: what the intake chat was told, a form never asks -------
// The person types their address in the resume builder's chat (the test model reads it the way
// the real one does). The ZIP code exists nowhere else: the seeded profile only says "Chicago, IL".
const post = (url, body) => win.evaluate(([u, b]) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(b) }).then((r) => r.status).catch(() => 0), [url, body]);
const st1 = await post("/api/session/start", { jd: "Business Intelligence Analyst at Acme Corp. SQL, Python and dashboards." });
const st2 = await post("/api/session/answer", { text: "My home address is 200 W Madison St, Chicago, IL 60606" });
check("the intake chat took the address", st1 === 200 && st2 > 0, `start ${st1}, answer ${st2}`);
await win.waitForSelector(`.aa-row[data-id="${RID_ADDRESS}"]`, { timeout: 30_000 });
seen.length = 0;
await win.click(`.aa-row[data-id="${RID_ADDRESS}"] [data-act]`);
const addrDone = await watch(/ready to send|stopped|have a look|could not|the application asks/i, 150_000);
check("the address run reaches Ready to send", /ready to send/i.test(addrDone), addrDone);
const log8 = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("the contact page fills city, state and ZIP from memory", log8.includes("address:Chicago|IL|60606"), JSON.stringify(log8));
check("a value typed in intake chat is not asked again on the form",
      !log8.includes("zip-required") && !seen.some((t) => /zip|postal|the application asks/i.test(t)), JSON.stringify(seen));
check("nothing was submitted on the address run", !log8.includes("submitted"), JSON.stringify(log8));

// ---- 13. the applicant brain, Phase 4: a question in new words still finds the saved answer ------
// "What base pay are you targeting for this position?" (required) matches no rule. The label
// mapper makes ONE model call for it (the test model counts calls in /api/brain/status), the
// saved salary fills it, and the next application with the same wording makes no call at all.
const mapperCalls = async () => ((await fetch("http://127.0.0.1:57000/api/brain/status").then((r) => r.json()).catch(() => null)) || {}).mapper || {};
const salCard = await win.evaluate(() => fetch("/api/answers/basics", { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ fields: { salary: "95000" } }) }).then((r) => r.json()));
check("the salary is saved on the form answers card", String((salCard.values || {}).salary || "") === "95000", JSON.stringify(salCard.values || salCard));
const calls0 = (await mapperCalls()).calls || 0;
await win.waitForSelector(`.aa-row[data-id="${RID_PAY}"]`, { timeout: 30_000 });
seen.length = 0;
await win.click(`.aa-row[data-id="${RID_PAY}"] [data-act]`);
const payDone = await watch(/ready to send|stopped|have a look|could not|the application asks/i, 150_000);
const calls1 = (await mapperCalls()).calls || 0;
const log9 = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("the base pay run reaches Ready to send without asking", /ready to send/i.test(payDone)
      && !seen.some((t) => /base pay|salary|the application asks/i.test(t)), JSON.stringify(seen));
check("the agent fills the saved salary after one mapping call", log9.includes("pay:95000") && calls1 - calls0 === 1,
      `calls ${calls0} -> ${calls1} | ${JSON.stringify(log9)}`);
await win.waitForSelector(`.aa-row[data-id="${RID_PAY2}"]`, { timeout: 30_000 });
seen.length = 0;
await win.click(`.aa-row[data-id="${RID_PAY2}"] [data-act]`);
const pay2Done = await watch(/ready to send|stopped|have a look|could not|the application asks/i, 150_000);
const calls2 = (await mapperCalls()).calls || 0;
const log10 = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("the second run makes no mapping call", /ready to send/i.test(pay2Done) && log10.includes("pay:95000") && calls2 === calls1,
      `calls ${calls1} -> ${calls2} | ${pay2Done} | ${JSON.stringify(log10)}`);
check("nothing was submitted on the base pay runs", !log9.includes("submitted") && !log10.includes("submitted"));

// ---- 14. the memory page, Phase 6: Export holds what the brain knows; Forget really forgets ------
// Export is one zip: memory.json with the salary saved in section 13, and the chat diary with the
// address typed in section 12. Then the salary is forgotten on the memory page itself (its row's
// Forget, then the in-row confirm), and the next application with the base pay question asks.
const zipCheck = spawnSync(PY, ["-c", `
import io, json, sys, urllib.request, zipfile
z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen("http://127.0.0.1:57000/api/memory/export.zip").read()))
names = z.namelist()
mem = z.read("memory.json").decode()
diary = "".join(z.read(n).decode() for n in names if n.startswith("diary/"))
print(json.dumps({"names": names, "salary": "95000" in mem, "diary": "60606" in diary}))
`], { encoding: "utf8" });
const zr = (() => { try { return JSON.parse(zipCheck.stdout.trim().split("\n").pop()); } catch { return {}; } })();
check("Export holds the seeded salary and the diary", zr.salary === true && zr.diary === true && (zr.names || []).includes("memory.json"),
      zipCheck.stdout.slice(-300) + zipCheck.stderr.slice(-300));
await win.click("#acctBtn");
await win.click("#menuProfile");
await win.waitForSelector('#memAnswerRows [data-id="key:@salary"]', { timeout: 30_000 });
check("the memory page shows the saved salary with where it came from",
      /\$95,000/.test(await win.locator('#memAnswerRows [data-id="key:@salary"]').textContent()));
await win.click('#memAnswerRows [data-id="key:@salary"] [data-mem="forget"]');
await win.click('#memAnswerRows [data-id="key:@salary"] [data-mem="forget-yes"]');
await win.waitForFunction(() => !document.querySelector('#memAnswerRows [data-id="key:@salary"]'), null, { timeout: 30_000 });
const afterForget = await win.evaluate(() => fetch("/api/answers/basics").then((r) => r.json()));
check("Forget clears the salary everywhere forms read it", !(afterForget.values || {}).salary, JSON.stringify(afterForget.values || afterForget));
await win.click("[data-nav=autoapply]");
await win.waitForSelector(`.aa-row[data-id="${RID_FORGOT}"]`, { timeout: 30_000 });
await win.click(`.aa-row[data-id="${RID_FORGOT}"] [data-act]`);
const forgotAsk = await waitBar(/the application asks|ready to send|stopped|have a look|needs/i, 150_000);
check("Forget the salary on the memory page, the next application asks for it",
      /the application asks/i.test(forgotAsk) && /base pay|salary/i.test(forgotAsk), forgotAsk);
const logForgot = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("the forgotten salary was not filled", !logForgot.includes("pay:95000") && !logForgot.includes("submitted"), JSON.stringify(logForgot));

// ---- 15. nobody answers: the job parks instead of dying, and an answer later carries it on ------
// Section 14's base pay question was never answered: past the (shortened) wait the job parks.
const parked14 = await waitBar(/paused/i, (ASK_WAIT_S + 40) * 1000);
check("an unanswered question parks the job instead of stopping it", /^paused: waiting on your answer/i.test(parked14), parked14);
const row14 = await rowOf(RID_FORGOT);
check("the row says Paused: needs you, with the question, and offers Answer and continue",
      row14 && row14.tile === "needs" && /^Paused: needs you\. The application asks: .*base pay/i.test(row14.stage_detail) && row14.action === "answer",
      JSON.stringify(row14 && { tile: row14.tile, detail: row14.stage_detail, action: row14.action }));
await win.click("[data-nav=autoapply]");
await win.waitForSelector(`.aa-row[data-id="${RID_PAUSE}"]`, { timeout: 30_000 });
await win.click(`.aa-row[data-id="${RID_PAUSE}"] [data-act]`);
const pauseAsk = await waitBar(/needs you to accept|ready to send|stopped|have a look/i, 120_000);
check("the nobody-answers run asks to accept the agreement", /needs you to accept its Data Privacy Agreement/.test(pauseAsk), pauseAsk);
const t0 = Date.now();
const pausedBar = await waitBar(/^paused/i, (ASK_WAIT_S + 40) * 1000);
check("nobody answers: the run parks after the wait", /^Paused: waiting on your answer\. Acme Corp needs you to accept its Data Privacy Agreement/.test(pausedBar)
      && Date.now() - t0 >= (ASK_WAIT_S - 5) * 1000, `${pausedBar} after ${Math.round((Date.now() - t0) / 1000)}s`);
const logP = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("nothing was accepted while nobody answered", !logP.includes("privacy-ack"), JSON.stringify(logP));
const parkedP = ((await api("/api/autoapply/agent/parked")) || {}).parked || [];
const pe = parkedP.find((e) => String(e.record_id) === String(RID_PAUSE));
check("the question stays open while the job is paused", pe && pe.state === "waiting" && pe.kind === "terms", JSON.stringify(pe));
check("the bar still offers the three choices", (await win.locator("#bbFill .bb-opt").allTextContents()).join("|") === "Accept for all applications|Just this one|Stop");
await win.locator("#bbFill .bb-opt", { hasText: /^Just this one$/ }).first().click();
const answeredBar = await waitBar(/i carry on|carrying on|ready to send/i, 15_000);
check("the late answer says the job carries on", /i carry on with the application|carrying on|ready to send/i.test(answeredBar), answeredBar);
const pauseDone = await waitBar(/ready to send|stopped|have a look|could not|the application asks|^paused/i, 150_000);
check("the paused run carries on from the same page and reaches Ready to send", /ready to send/i.test(pauseDone), pauseDone);
const logP2 = await agentLog();
check("it carried on where it paused (the job was not opened again)",
      logP2.some((x) => x.r === RID_PAUSE && x.m === "Thanks. Carrying on where I paused")
      && !logP2.some((x) => x.r === RID_PAUSE && x.m === "Thanks. Opening the job again"), JSON.stringify(logP2.filter((x) => x.r === RID_PAUSE)));
const logP3 = JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
check("the agreement was acknowledged after the answer", logP3.includes("privacy-ack") && !logP3.includes("submitted"), JSON.stringify(logP3));
const pkgP = await win.evaluate((id) => fetch(`/api/record/${id}`).then((r) => r.json()), RID_PAUSE);
check("Just this one is remembered on that application only", (pkgP.terms_consent || []).length === 1 && !pkgP.agent_park,
      JSON.stringify({ consent: pkgP.terms_consent, park: pkgP.agent_park }));
const basicsP = await api("/api/answers/basics");
check("the standing choice stays as the person set it", basicsP && basicsP.accept_required_terms === "ask" && basicsP.terms_decided === true,
      JSON.stringify(basicsP && { a: basicsP.accept_required_terms, d: basicsP.terms_decided }));

// ---- 16. a batch of three while the person is away: nothing waits, ONE digest, answers resume ---
const digest0 = (((await api("/api/autoapply/agent/digest")) || {}).count) || 0;
const jobs = [];
for (const id of [RID_B1, RID_B2, RID_B3]) {
  const it = await api(`/api/autoapply/item/${id}`);
  jobs.push({ recordId: id, url: it.apply_url, resumePath: it.cv_path || "", lane: it.lane || "assisted", autonomous: false, company: it.company || "" });
}
const tB = Date.now();
await win.evaluate((js) => { window.__batchR = null; window.tailorShell.agentBatch(js).then((r) => { window.__batchR = r; }); }, jobs);
let batchR = null;
for (let i = 0; i < 400 && !batchR; i++) { await sleep(750); batchR = await win.evaluate(() => window.__batchR); }
const byId = Object.fromEntries(((batchR && batchR.results) || []).map((r) => [r.record_id, r]));
check("the batch ran all three", batchR && batchR.ok && (batchR.results || []).length === 3, JSON.stringify(batchR));
check("job 1 (consent not given) and job 2 (a new required question) parked",
      byId[RID_B1] && byId[RID_B1].parked && byId[RID_B1].kind === "terms" && byId[RID_B2] && byId[RID_B2].parked && byId[RID_B2].kind === "questions",
      JSON.stringify(byId));
check("job 3 reached Ready to send", byId[RID_B3] && byId[RID_B3].ready === true, JSON.stringify(byId[RID_B3]));
check("the batch never waited on the person", Date.now() - tB < 2 * ASK_WAIT_S * 1000 + 150_000, `${Math.round((Date.now() - tB) / 1000)}s`);
const dg = (await api("/api/autoapply/agent/digest")) || {};
const dj = ((dg.last || {}).jobs || []).map(String);
check("ONE digest lists the parked jobs with their questions", dg.count === digest0 + 1 && dj.includes(String(RID_B1)) && dj.includes(String(RID_B2))
      && /Paused: \d+ applications need you\./.test((dg.last || {}).text || "") && /Data Privacy Agreement/.test(dg.last.text) && /base pay/i.test(dg.last.text),
      JSON.stringify({ count: dg.count, before: digest0, last: dg.last }));
const doneBar = await waitBar(/done with 3/i, 10_000);
check("the bar sums the batch up", /Done with 3 applications: 1 ready to send, 2 paused, waiting on you\./.test(doneBar), doneBar);
const r1 = await rowOf(RID_B1), r2 = await rowOf(RID_B2);
check("both parked rows say Paused with Answer and continue", r1 && r2 && r1.action === "answer" && r2.action === "answer"
      && /^Paused: needs you\./.test(r1.stage_detail) && /^Paused: needs you\./.test(r2.stage_detail),
      JSON.stringify([r1 && r1.stage_detail, r2 && r2.stage_detail]));
const pk = ((await api("/api/autoapply/agent/parked")) || {}).parked || [];
const askOf = (id) => ((pk.find((e) => String(e.record_id) === String(id)) || {}).ask_view || {}).id;
const a1 = await api("/api/autoapply/agent/answer", { id: askOf(RID_B1), answer: "Just this one", exact: true, index: 0 });
const a2 = await api("/api/autoapply/agent/answer", { id: askOf(RID_B2), answer: "95000", exact: true, index: 0 });
check("answering both says each carries on", a1 && a1.parked && a2 && a2.parked && /I carry on with the application/.test(a1.reply + a2.reply),
      JSON.stringify([a1, a2]));
const tR = Date.now();
let logR = [];
while (Date.now() - tR < 300_000) {
  logR = await agentLog();
  const ok1 = logR.some((x) => x.r === RID_B1 && x.s === "ready"), ok2 = logR.some((x) => x.r === RID_B2 && x.s === "ready");
  if (ok1 && ok2) break;
  await sleep(1000);
}
check("answering both re-runs each to Ready to send", logR.some((x) => x.r === RID_B1 && x.s === "ready") && logR.some((x) => x.r === RID_B2 && x.s === "ready"),
      JSON.stringify(logR.filter((x) => x.r === RID_B1 || x.r === RID_B2).slice(-12)));
check("each re-run opened its job again (the pane had moved on)",
      logR.some((x) => x.r === RID_B1 && x.m === "Thanks. Opening the job again") && logR.some((x) => x.r === RID_B2 && x.m === "Thanks. Opening the job again"));
const pk2 = ((await api("/api/autoapply/agent/parked")) || {}).parked || [];
check("the answered jobs left the paused list; the unanswered one stays", !pk2.some((e) => [String(RID_B1), String(RID_B2)].includes(String(e.record_id)))
      && pk2.some((e) => String(e.record_id) === String(RID_FORGOT) && e.state === "waiting"), JSON.stringify(pk2.map((e) => [e.record_id, e.state])));

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
await app.close();
stopEngine();
server.close();
fs.rmSync(DATA, { recursive: true, force: true });
process.exit(failed.length ? 1 : 0);
