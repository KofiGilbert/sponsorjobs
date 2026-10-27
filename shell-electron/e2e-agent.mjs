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
recs = CVRecords(db)
ids = []
for role, url in [("Business Intelligence Analyst", ${JSON.stringify(JOB)}),
                  ("Analyst (captcha)", ${JSON.stringify(JOB + "?captcha=1")})]:
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
const [RID, RID_CAPTCHA] = JSON.parse(seed.stdout.trim().split("\n").pop());
check("seeded two queued jobs", RID > 0 && RID_CAPTCHA > 0, `ids ${RID}, ${RID_CAPTCHA}`);

// ---- 3. the engine on the test model, then the real app ---------------------------------
// The shell strips the test-model flags from any engine it spawns (real model only, CLAUDE.md
// section 13), so the test starts the engine itself and the shell reuses it on :57000.
const engineEnv = { ...process.env, RESUME_AGENT_DATA_DIR: DATA, RESUME_AGENT_DEV: "1", RESUME_AGENT_FAKE_LLM: "1",
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

const app = await electron.launch({
  args: [HERE],
  cwd: HERE,
  env: { ...process.env, ELECTRON_RUN_AS_NODE: undefined, PATH: "/opt/homebrew/bin:" + process.env.PATH,
         TAILOR_PYTHON: PY, RESUME_AGENT_DATA_DIR: DATA, RESUME_AGENT_DEV: "1", RESUME_AGENT_FAKE_LLM: "1",
         RESUME_AGENT_CRED_FILE: path.join(DATA, "credentials.env") },
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
const ready = await waitBar(/ready to send|stopped|could not|needs you|have a look/i, 120_000);
check("the run ends on Ready to send", /ready to send/i.test(ready), ready);
const log1 = (await paneEval("JSON.stringify(window.fxLog ? window.fxLog() : null)")) || "null";
const l1 = JSON.parse(log1) || [];
check("the agent clicked Apply itself", l1.includes("apply"), log1);
check("the agent got past the sign-in wall", l1.includes("signin") || l1.includes("account-created"), log1);
const contact = l1.find((x) => String(x).startsWith("contact:")) || "";
check("the contact page was filled and the resume attached", /contact:4:cv/.test(contact), contact || "no contact line");
check("every page was walked to Review", l1.includes("questions") && l1.includes("disclosures"), log1);
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

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
await app.close();
stopEngine();
server.close();
fs.rmSync(DATA, { recursive: true, force: true });
process.exit(failed.length ? 1 : 0);
