// The iCIMS-style upload (2026-11-19): an upload box with no file input of its own until its
// button is pressed. The engine finds the resume's upload button (never the cover letter's) and
// the shell's chooser upload (shell-electron/chooser.js) puts the application's resume in through
// the page's own file chooser, in headless Chromium (tests/test_queue_run_2026_11_19.py runs it).
"use strict";
const fs = require("fs");
const os = require("os");
const http = require("http");
const path = require("path");
const { uploadViaChooser } = require(path.join(__dirname, "..", "..", "shell-electron", "chooser.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };

function findPlaywright() {
  const cands = [process.env.SJ_PLAYWRIGHT_DIR,
                 path.join(__dirname, "..", "..", "shell-electron", "node_modules", "playwright"),
                 path.join(process.env.HOME || "", "sponsorjobs", "shell-electron", "node_modules", "playwright")];
  for (const c of cands) { if (c && fs.existsSync(path.join(c, "package.json"))) return require(c); }
  throw new Error("playwright not found");
}
function serve(dir) {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const u = new URL(req.url, "http://x");
      const f = path.join(dir, path.normalize(decodeURIComponent(u.pathname)).replace(/^[/\\]+/, ""));
      if (!f.startsWith(dir) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end(); return; }
      res.writeHead(200, { "Content-Type": f.endsWith(".html") ? "text/html; charset=utf-8" : "text/css" });
      fs.createReadStream(f).pipe(res);
    });
    srv.listen(0, "127.0.0.1", () => resolve(srv));
  });
}
const LABEL = "Please upload your resume/CV (Max size: 5 MB)";

(async () => {
  const root = path.join(__dirname, "..", "fixtures", "forms");
  const core = fs.readFileSync(path.join(__dirname, "..", "..", "extension", "autofill_core.js"), "utf8");
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "sj-icims-"));
  const file = path.join(tmp, "Avery Example Resume.pdf");
  fs.writeFileSync(file, "%PDF-1.4\n% test\n");
  const srv = await serve(root);
  const base = "http://127.0.0.1:" + srv.address().port + "/icims_upload.html";
  const { chromium } = findPlaywright();
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    await page.goto(base);
    await page.evaluate(core);
    // no file input yet: the engine's own resume box finder has nothing to give
    assert(await page.evaluate("window.TailorAutofill.resumeInputOpen()") === false, "no file input before the press");
    const pt = await page.evaluate(`window.TailorAutofill.uploadButtonPoint(${JSON.stringify(LABEL)})`);
    assert(pt && pt.text === "Upload", "the resume's Upload button: " + JSON.stringify(pt));
    const box = await page.evaluate("(() => { const r = document.getElementById('up').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()");
    assert(Math.abs(pt.x - box.x) <= 1 && Math.abs(pt.y - box.y) <= 1, "the point is the resume's button, never the cover letter's");
    const s = await page.context().newCDPSession(page);
    const cdp = {
      send: (m, p) => s.send(m, p || {}),
      on: (fn) => { const h = (p) => fn("Page.fileChooserOpened", p); s.on("Page.fileChooserOpened", h); return () => s.off("Page.fileChooserOpened", h); },
    };
    const ok = await uploadViaChooser(cdp, pt, file);
    await page.waitForTimeout(200);
    const v = await page.evaluate("window.fxValues()");
    assert(ok === true, "the chooser took the file");
    assert(v.file === "Avery Example Resume.pdf" && v.letter === "" && v.opened === 1, "the resume went in, the letter box untouched: " + JSON.stringify(v));
    assert(await page.evaluate("document.getElementById('resumeName').value") === "Avery Example Resume.pdf", "the page shows it");
    // a press that opens no chooser: false, quickly, nothing set
    const none = await uploadViaChooser(cdp, { x: 5, y: 5 }, file, 600);
    assert(none === false, "no chooser: false");
    // no upload control at all: nothing to press
    await page.goto(base + "?nobutton");
    await page.evaluate(core);
    assert(await page.evaluate(`window.TailorAutofill.uploadButtonPoint(${JSON.stringify(LABEL)})`) === null, "no button: null");
  } finally {
    await browser.close();
    srv.close();
  }
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
