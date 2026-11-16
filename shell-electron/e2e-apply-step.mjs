/* End-to-end test of Apply after tailoring (Kofi, 2026-11-16: "that is only step one, what about
 * the applying for the job part?"), driving the REAL Electron app against the mock application
 * system (tests/fixtures/forms/mock_ats.html), the way e2e-agent.mjs does.
 *
 *   node e2e-apply-step.mjs            (SJ_E2E_SHOTS=<dir> also saves the design screenshots)
 *
 * Runs its own engine on the test model (RESUME_AGENT_FAKE_LLM=1, a scripted brain) in a throwaway
 * data folder, on its OWN ports (engine 57020, CDP 9243, its own Electron profile), so it runs
 * beside the installed app (57000/9223) and the owner's dev window (57010/9233) without touching
 * either. Nothing real is touched and nothing is sent anywhere. Asserts on what the window shows:
 *  1. a resume chat started from a job (start_job: the posting and its link are known) builds a
 *     one-page resume, and the review footer's one primary is Apply (Accept & Save is ghost,
 *     Download PDF stays);
 *  2. Apply saves the resume (the record and its PDF), makes the Auto-apply package (Waiting on
 *     you, the Tracker's Approved column), switches the pane to Browser and starts Apply for me;
 *  3. the run is told in the chat (Applying to <role> at <company>, its steps) and on the bar;
 *     the agent clicks Apply on the mock, gets past the sign-in, fills every page, attaches the
 *     tailored resume, and stops at Submit with Send; nothing is submitted by the agent;
 *  4. Send presses the mock's Submit, the record is applied and the Tracker card moves to Applied;
 *  5. a pasted description with no link: Apply asks once in the chat for the link, and the link
 *     typed in the composer starts the run.
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
const PORT = Number(process.env.SJ_E2E_PORT) || 57020;
const CDP = Number(process.env.SJ_E2E_CDP) || 9243;
const FIX_PORT = 57454;
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SJ_E2E_SHOTS || "";
const results = [];
const check = (name, pass, detail = "") => {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  - " + detail : ""}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
for (const p of [PORT, CDP]) {
  const busy = await fetch(`http://127.0.0.1:${p}/`).then(() => true).catch(() => false);
  if (busy) { console.error(`port ${p} is in use; set SJ_E2E_PORT / SJ_E2E_CDP`); process.exit(2); }
}

// ---- 1. the mock ATS ----------------------------------------------------------------------------
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
const SID = "mock:bi-analyst";

// ---- 2. a throwaway data folder: a saved profile and one saved job whose link is the mock -------
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "sj-applystep-"));
const JD = "Business Intelligence Analyst at Acme Corp. Bloomington, IL. Build SQL and Python dashboards, "
  + "statistics, Tableau and Excel reporting for the sales team. Required: SQL, Python, Tableau, Excel.";
const seed = spawnSync(PY, ["-c", `
import os, sys, json
sys.path.insert(0, ${JSON.stringify(REPO)})
os.environ["RESUME_AGENT_DATA_DIR"] = ${JSON.stringify(DATA)}
from pathlib import Path
from intake.memory import ConversationMemory
from sourcing.watchlist import Watchlist
data = Path(${JSON.stringify(DATA)}); (data / "cv_build").mkdir(parents=True, exist_ok=True)
db = str(data / "resume_agent.db")
prof = {"identity": {"name": "Kofi Test", "email": "kofi.test@example.com", "phone": "312-555-0100", "location": "Chicago, IL"},
        "summary": "Analyst who builds SQL and Python dashboards.",
        "experience": [
          {"org": "BlackOrigin", "location": "Remote", "roles": [{"title": "Data Analyst", "dates": "Jan 2022 - Present", "bullets": [
            "Built SQL and Python dashboards in Tableau used by 300 people every week.", "Automated Excel reporting for the sales team, saving 10 hours a week."]}]},
          {"org": "Stanbic Bank", "location": "Accra, Ghana", "roles": [{"title": "Credit Analyst", "dates": "Jan 2019 - Dec 2021", "bullets": [
            "Built credit risk models in Excel and SQL for the lending team.", "Reported portfolio statistics to the credit committee every month."]}]}],
        "education": [{"school": "University of Ghana", "degree": "BSc Statistics", "date": "May 2018", "location": "Accra, Ghana"}],
        "skills": {"Data": "SQL, Python, Tableau, Excel, statistics"},
        "extracurricular": [{"title": "Volunteer tutor", "date": "Jan 2017 - May 2018", "bullets": ["Taught statistics to 40 students."]}],
        "interests": "Chess, running"}
ConversationMemory(db).save("default", prof, {"identity": prof["identity"]}, [])
w = Watchlist(db)
w.set_saved(${JSON.stringify(SID)}, True, job={"source_id": ${JSON.stringify(SID)}, "source": "greenhouse", "company": "Acme Corp",
            "title": "Business Intelligence Analyst", "location": "Bloomington, IL", "url": ${JSON.stringify(JOB)},
            "jd_text": ${JSON.stringify(JD)}, "posted_at": "2026-11-15"})
print(json.dumps({"stage": w.stage_of(${JSON.stringify(SID)})}))
w.close()
`], { encoding: "utf8" });
if (seed.status !== 0) { console.error(seed.stderr); process.exit(1); }
check("seeded a profile and one saved job", /"stage": "saved"/.test(seed.stdout), seed.stdout.trim());
const stageOf = (sid) => {
  const r = spawnSync(PY, ["-c", `
import sys; sys.path.insert(0, ${JSON.stringify(REPO)})
from sourcing.watchlist import Watchlist
w = Watchlist(${JSON.stringify(path.join(DATA, "resume_agent.db"))}); print(w.stage_of(${JSON.stringify(sid)})); w.close()`], { encoding: "utf8" });
  return (r.stdout || "").trim();
};

// ---- 3. the engine on the test model, then the real app ------------------------------------------
const engineEnv = { ...process.env, RESUME_AGENT_DATA_DIR: DATA, RESUME_AGENT_DEV: "1", RESUME_AGENT_FAKE_LLM: "1",
                    RESUME_AGENT_FAKE_MAILBOX: "1", RESUME_AGENT_CRED_FILE: path.join(DATA, "credentials.env"),
                    TAILOR_SERVER_ONLY: "1", TAILOR_PORT: String(PORT), SJ_AGENT_DEBUG: "1" };
const engine = spawn(PY, ["-m", "ui.app"], { cwd: REPO, env: engineEnv, stdio: ["ignore", "ignore", "pipe"] });
let engineErr = "";
engine.stderr.on("data", (d) => { engineErr += d.toString(); });
let up = false;
for (let i = 0; i < 90 && !up; i++) {
  up = !!(await fetch(BASE + "/api/health").catch(() => null))?.ok;
  if (!up) await sleep(1000);
}
check("engine up on the test model", up, up ? "" : engineErr.slice(-400));
const stopEngine = () => { try { engine.kill(); } catch {} };
process.on("exit", stopEngine);

const app = await electron.launch({
  args: [HERE, `--user-data-dir=${path.join(DATA, "electron")}`],
  cwd: HERE,
  env: { ...process.env, ELECTRON_RUN_AS_NODE: undefined, PATH: "/opt/homebrew/bin:" + process.env.PATH,
         TAILOR_PYTHON: PY, RESUME_AGENT_DATA_DIR: DATA, RESUME_AGENT_DEV: "1", RESUME_AGENT_FAKE_LLM: "1",
         RESUME_AGENT_CRED_FILE: path.join(DATA, "credentials.env"), TAILOR_PORT: String(PORT),
         TAILOR_CDP_PORT: String(CDP), SJ_AGENT_DEBUG: "1" },
});
const finish = async (code) => {
  const failed = results.filter((r) => !r.pass);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  try { await app.close(); } catch {}
  stopEngine();
  server.close();
  fs.rmSync(DATA, { recursive: true, force: true });
  process.exit(code != null ? code : failed.length ? 1 : 0);
};
const win = await app.firstWindow();
win.on("pageerror", (e) => console.log("  [pageerror]", String(e).slice(0, 300)));
await win.waitForLoadState("domcontentloaded");
await win.waitForFunction(() => !!document.querySelector(".sidebar"), null, { timeout: 90_000 });
// The owner's window size, so the screenshots read as his screen does.
await app.evaluate(({ BrowserWindow }) => { const w = BrowserWindow.getAllWindows()[0]; w.setContentSize(1893, 1183); w.center(); });
await sleep(800);
check("app window opens", true, await win.title());

const api = (url, body) => fetch(BASE + url, body === undefined ? { headers: { "X-Tailor-Extension": "1" } }
  : { method: "POST", headers: { "Content-Type": "application/json", "X-Tailor-Extension": "1" }, body: JSON.stringify(body) })
  .then((r) => r.json()).catch(() => null);
const paneEval = async (expr) => {
  const list = await fetch(`http://127.0.0.1:${CDP}/json/list`).then((r) => r.json()).catch(() => []);
  const p = list.find((t) => t.type === "page" && !t.url.startsWith(BASE));
  if (!p) return null;
  const ws = new WebSocket(p.webSocketDebuggerUrl);
  return new Promise((resolve) => {
    ws.onopen = () => ws.send(JSON.stringify({ id: 1, method: "Runtime.evaluate", params: { expression: expr, returnByValue: true } }));
    ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id === 1) { ws.close(); resolve(d.result && d.result.result && d.result.result.value); } };
    ws.onerror = () => resolve(null);
  });
};
const fxLog = async () => JSON.parse((await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null") || [];
await win.evaluate(() => { window.__agentLog = []; window.tailorShell.onAutoApplyProgress((i) => {
  if (i && i.agent) window.__agentLog.push({ s: i.state, r: i.record_id, m: i.message }); }); });
const barText = () => win.evaluate(() => {
  const b = document.querySelector("#bbFill");
  if (!b || b.hidden) return "";
  const c = b.cloneNode(true);
  c.querySelectorAll(".bb-meta, .act-toggle").forEach((x) => x.remove());
  return c.textContent.trim();
});
const waitBar = async (re, ms) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { const t = await barText(); if (re.test(t)) return t; await sleep(400); }
  return await barText();
};
const chatApply = () => win.evaluate(() => [...document.querySelectorAll("#chatScroll .apply-msg")].map((m) => ({
  live: m.classList.contains("is-live"), text: m.innerText.trim() })));
// The window as the person sees it: the page, with the in-app browser (a native view drawn over
// its slot) composited in. Saved only when SJ_E2E_SHOTS names a folder.
const shot = async (name) => {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  const parts = await app.evaluate(async ({ BrowserWindow }) => {
    const w = BrowserWindow.getAllWindows()[0];
    const main = await w.webContents.capturePage();
    const out = { main: main.toPNG().toString("base64"), size: w.getContentSize(), views: [] };
    for (const v of (w.contentView && w.contentView.children) || []) {
      if (!v.webContents || v.webContents === w.webContents) continue;
      const b = v.getBounds();
      if (!b.width || !b.height) continue;
      const img = await v.webContents.capturePage();
      out.views.push({ b, png: img.toPNG().toString("base64") });
    }
    return out;
  });
  const tmp = path.join(DATA, "shot-" + name + ".json");
  fs.writeFileSync(tmp, JSON.stringify(parts));
  const r = spawnSync(PY, ["-c", `
import base64, io, json, sys
from PIL import Image
d = json.load(open(sys.argv[1]))
main = Image.open(io.BytesIO(base64.b64decode(d["main"]))).convert("RGB")
w, h = d["size"]; sx = main.width / w; sy = main.height / h
for v in d["views"]:
    b = v["b"]; im = Image.open(io.BytesIO(base64.b64decode(v["png"]))).convert("RGB")
    im = im.resize((round(b["width"] * sx), round(b["height"] * sy)))
    main.paste(im, (round(b["x"] * sx), round(b["y"] * sy)))
main.save(sys.argv[2])
print(main.size)
`, tmp, path.join(SHOTS, name + ".png")], { encoding: "utf8" });
  console.log(`  [shot] ${name}.png ${(r.stdout || r.stderr || "").trim()}`);
};

// ---- 4. a resume chat from the job, built --------------------------------------------------------
// The Jobs card and detail pane's "Tailor & apply" call /api/session/start_job (app.js tailorJob);
// the test makes the same calls, then opens the builder from the rail like a person returning to it.
// A question the test model still asks (a section the template prints) gets "leave it off".
const buildIn = (start, body) => win.evaluate(async ([start, body]) => {
  const post = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) }).then((r) => r.json());
  await post(start, body);
  let st = await post("/api/session/answer", { text: "Nothing new. Everything is in my saved profile. Build it." });
  for (let i = 0; i < 3 && st && st.phase !== "review"; i++) st = await post("/api/session/answer", { text: "None, leave it off. Build it." });
  return st;
}, [start, body]);
const built = await buildIn("/api/session/start_job", { source_id: SID });
check("the job's resume builds to one page", built && built.phase === "review" && built.one_page === true,
      built && JSON.stringify({ phase: built.phase, one_page: built.one_page, msg: (built.messages || []).join(" | ").slice(0, 200) }));
check("the state knows where Apply goes", built && built.apply && built.apply.url === JOB && built.apply.need_url === false,
      JSON.stringify(built && built.apply));
await win.click("[data-nav=dashboard]");
await win.waitForFunction(() => !!document.querySelector("#liveNavBtn"), null, { timeout: 60_000 });
await win.evaluate(() => document.querySelector("#liveNavBtn").click());
if (!(await win.waitForSelector("#applyBtn:not([hidden])", { timeout: 60_000 }).catch(() => null))) {
  check("the footer shows Apply", false);
  await finish(1);
}
await win.evaluate(() => window.Panel && window.Panel.open("cv"));
await sleep(1500);
const foot = await win.evaluate(() => {
  const a = document.querySelector("#applyBtn"), s = document.querySelector("#acceptBtn"), d = document.querySelector("#downloadBtn");
  return { apply: a.textContent.trim(), applyPrimary: a.classList.contains("btn-primary"), accept: s.textContent.trim(),
           acceptGhost: s.classList.contains("btn-ghost") && !s.classList.contains("btn-primary"), download: !d.hidden,
           note: document.querySelector("#acceptNote").textContent.trim(),
           primaries: [...document.querySelectorAll(".accept-actions .btn-primary")].filter((b) => !b.hidden).length };
});
check("the footer's one primary is Apply", /^Apply\s*→$/.test(foot.apply) && foot.applyPrimary && foot.primaries === 1, JSON.stringify(foot));
check("Accept & Save steps back to ghost, Download PDF stays", foot.acceptGhost && /accept/i.test(foot.accept) && foot.download, JSON.stringify(foot));
check("the footer line is one short line with no dashes", foot.note.length < 70 && !/[–—]/.test(foot.note), foot.note);
await shot("apply-footer-1893");

// ---- 5. Apply: save, package, Browser, Apply for me ----------------------------------------------
await win.click("#applyBtn");
const gate = await waitBar(/before i apply|opening|applying|signing|ready to send/i, 60_000);
if (/before i apply/i.test(gate)) {       // a fresh data folder: the answers every form asks, once
  check("the bar asks for the form answers once first (the queue's own gate)", true, gate);
  await win.locator("#bbFill button", { hasText: /apply anyway/i }).first().click();
}
const pkg = await api("/api/autoapply/board");
const row = pkg && pkg.tiles && (pkg.tiles.waiting || [])[0];
check("Apply saved the resume and made the Auto-apply package (Waiting on you)", !!row && row.posting_url === JOB && !!row.cv_path,
      JSON.stringify(row && { id: row.id, tile: row.tile, url: row.posting_url, cv: !!row.cv_path }));
const RID = row && row.id;
check("the saved resume's PDF is on disk", !!RID && fs.existsSync(path.join(DATA, "cv_build", `cv-${RID}.pdf`)));
check("the Tracker card moved to Approved", stageOf(SID) === "approved", stageOf(SID));
check("the pane switched to its Browser tab", await win.evaluate(() => window.Panel.isOpen("browser")));
// mid-fill: the chat tells the run, the Browser tab shows the form being filled
let mid = null;
for (let i = 0; i < 120 && !mid; i++) {
  const l = await fxLog();
  if (l.some((x) => String(x).startsWith("contact:")) || l.includes("questions")) mid = l;
  else await sleep(250);
}
const live = await chatApply();
check("the chat tells the run while it fills", live.some((m) => /Applying to Business Intelligence Analyst at Acme Corp/.test(m.text)), JSON.stringify(live));
await shot("apply-browser-midfill-1893");
const ready = await waitBar(/ready to send|stopped|could not|needs you|have a look/i, 180_000);
check("the run ends on Ready to send, with Send on the bar", /ready to send/i.test(ready)
      && await win.evaluate(() => !!document.querySelector("#bbFill .bb-send")), ready);
const l1 = await fxLog();
check("the agent clicked Apply and got past the sign-in", l1.includes("apply") && (l1.includes("signin") || l1.includes("account-created")), JSON.stringify(l1));
const contact = l1.find((x) => String(x).startsWith("contact:")) || "";
check("the contact page was filled and the tailored resume attached", /contact:4:cv/.test(contact), contact || JSON.stringify(l1));
check("every page was walked to Review, nothing submitted by the agent", l1.includes("disclosures") && !l1.includes("submitted"), JSON.stringify(l1));
check("the pane stops on the mock's Submit", (await paneEval("!!document.getElementById('submit')")) === true);
const after = await chatApply();
const sum = after[after.length - 1] || {};
check("the chat's apply message ends on Ready to send, steps folded under it", !sum.live && /ready to send/i.test(sum.text), JSON.stringify(after));
check("the chat never shows a profile value", !after.some((m) => /kofi\.test@example\.com|312-555-0100/.test(m.text)));
await shot("apply-ready-1893");

// ---- 6. Send: the shell presses Submit; the record and the Tracker say Applied -------------------
await win.click("#bbFill .bb-send");
const sent = await waitBar(/sent|could not find/i, 30_000);
check("Send submits and the bar says Sent", /^sent/i.test(sent), sent);
check("the mock received the submission", (await fxLog()).includes("submitted"));
let applied = "";
for (let i = 0; i < 20 && applied !== "applied"; i++) { applied = stageOf(SID); if (applied !== "applied") await sleep(500); }
check("the Tracker card moved to Applied", applied === "applied", applied);
const rec = await api(`/api/autoapply/item/${RID}`);
check("the application is Submitted", rec && rec.tile === "submitted", JSON.stringify(rec && { tile: rec.tile, stage: rec.stage }));
const said = await chatApply();
check("the chat says it was sent", (await win.evaluate(() => [...document.querySelectorAll("#chatScroll .msg.agent")].slice(-2).map((m) => m.innerText).join(" | "))).match(/sent/i) !== null
      || said.some((m) => /sent/i.test(m.text)));

// ---- 7. a pasted description with no link: Apply asks once, the link in the chat starts it -------
const pasted = await buildIn("/api/session/start", { jd: "Reporting Analyst at Globex. Chicago, IL. SQL, Excel and Tableau reporting for finance. Required: SQL, Excel, Tableau, statistics." });
check("the pasted resume builds, and Apply needs a link", pasted && pasted.phase === "review" && pasted.apply && pasted.apply.need_url === true,
      JSON.stringify(pasted && pasted.apply));
await win.click("[data-nav=dashboard]");
await win.waitForFunction(() => !!document.querySelector("#liveNavBtn"), null, { timeout: 60_000 });
await win.evaluate(() => document.querySelector("#liveNavBtn").click());
await win.waitForFunction(() => (document.querySelector("#builderRole")?.textContent || "").includes("Globex")
  || (document.querySelector("#builderRole")?.textContent || "").includes("Reporting"), null, { timeout: 60_000 }).catch(() => {});
await win.waitForSelector("#applyBtn:not([hidden])", { timeout: 60_000 });
const before = await api("/api/autoapply/board");
const nBefore = Object.values((before && before.tiles) || {}).reduce((n, t) => n + t.length, 0);
await win.click("#applyBtn");
await win.waitForFunction(() => /paste the link to the application/i.test([...document.querySelectorAll("#chatScroll .msg.agent")].pop()?.innerText || ""), null, { timeout: 30_000 }).catch(() => {});
const askText = await win.evaluate(() => [...document.querySelectorAll("#chatScroll .msg.agent")].pop()?.innerText || "");
check("Apply asks once in the chat for the application link", /paste the link to the application/i.test(askText), askText);
const mid2 = await api("/api/autoapply/board");
check("nothing was saved before there is somewhere to apply",
      Object.values((mid2 && mid2.tiles) || {}).reduce((n, t) => n + t.length, 0) === nBefore);
await win.fill("#composerInput", `Here it is: ${JOB}?again=7`);
await win.press("#composerInput", "Enter");
const run2 = await waitBar(/before i apply|opening|applying|signing|clicking|filled|ready to send/i, 60_000);
check("the link in the chat starts Apply for me", run2 !== "", run2);
const ready2 = await waitBar(/ready to send|stopped|could not|needs you|have a look/i, 180_000);
check("the pasted job's run also stops at Submit with Send", /ready to send/i.test(ready2), ready2);
const c2 = await chatApply();
check("the pasted job's run is told in the chat", c2.some((m) => /Applying to/.test(m.text)) && /ready to send/i.test((c2[c2.length - 1] || {}).text || ""), JSON.stringify(c2.slice(-2)));
// Stop here: nothing is sent for the pasted job.
await finish();
