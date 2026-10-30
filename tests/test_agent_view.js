// The apply agent's page view and in-page actions (extension/agent_view.js) on the local form
// fixtures in tests/fixtures/forms/, in a real headless Chromium (docs/apply-agent.md, section
// 11). Never touches a real site: two local static servers (the second is the "other origin"
// an iCIMS-style iframe is served from). Run directly (node tests/test_agent_view.js) or
// through tests/test_agent_view.py, which is how pytest runs it.
//
// What it pins: indices in document order from indexBase; `*` on an element that was not in
// the previous view; no personal VALUE in value= (a filled email reads "(filled)", a password
// never shows its contents); select options count and first four; a file input at opacity 0
// still listed; the submit guard; a covered element clicked with note "covered"; the 12000
// char cap with its tail; the multipage fixture's step 2 fields marked new after Next; a
// cross-origin frame viewed with its own index base.
"use strict";
const fs = require("fs");
const http = require("http");
const path = require("path");
const assert = require("assert");

const ROOT = path.resolve(__dirname, "..");
const AGENT = fs.readFileSync(path.join(ROOT, "extension", "agent_view.js"), "utf8");
const CORE = fs.readFileSync(path.join(ROOT, "extension", "autofill_core.js"), "utf8");
const FIXTURES = path.join(ROOT, "tests", "fixtures", "forms");

function findPlaywright() {
  const cands = [process.env.SJ_PLAYWRIGHT_DIR,
                 path.join(ROOT, "shell-electron", "node_modules", "playwright"),
                 path.join(process.env.HOME || "", "sponsorjobs", "shell-electron", "node_modules", "playwright")];
  for (const c of cands) { if (c && fs.existsSync(path.join(c, "package.json"))) return require(c); }
  throw new Error("playwright not found (shell-electron devDependency)");
}

function serve(dir) {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const u = new URL(req.url, "http://x");
      const f = path.join(dir, path.normalize(decodeURIComponent(u.pathname)).replace(/^[/\\]+/, ""));
      if (!f.startsWith(dir) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end(); return; }
      const type = f.endsWith(".html") ? "text/html" : f.endsWith(".js") ? "text/javascript"
                 : f.endsWith(".css") ? "text/css" : "application/octet-stream";
      res.writeHead(200, { "Content-Type": type + "; charset=utf-8" });
      fs.createReadStream(f).pipe(res);
    });
    srv.listen(0, "127.0.0.1", () => resolve(srv));
  });
}

// The view's lines for one index, e.g. line(text, 3) -> '[3]<input ...>'.
const line = (text, i) => (text.split("\n").find((l) => l.replace(/^\*/, "").startsWith(`[${i}]`)) || "");
const indices = (text) => text.split("\n").map((l) => (l.match(/^\*?\[(\d+)\]/) || [])[1]).filter(Boolean).map(Number);
const starred = (text) => text.split("\n").filter((l) => l.startsWith("*["));

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

// ------------------------------------------------------------------ the view
test("indices run in document order from indexBase, with and without the engine", async ({ open }) => {
  for (const withEngine of [false, true]) {
    const page = await open("react_select.html");
    if (withEngine) await page.evaluate(CORE);
    await page.evaluate(AGENT);
    const v = await page.evaluate("window.TailorAgent.view()");
    const ix = indices(v.text);
    assert.deepStrictEqual(ix, ix.map((_, i) => i + 1), "indices 1..N in order");
    assert.strictEqual(v.count, ix.length);
    assert.ok(v.text.startsWith("URL: http://127.0.0.1:"), v.text.slice(0, 60));
    assert.ok(/\nTitle: Apply \(react-select fixture\)\n\[Start of page\]\n/.test(v.text));
    assert.ok(v.text.includes("\nSoftware Engineer\n"), "the h1 is a text line");
    assert.ok(line(v.text, 1).includes('<input type=text label="First Name" value="" required>') || line(v.text, 1).includes('<input type=text label="First Name" value="">'), line(v.text, 1));
    assert.ok(line(v.text, 3).includes('<input type=email label="Email" value=""'), line(v.text, 3));
    assert.ok(/<combobox label="Country" value="" expanded=false/.test(v.text), "react-select input is a combobox");
    assert.ok(/<checkbox label="I agree to the candidate privacy notice" checked=false required>/.test(v.text), v.text);
    assert.ok(/\]<button required>Submit application<\/button>|\]<button>Submit application<\/button>/.test(v.text), "submit button listed");
    assert.ok(v.text.trim().endsWith("[End of page]") || /\[End of page\] \(/.test(v.text), "ends with the end marker");
    assert.ok(starred(v.text).length === 0, "nothing is starred on the first view");
    assert.ok(!v.truncated);
    const b = await page.evaluate("window.TailorAgent.view({indexBase: 2001})");
    assert.strictEqual(indices(b.text)[0], 2001);
    assert.ok(await page.evaluate("window.TailorAgent.element(2001) === document.getElementById('first_name')"));
    assert.ok(await page.evaluate("window.TailorAgent.element(1) === null"), "an index from the old base is not valid");
    await page.close();
  }
});

test("an element added after the first view is marked with *", async ({ open }) => {
  const page = await open("react_select.html");
  await page.evaluate(AGENT);
  const a = await page.evaluate("window.TailorAgent.view()");
  assert.strictEqual(starred(a.text).length, 0);
  await page.evaluate(() => {
    const d = document.createElement("div");
    d.innerHTML = '<label for="extra">Anything else?</label><input type="text" id="extra">';
    document.getElementById("widgets").appendChild(d);
  });
  const b = await page.evaluate("window.TailorAgent.view()");
  const s = starred(b.text);
  assert.strictEqual(s.length, 1, b.text);
  assert.ok(s[0].includes('label="Anything else?"'), s[0]);
  const c = await page.evaluate("window.TailorAgent.view()");
  assert.strictEqual(starred(c.text).length, 0, "seen once, no longer new");
  await page.close();
});

test("no personal value in value=: a filled email reads (filled), a password never shows", async ({ open }) => {
  const page = await open("react_select.html");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const email = indices(v.text).find((i) => line(v.text, i).includes("type=email"));
  const r = await page.evaluate(`window.TailorAgent.act({op: "type", index: ${email}, text: "kofi@example.com"})`);
  assert.strictEqual(r.ok, true, JSON.stringify(r));
  assert.strictEqual(r.note, "");
  assert.strictEqual(await page.evaluate("document.getElementById('email').value"), "kofi@example.com");
  const w = await page.evaluate("window.TailorAgent.view()");
  assert.ok(line(w.text, email).includes('value="(filled)"'), line(w.text, email));
  assert.ok(!w.text.includes("kofi@example.com"), "the address never appears in the view");
  assert.ok(!w.text.includes("kofi"), "not even part of it");
  await page.close();

  const p2 = await open("workday_account.html");
  await p2.evaluate(AGENT);
  const a = await p2.evaluate("window.TailorAgent.view()");
  const pw = indices(a.text).find((i) => line(a.text, i).includes("type=password"));
  assert.ok(pw, a.text);
  assert.ok(/<input type=password label="Password" value="">/.test(line(a.text, pw)), line(a.text, pw));
  await p2.evaluate(`window.TailorAgent.act({op: "type", index: ${pw}, text: "Hunter2-secret"})`);
  const b = await p2.evaluate("window.TailorAgent.view()");
  assert.ok(!b.text.includes("Hunter2"), "a password's contents never appear");
  assert.ok(/value="(\(filled\))?"/.test(line(b.text, pw)), line(b.text, pw));
  // The anti-spam trap is listed like any input (the brain is told not to answer questions
  // about the person); its label says what it is.
  assert.ok(/robots only/.test(a.text));
  await p2.close();
});

test("select shows its options count and first four; select() picks by text, lists options on a miss", async ({ open }) => {
  const page = await open("multipage.html");
  await page.evaluate(AGENT);
  // The person (here: the test) presses Next twice to reach the select on step 3.
  await page.click("#next"); await page.click("#next");
  const v = await page.evaluate("window.TailorAgent.view()");
  const sel = indices(v.text).find((i) => line(v.text, i).includes("<select"));
  assert.ok(sel, v.text);
  assert.ok(line(v.text, sel).includes('<select label="Veteran status" value="" options=4 first="Select...|I am a protected veteran|I am not a protected veteran|I do not wish to self-identify">'), line(v.text, sel));
  const miss = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${sel}, option: "Active duty"})`);
  assert.strictEqual(miss.ok, false);
  assert.ok(/options: Select\.\.\., I am a protected veteran/.test(miss.note), miss.note);
  const hit = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${sel}, option: "i do not wish to self-identify"})`);
  assert.strictEqual(hit.ok, true, JSON.stringify(hit));
  const w = await page.evaluate("window.TailorAgent.view()");
  // a self-identification choice reads as filled, never which one
  assert.ok(line(w.text, sel).includes('value="(filled)"'), line(w.text, sel));
  // click on a select is refused: the brain must use select.
  const c = await page.evaluate(`window.TailorAgent.act({op: "click", index: ${sel}})`);
  assert.deepStrictEqual([c.ok, c.note], [false, "use select"]);
  await page.close();
});

test("a custom dropdown is picked through the engine when present, by clicking options without it", async ({ open }) => {
  const page = await open("workday.html");
  await page.evaluate(AGENT);                                   // no engine: the plain path
  const v = await page.evaluate("window.TailorAgent.view()");
  const country = indices(v.text).find((i) => /<combobox label="Country"/.test(line(v.text, i)));
  assert.ok(country, v.text);
  assert.ok(line(v.text, country).includes('value="" expanded=false'), line(v.text, country));
  const miss = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${country}, option: "Narnia"})`);
  assert.strictEqual(miss.ok, false);
  assert.ok(/options: Afghanistan, Canada, India/.test(miss.note), miss.note);
  const hit = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${country}, option: "United States of America"})`);
  assert.strictEqual(hit.ok, true, JSON.stringify(hit));
  assert.strictEqual((await page.evaluate("window.fxValues()")).country, "United States of America");
  await page.close();

  const p2 = await open("react_select.html");
  await p2.evaluate(CORE);
  await p2.evaluate(AGENT);
  const a = await p2.evaluate("window.TailorAgent.view()");
  const c2 = indices(a.text).find((i) => /<combobox label="Country"/.test(line(a.text, i)));
  const r = await p2.evaluate(`window.TailorAgent.act({op: "select", index: ${c2}, option: "United States"})`);
  assert.strictEqual(r.ok, true, JSON.stringify(r));
  assert.strictEqual((await p2.evaluate("window.fxValues()")).country, "United States");
  const b = await p2.evaluate("window.TailorAgent.view()");
  assert.ok(line(b.text, c2).includes('value="united states"') || line(b.text, c2).includes('value="United States"'), line(b.text, c2));
  await p2.close();
});

test("a file input at opacity 0 (the upload button pattern) is still listed; click says use upload", async ({ open }) => {
  const page = await open("icims_inner.html");
  await page.evaluate(AGENT);
  await page.evaluate(() => {
    const f = document.getElementById("resume_upload");
    f.style.opacity = "0"; f.style.width = "0.1px"; f.style.height = "0.1px"; f.style.position = "absolute";
  });
  const v = await page.evaluate("window.TailorAgent.view()");
  const file = indices(v.text).find((i) => line(v.text, i).includes("<file"));
  assert.ok(file, v.text);
  assert.ok(line(v.text, file).includes('<file label="Resume" file=none accept=".pdf,.doc,.docx">'), line(v.text, file));
  const c = await page.evaluate(`window.TailorAgent.act({op: "click", index: ${file}})`);
  assert.deepStrictEqual([c.ok, c.note], [false, "use upload"]);
  const t = await page.evaluate(`window.TailorAgent.act({op: "type", index: ${file}, text: "x"})`);
  assert.deepStrictEqual([t.ok, t.note], [false, "use upload"]);
  await page.close();
});

test("a styled checkbox (the real input at opacity 0 inside a drawn box) is listed and clicks", async ({ open }) => {
  // Workday's "I consent to the terms" box, 2026-10-28: the agent could not see it and gave up.
  const page = await open("mock_ats.html");
  await page.evaluate(AGENT);
  await page.evaluate(() => {
    const wrap = document.createElement("div");
    wrap.innerHTML = '<div class="box" style="position:relative;width:24px;height:24px;border:1px solid #333">'
      + '<input id="consent" type="checkbox" style="opacity:0;position:absolute;width:24px;height:24px;margin:0"></div>'
      + '<label for="consent">Yes, I have read and consent to the terms and conditions.</label>';
    document.getElementById("step").appendChild(wrap);
  });
  const v = await page.evaluate("window.TailorAgent.view()");
  const box = indices(v.text).find((i) => line(v.text, i).includes("<checkbox"));
  assert.ok(box, v.text);
  assert.ok(line(v.text, box).includes('label="Yes, I have read and consent to the terms and conditions." checked=false'), line(v.text, box));
  const c = await page.evaluate(`window.TailorAgent.act({op: "click", index: ${box}})`);
  assert.ok(c.ok, JSON.stringify(c));
  assert.strictEqual(await page.evaluate("document.getElementById('consent').checked"), true);
  await page.close();
});

test("the submit guard refuses Submit; submitButton() still finds it for the shell", async ({ open }) => {
  const page = await open("react_select.html");
  await page.evaluate(AGENT);
  await page.evaluate("document.getElementById('submit').addEventListener('click', () => { window.__sent = true; })");
  const v = await page.evaluate("window.TailorAgent.view()");
  const submit = indices(v.text).find((i) => /<button[^>]*>Submit application<\/button>/.test(line(v.text, i)));
  assert.ok(submit, v.text);
  const r = await page.evaluate(`window.TailorAgent.act({op: "click", index: ${submit}})`);
  assert.deepStrictEqual([r.ok, r.note, r.changed], [false, "submit-guard", false]);
  assert.strictEqual(await page.evaluate("window.__sent === true"), false, "never clicked");
  assert.strictEqual(await page.evaluate("window.TailorAgent.submitButton()"), submit);
  await page.close();

  // No submit-like button on a job page: null. And a label wrapping a Submit control counts.
  const p2 = await open("job_page.html");
  await p2.evaluate(AGENT);
  await p2.evaluate("window.TailorAgent.view()");
  assert.strictEqual(await p2.evaluate("window.TailorAgent.submitButton()"), null);
  await p2.close();
});

test("a covered element is still clicked, with note covered; an uncovered one has no note", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(AGENT);
  await page.evaluate(() => {
    window.__applied = 0;
    document.querySelector("a.apply").addEventListener("click", (e) => { e.preventDefault(); window.__applied++; });
  });
  const v = await page.evaluate("window.TailorAgent.view()");
  const apply = indices(v.text).find((i) => /<a>Apply now<\/a>/.test(line(v.text, i)));
  assert.ok(apply, v.text);
  const plain = await page.evaluate(`window.TailorAgent.act({op: "click", index: ${apply}})`);
  assert.deepStrictEqual([plain.ok, plain.note], [true, ""]);
  await page.evaluate(() => {
    const o = document.createElement("div");
    o.id = "cookie-banner";
    o.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:1000";
    o.textContent = "We use cookies";
    document.body.appendChild(o);
  });
  const cov = await page.evaluate(`window.TailorAgent.act({op: "click", index: ${apply}})`);
  assert.deepStrictEqual([cov.ok, cov.note], [true, "covered"], JSON.stringify(cov));
  assert.strictEqual(await page.evaluate("window.__applied"), 2);
  // The site's own search boxes are listed but flagged, so the brain can ignore them.
  assert.ok(/<input type=text label="Search for jobs by title or keyword" value="" search=true>/.test(v.text), v.text);
  assert.ok(/<combobox label="City, state, or country" value="" expanded=false search=true>/.test(v.text), v.text);
  await page.close();
});

test("the view is capped at 12000 chars with a tail saying how many elements were cut", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(AGENT);
  await page.evaluate(() => {
    const m = document.querySelector("main");
    for (let i = 0; i < 700; i++) {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = "Option number " + i + " of the long list";
      m.appendChild(b);
    }
  });
  const v = await page.evaluate("window.TailorAgent.view()");
  assert.ok(v.text.length <= 12000, "length " + v.text.length);
  assert.strictEqual(v.truncated, true);
  assert.ok(v.count > 700);
  const m = v.text.match(/\((\d+) more elements below\)\n\[End of page\]/);
  assert.ok(m, v.text.slice(-200));
  assert.strictEqual(Number(m[1]), v.count - indices(v.text).length);
  assert.ok(/\[End of page\] \([\d.]+ pages below\)$/.test(v.text.trim()), v.text.slice(-80));
  // Every index that was shown still resolves; the cut ones do too (refs hold them all).
  assert.ok(await page.evaluate(`window.TailorAgent.element(${v.count}) !== null`));
  await page.close();
});

test("multipage: after Next the step 2 fields are new, headings and errors are text lines", async ({ open }) => {
  const page = await open("multipage.html");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  const a = await page.evaluate("window.TailorAgent.view()");
  assert.ok(a.text.includes("\nApplication\nStep 1: Contact\n"), a.text);
  const next = indices(a.text).find((i) => /<button>Next<\/button>/.test(line(a.text, i)));
  assert.ok(next, a.text);
  const r = await page.evaluate(`window.TailorAgent.act({op: "click", index: ${next}})`);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.changed, true, "the page changed");
  assert.strictEqual(await page.evaluate("fxPage()"), 2);
  const b = await page.evaluate("window.TailorAgent.view()");
  assert.ok(b.text.includes("\nStep 2: Questions\n"), b.text);
  const s = starred(b.text);
  assert.ok(s.length >= 3, b.text);
  assert.ok(s.some((l) => /<combobox label="Are you legally authorized to work in the United States\?"/.test(l)), b.text);
  assert.ok(s.some((l) => /<radio label="Will you now or in the future require visa sponsorship\?" option="Yes" checked=false>/.test(l)), b.text);
  assert.ok(s.some((l) => /<input type=text label="How many years of experience do you have\?" value="">/.test(l)), b.text);
  assert.ok(!starred(b.text).some((l) => /<button>Next<\/button>/.test(l)), "Next was seen before");
  // A visible validation error is rendered for the model.
  await page.evaluate(() => {
    const e = document.createElement("div"); e.className = "field-error"; e.textContent = "Years of experience is required";
    document.getElementById("step").appendChild(e);
    const al = document.createElement("p"); al.setAttribute("role", "alert"); al.textContent = "Please fix the errors below";
    document.body.insertBefore(al, document.body.firstChild);
  });
  const c = await page.evaluate("window.TailorAgent.view()");
  assert.ok(c.text.includes('Error: "Years of experience is required"'), c.text);
  assert.ok(c.text.includes('Error: "Please fix the errors below"'), c.text);
  // Clicking an ARIA radio by index ticks it.
  const yes = indices(c.text).find((i) => /<radio label="Will you now[^"]*" option="Yes"/.test(line(c.text, i)));
  await page.evaluate(`window.TailorAgent.act({op: "click", index: ${yes}})`);
  const d = await page.evaluate("window.TailorAgent.view()");
  assert.ok(line(d.text, yes).includes("checked=true"), line(d.text, yes));
  await page.close();
});

test("scroll and wait: scroll moves 0.8 of a viewport, wait is capped at 5 s", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(AGENT);
  await page.evaluate("document.body.style.height = '5000px'");
  const v = await page.evaluate("window.TailorAgent.view()");
  assert.ok(/\[End of page\] \([\d.]+ pages below\)$/.test(v.text.trim()), v.text.slice(-60));
  const r = await page.evaluate("window.TailorAgent.act({op: 'scroll', direction: 'down'})");
  assert.strictEqual(r.ok, true);
  const y = await page.evaluate("window.scrollY");
  const vh = await page.evaluate("window.innerHeight");
  assert.ok(Math.abs(y - Math.round(vh * 0.8)) <= 2, `scrolled ${y} of ${vh}`);
  const w = await page.evaluate("window.TailorAgent.view()");
  assert.ok(/pages above/.test(w.text), w.text.slice(-80));
  const t0 = Date.now();
  await page.evaluate("window.TailorAgent.act({op: 'wait', seconds: 60})");
  assert.ok(Date.now() - t0 < 7000, "capped");
  await page.close();
});

test("shadow DOM: open roots are walked in order, a closed root stays invisible", async ({ open }) => {
  const page = await open("shadow.html");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const labels = indices(v.text).map((i) => (line(v.text, i).match(/label="([^"]*)"/) || [])[1]);
  assert.deepStrictEqual(labels, ["First name", "Last name", "Email address", "Mobile phone", "Are you legally authorized to work in the US?"], v.text);
  await page.close();
});

test("a cross-origin frame is viewed with its own index base", async ({ open, B }) => {
  const page = await open("icims_outer.html", `?inner=http://localhost:${B}/icims_inner.html`);
  await page.waitForTimeout(300);
  await page.evaluate(AGENT);
  const top = await page.evaluate("window.TailorAgent.view()");
  assert.ok(!/<file/.test(top.text), "the top page cannot see into the frame");
  assert.ok(top.text.includes("\nCareers at Example Co\n"));
  const frame = page.frames()[1];
  await frame.evaluate(AGENT);                                  // what frames.js does over CDP
  const inner = await frame.evaluate("window.TailorAgent.view({indexBase: 1001})");
  const ix = indices(inner.text);
  assert.strictEqual(ix[0], 1001, inner.text);
  assert.deepStrictEqual(ix, ix.map((_, i) => 1001 + i));
  assert.ok(/\[\d+\]<file label="Resume" file=none/.test(inner.text), inner.text);
  assert.ok(/\[\d+\]<button>Submit<\/button>/.test(inner.text), inner.text);
  const sub = ix.find((i) => /<button>Submit<\/button>/.test(line(inner.text, i)));
  const r = await frame.evaluate(`window.TailorAgent.act({op: "click", index: ${sub}})`);
  assert.deepStrictEqual([r.ok, r.note], [false, "submit-guard"]);
  assert.strictEqual(await frame.evaluate("window.TailorAgent.submitButton()"), sub);
  await page.close();
});

test("a captcha iframe is never entered", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(AGENT);
  await page.evaluate(() => {
    const f = document.createElement("iframe");
    f.src = "icims_inner.html"; f.title = "reCAPTCHA"; f.width = 300; f.height = 80;
    document.querySelector("main").appendChild(f);
  });
  await page.waitForTimeout(300);
  const v = await page.evaluate("window.TailorAgent.view()");
  assert.ok(!/<file/.test(v.text) && !/First Name/.test(v.text), v.text);
  await page.close();
});

// ------------------------------------------------------------------ choices for an ask
// The shell's real order (shell-electron/agent.js dropdownOptions / closeDropdown): mark what is
// on screen, a TRUSTED click, read the popup, then ONE real Escape only while it is open, and
// closePopup("blur") only when the widget ignored that Escape. Returns what was read, how many
// Escapes went out, and whether the popup is still open.
async function readLikeShell(page, idx, click) {
  const was = await page.evaluate(`window.TailorAgent.popupOpen(${idx})`);
  await page.evaluate(`window.TailorAgent.markPopups(${idx}, ${!was})`);
  if (!was) await click();
  await page.waitForTimeout(150);
  const read = await page.evaluate(`window.TailorAgent.popupOptions(${idx})`);
  let escapes = 0;
  if (await page.evaluate(`window.TailorAgent.popupOpen(${idx})`)) {
    await page.keyboard.press("Escape"); escapes++;
    await page.waitForTimeout(50);
    if (await page.evaluate(`window.TailorAgent.popupOpen(${idx})`)) await page.evaluate(`window.TailorAgent.closePopup(${idx}, "blur")`);
  }
  return { read, escapes, open: await page.evaluate(`window.TailorAgent.popupOpen(${idx})`) };
}
const indexOf = (text, re) => indices(text).find((i) => re.test(line(text, i)));

test("popupOptions reads the popup the click opened (aria-controls, a portal); closed means no Escape at all", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(() => {
    const m = document.querySelector("main") || document.body;
    m.insertAdjacentHTML("beforeend", `
      <div class="field"><label id="gl">Gender</label>
      <button id="g" type="button" aria-haspopup="listbox" aria-expanded="false" aria-labelledby="gl" aria-controls="glist">Select</button>
      <ul id="glist" role="listbox" hidden>
        <li role="option">Select...</li><li role="option">Male</li><li role="option">Female</li><li role="option">I don't wish to answer</li>
      </ul></div>
      <div class="field"><label for="e">Ethnicity</label>
      <input id="e" role="combobox" aria-expanded="false" aria-autocomplete="list"></div>
      <div id="portal"></div>`);
    window.escapes = 0;
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") window.escapes++; }, true);
    const g = document.getElementById("g"), gl = document.getElementById("glist");
    const set = (on) => { gl.hidden = !on; g.setAttribute("aria-expanded", on ? "true" : "false"); };
    g.addEventListener("click", () => set(gl.hidden));
    g.addEventListener("keydown", (e) => { if (e.key === "Escape") set(false); });
    const e = document.getElementById("e"), portal = document.getElementById("portal");
    e.addEventListener("click", () => {          // a portal listbox with no aria-controls at all
      portal.innerHTML = '<div role="listbox"><div role="option">Asian</div><div role="option">White</div>'
        + '<div role="option">Decline to self-identify</div></div>';
      e.setAttribute("aria-expanded", "true");
    });
    e.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { portal.innerHTML = ""; e.setAttribute("aria-expanded", "false"); } });
  });
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const gi = indexOf(v.text, /<combobox label="Gender"/), ei = indexOf(v.text, /<combobox label="Ethnicity"/);
  assert.ok(gi && ei, v.text);
  let r = await readLikeShell(page, gi, () => page.click("#g"));
  assert.deepStrictEqual(r.read.options, ["Male", "Female", "I don't wish to answer"]);
  assert.strictEqual(r.read.partial, false);
  assert.ok(r.escapes === 1 && !r.open && await page.evaluate(() => document.getElementById("glist").hidden));
  r = await readLikeShell(page, ei, () => page.click("#e"));
  assert.deepStrictEqual(r.read.options, ["Asian", "White", "Decline to self-identify"]);
  assert.ok(r.escapes === 1 && !r.open && await page.evaluate(() => document.getElementById("portal").innerHTML === ""));
  // Once closed, closePopup sends nothing (an Escape there can close the dialog holding the form)
  const before = await page.evaluate("window.escapes");
  assert.strictEqual(await page.evaluate(`window.TailorAgent.closePopup(${gi}, "escape")`), true);
  assert.strictEqual(await page.evaluate(`window.TailorAgent.closePopup(${ei})`), true);
  assert.strictEqual(await page.evaluate("window.escapes"), before);
  // A detached control reads nothing
  await page.evaluate(() => document.getElementById("g").remove());
  assert.deepStrictEqual((await page.evaluate(`window.TailorAgent.popupOptions(${gi})`)).options, []);
  await page.close();
});

test("the fixture widgets: a Workday listbox and react-select keep a 'Choose not to ...' decline, and a chosen one reads as answered", async ({ open }) => {
  for (const withEngine of [false, true]) {
    const page = await open("workday.html");
    if (withEngine) await page.evaluate(CORE);
    await page.evaluate(() => {
      window.FixtureWidgets.WorkdayListbox(document.getElementById("wd"), { aid: "gender", label: "Gender",
        options: ["Male", "Female", "Choose not to disclose"] });
      window.FixtureWidgets.WorkdayPrompt(document.getElementById("wd"), { label: "Ethnicity",
        options: ["Asian", "Hispanic or Latino", "Choose not to self-identify"] });
    });
    await page.evaluate(AGENT);
    let v = await page.evaluate("window.TailorAgent.view()");
    const gi = indexOf(v.text, /<combobox label="Gender"/);
    assert.ok(gi, v.text);
    let r = await readLikeShell(page, gi, () => page.click("[data-automation-id='gender']"));
    assert.deepStrictEqual(r.read.options, ["Male", "Female", "Choose not to disclose"], "engine=" + withEngine);
    assert.ok(r.escapes === 1 && !r.open);
    const pi = indexOf(v.text, /label="Ethnicity"/);
    assert.ok(pi, v.text);
    r = await readLikeShell(page, pi, () => page.click("input[placeholder='Search'] >> nth=-1"));
    assert.deepStrictEqual(r.read.options, ["Asian", "Hispanic or Latino", "Choose not to self-identify"]);
    assert.ok(!r.open);
    // the person's decline, once chosen, is a value: the field no longer looks unanswered (a
    // self-identification choice reads as filled, never which one)
    await page.click("[data-automation-id='gender']");
    await page.click("text=Choose not to disclose");
    v = await page.evaluate("window.TailorAgent.view()");
    assert.ok(/<combobox label="Gender" value="\(filled\)"/i.test(v.text), v.text);
    await page.close();
  }
  const page = await open("react_select.html");
  await page.evaluate(() => window.FixtureWidgets.ReactSelect(document.getElementById("widgets"), { id: "vet",
    label: "Veteran status", options: ["I am a protected veteran", "I am not a protected veteran", "Choose not to self-identify"] }));
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const vi = indexOf(v.text, /<combobox label="Veteran status"/);
  assert.ok(vi, v.text);
  const r = await readLikeShell(page, vi, () => page.click("#vet"));
  assert.deepStrictEqual(r.read.options, ["I am a protected veteran", "I am not a protected veteran", "Choose not to self-identify"]);
  assert.ok(!r.open);
  await page.close();
});

test("only the opened list is read: other closed lists, an inline listbox and the site's nav menu never are", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(() => {
    document.querySelector("header").insertAdjacentHTML("beforeend",
      '<nav><ul class="dropdown menu"><li><a href="#">Home</a></li><li><a href="#">About us</a></li>'
      + '<li><a href="#">Students and graduates</a><ul class="submenu"><li><a href="#">Internships</a></li></ul></li></ul></nav>');
    const m = document.querySelector("main");
    const field = (id, label, opts, ignoresEscape) => `
      <div class="field"><span class="q" id="${id}-l">${label}</span>
        <button id="${id}" type="button" aria-haspopup="listbox" aria-labelledby="${id}-l" ${ignoresEscape ? "data-noesc" : ""}>Select</button>
        <ul class="dropdown-options" id="${id}-list" style="visibility:hidden">${opts.map((o) => `<li>${o}</li>`).join("")}</ul></div>`;
    m.insertAdjacentHTML("beforeend",
      field("gender", "Gender", ["Male", "Female", "Decline to self-identify"])
      + field("race", "Race", ["Asian", "Black or African American", "White"], true)
      + field("vet", "Veteran", ["I am a veteran", "I am not a veteran"])
      + '<div class="field"><span class="q" id="skills-l">Skills</span><div role="listbox" aria-labelledby="skills-l">'
      + '<div role="option">Python</div><div role="option">SQL</div></div></div>'
      + '<div class="field"><label for="city">Current city</label><input id="city" role="combobox" aria-autocomplete="list" required></div>'
      + '<div class="field"><label for="dead">Pronouns</label><button id="dead" type="button" aria-haspopup="listbox">Select</button></div>');
    for (const id of ["gender", "race", "vet"]) {
      const b = document.getElementById(id), list = document.getElementById(id + "-list");
      b.addEventListener("click", () => { list.style.visibility = list.style.visibility === "hidden" ? "visible" : "hidden"; });
      if (!b.hasAttribute("data-noesc")) b.addEventListener("keydown", (e) => { if (e.key === "Escape") list.style.visibility = "hidden"; });
    }
  });
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const ri = indexOf(v.text, /<combobox label="Race"/), ci = indexOf(v.text, /label="Current city"/);
  const di = indexOf(v.text, /<combobox label="Pronouns"/), gi = indexOf(v.text, /<combobox label="Gender"/);
  assert.ok(ri && ci && di && gi, v.text);
  let r = await readLikeShell(page, ri, () => page.click("#race"));
  assert.deepStrictEqual(r.read.options, ["Asian", "Black or African American", "White"]);
  // Race ignores Escape: one Escape, then a second click on its button closes it
  assert.ok(r.escapes === 1 && !r.open && await page.evaluate(() => document.getElementById("race-list").style.visibility === "hidden"));
  r = await readLikeShell(page, ci, () => page.click("#city"));      // a typeahead that lists nothing yet
  assert.deepStrictEqual(r.read.options, []);
  assert.strictEqual(r.escapes, 0);
  r = await readLikeShell(page, di, () => page.click("#dead"));      // a click that opened nothing
  assert.deepStrictEqual(r.read.options, []);
  assert.strictEqual(r.escapes, 0);
  r = await readLikeShell(page, gi, () => page.click("#gender"));
  assert.deepStrictEqual(r.read.options, ["Male", "Female", "Decline to self-identify"]);
  await page.close();
});

test("status rows, disabled headers and a loading list are never choices; a native select gives every choice", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(() => {
    const m = document.querySelector("main");
    m.insertAdjacentHTML("beforeend", `
      <div class="field"><label for="city">City</label><input id="city" role="combobox" aria-controls="city-list" aria-expanded="true">
        <ul id="city-list" role="listbox"><li class="no-results">No results found</li></ul></div>
      <div class="field"><label id="eth-l">Ethnicity</label><button id="eth" type="button" aria-haspopup="listbox" aria-labelledby="eth-l" aria-controls="eth-list" aria-expanded="true">Select</button>
        <ul id="eth-list" role="listbox"><li role="option" aria-disabled="true">Hispanic or Latino (choose one below)</li>
          <li role="option">Mexican</li><li role="option">Cuban</li><li role="presentation">More</li></ul></div>
      <div class="field"><label for="loc">Location</label><input id="loc" role="combobox" aria-controls="loc-list" aria-expanded="true">
        <div id="loc-list" role="listbox" aria-busy="true"><div class="notice">Loading...</div></div></div>
      <div class="field"><label for="race">Race/Ethnicity</label><select id="race" required>
        <option value="">Select ...</option><option>American Indian or Alaskan Native</option><option>Asian</option>
        <option>Black or African American</option><option disabled>Hispanic groups</option><option>Hispanic or Latino</option>
        <option>Native Hawaiian or Other Pacific Islander</option><option>White</option><option>Two or More Races</option>
        <option>Decline to self-identify</option></select></div>`);
  });
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const ci = indexOf(v.text, /label="City"/), ei = indexOf(v.text, /<combobox label="Ethnicity"/);
  const li = indexOf(v.text, /label="Location"/), si = indexOf(v.text, /<select label="Race\/Ethnicity"/);
  assert.ok(ci && ei && li && si, v.text);
  assert.deepStrictEqual((await page.evaluate(`window.TailorAgent.popupOptions(${ci})`)).options, []);
  assert.deepStrictEqual((await page.evaluate(`window.TailorAgent.popupOptions(${ei})`)).options, ["Mexican", "Cuban"]);
  const loading = await page.evaluate(`window.TailorAgent.popupOptions(${li})`);
  assert.ok(loading.busy && !loading.options.length, JSON.stringify(loading));
  assert.ok(/options=10 first=/.test(line(v.text, si)), line(v.text, si));       // the view still prints four
  assert.deepStrictEqual(await page.evaluate(`window.TailorAgent.selectOptions(${si})`), {
    options: ["American Indian or Alaskan Native", "Asian", "Black or African American", "Hispanic or Latino",
              "Native Hawaiian or Other Pacific Islander", "White", "Two or More Races", "Decline to self-identify"],
    partial: false });
  await page.close();
});

test("a dropdown in a same-origin frame or a nested shadow root is read from its own root", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(() => {
    const make = (doc, root) => {
      root.innerHTML = '<label id="gl">Gender</label><button id="g" type="button" aria-haspopup="listbox" aria-labelledby="gl">Select</button>';
      const b = root.querySelector("#g");
      b.addEventListener("click", () => {
        const lb = doc.createElement("div");
        lb.setAttribute("role", "listbox"); lb.id = "pop";
        lb.innerHTML = '<div role="option">Male</div><div role="option">Female</div><div role="option">Decline to self-identify</div>';
        root.appendChild(lb);
      });
      b.addEventListener("keydown", (e) => { if (e.key === "Escape") { const p = root.querySelector("#pop"); if (p) p.remove(); } });
    };
    const m = document.querySelector("main");
    const outer = document.createElement("div"); m.appendChild(outer);
    const inner = document.createElement("div");
    outer.attachShadow({ mode: "open" }).appendChild(inner);
    make(document, inner.attachShadow({ mode: "open" }));
    const f = document.createElement("iframe");
    f.srcdoc = "<!doctype html><body><div id='box'></div></body>";
    f.style.cssText = "width:400px;height:300px";
    f.onload = () => make(f.contentDocument, f.contentDocument.getElementById("box"));
    m.appendChild(f);
  });
  await page.waitForTimeout(300);
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const both = indices(v.text).filter((i) => /<combobox label="Gender"/.test(line(v.text, i)));
  assert.strictEqual(both.length, 2, v.text);
  let r = await readLikeShell(page, both[0], () => page.click("#g >> nth=0"));       // nested shadow root
  assert.deepStrictEqual(r.read.options, ["Male", "Female", "Decline to self-identify"]);
  assert.ok(!r.open);
  const frame = page.frames().find((f) => f !== page.mainFrame());
  r = await readLikeShell(page, both[1], () => frame.click("#g"));
  assert.deepStrictEqual(r.read.options, ["Male", "Female", "Decline to self-identify"]);
  assert.ok(!r.open);
  await page.close();
});

test("a form in a modal dialog stays open: one Escape closes only the dropdown, and the other answers stay", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(() => {
    document.body.insertAdjacentHTML("beforeend", `
      <div id="dlg" role="dialog" aria-modal="true" style="position:fixed;top:20px;left:20px;background:#fff;border:1px solid #333;padding:10px">
        <div class="field"><label id="gl">Gender</label><button id="g" type="button" aria-haspopup="listbox" aria-labelledby="gl" aria-expanded="false">Select</button></div>
        <div class="field"><label for="country">Country</label><select id="country"><option value="">Select...</option><option selected>Canada</option></select></div>
        <div class="field"><label id="nl">Notice period</label><button id="n" type="button" aria-haspopup="listbox" aria-labelledby="nl">Select</button></div>
      </div>`);
    // Radix-style layers: one document capture Escape listener dismisses the TOP layer only
    const layers = [{ close: () => document.getElementById("dlg").remove() }];
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && layers.length) layers.pop().close(); }, true);
    const g = document.getElementById("g");
    g.addEventListener("click", () => {
      const lb = document.createElement("div");
      lb.setAttribute("role", "listbox"); lb.id = "glist";
      lb.innerHTML = '<div role="option">Male</div><div role="option">Female</div>';
      document.body.appendChild(lb);
      g.setAttribute("aria-expanded", "true");
      layers.push({ close: () => { lb.remove(); g.setAttribute("aria-expanded", "false"); } });
    });
  });
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const gi = indexOf(v.text, /<combobox label="Gender"/), ni = indexOf(v.text, /<combobox label="Notice period"/);
  assert.ok(gi && ni, v.text);
  let r = await readLikeShell(page, gi, () => page.click("#g"));
  assert.deepStrictEqual(r.read.options, ["Male", "Female"]);
  assert.ok(r.escapes === 1 && !r.open);
  assert.ok(await page.evaluate(() => !!document.getElementById("dlg")), "the dialog is still open");
  assert.strictEqual(await page.evaluate(`window.TailorAgent.closePopup(${gi}, "escape")`), true);
  assert.ok(await page.evaluate(() => !!document.getElementById("dlg")), "closePopup sent nothing to a closed popup");
  r = await readLikeShell(page, ni, () => page.click("#n"));      // opens nothing: no key at all
  assert.ok(r.escapes === 0 && !r.read.options.length);
  assert.ok(await page.evaluate(() => !!document.getElementById("dlg") && document.getElementById("country").value === "Canada"));
  await page.close();
});

test("a self-identification answer never leaves the page: answered, never which choice", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(() => {
    document.body.insertAdjacentHTML("beforeend", `
      <form id="eeo">
        <label for="gen">Gender</label><select id="gen"><option value="">Select...</option><option selected>Male</option><option>Female</option></select>
        <label for="eth">Ethnicity</label><select id="eth"><option value="">Select...</option><option selected>Asian (Not Hispanic or Latino) (United States of America)</option></select>
        <fieldset><legend>Veteran Status</legend>
          <label><input type="radio" name="vet" value="n"> I am not a protected veteran</label>
          <label><input type="radio" name="vet" value="p" checked> I identify as one or more of the classifications of protected veterans</label></fieldset>
        <fieldset><legend>Disability Status</legend>
          <label><input type="checkbox" name="dis" checked> Yes, I have a disability, or have had one in the past</label>
          <label><input type="checkbox" name="dis"> No, I do not have a disability</label></fieldset>
        <label id="racel">Race</label><button id="race" type="button" aria-haspopup="listbox" aria-labelledby="racel">Black or African American</button>
        <label for="cty">Country</label><select id="cty"><option value="">Select...</option><option selected>Canada</option></select>
      </form>`);
  });
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  // the choices are the site's list (first="..."); the CHOICE made is never printed
  const self = v.text.split("\n").filter((l) => /label="(Gender|Ethnicity|Veteran Status|Race)"|label="Yes, I have a disability/.test(l));
  for (const word of ['value="Male"', 'value="Asian', "checked=true", "checked=false", 'value="Black']) {
    assert.ok(!self.some((l) => l.includes(word)), word + " leaked: " + self.join("\n"));
  }
  assert.ok(/<select label="Gender" value="\(filled\)" options=3/.test(v.text), v.text);
  assert.ok(/<select label="Ethnicity" value="\(filled\)"/.test(v.text), v.text);
  assert.ok(/<radio label="Veteran Status" option="I am not a protected veteran" checked=hidden answered=true/.test(v.text), v.text);
  // a box of a group says whether the GROUP is answered (so a filled group is not re-asked), never which box
  assert.ok(/<checkbox label="Yes, I have a disability, or have had one in the past" checked=hidden answered=true/.test(v.text), v.text);
  assert.ok(/<checkbox label="No, I do not have a disability" checked=hidden answered=true/.test(v.text), v.text);
  assert.ok(/<combobox label="Race" value="\(filled\)"/.test(v.text) || !/label="Race"/.test(v.text), v.text);
  assert.ok(/<select label="Country" value="Canada"/.test(v.text), "a site's ordinary choice still shows");
  await page.close();
});

// ------------------------------------------------------------------ Phenom (Lam Research, 2026-10-30)
// tests/fixtures/forms/phenom_lam.html: the real widget markup and choices. Its dropdown ignores
// Escape, blur and a second click, and its open list covers the next field, so the shell's old
// read took Gender's list as Ethnicity's choices and the saved answers never fitted.
const realClick = async (page, idx) => {
  const r = await page.evaluate(`(() => { const el = window.TailorAgent.element(${idx}); if (!el) return null;
    el.scrollIntoView({ block: "center" }); const b = el.getBoundingClientRect(); return { x: b.left + b.width / 2, y: b.top + b.height / 2 }; })()`);
  assert.ok(r, "no element " + idx);
  await page.mouse.click(r.x, r.y);
};
// The shell's dropdownOptions + closeDropdown, in order: close any other open one, mark, a
// trusted click, read; then one real Escape, blur, and the press from outside, each only while
// the popup is still open.
async function readLikeShellPhenom(page, idx) {
  await page.evaluate(`window.TailorAgent.closeOthers(${idx})`);
  const was = await page.evaluate(`window.TailorAgent.popupOpen(${idx})`);
  await page.evaluate(`window.TailorAgent.markPopups(${idx}, ${!was})`);
  if (!was) await realClick(page, idx);
  await page.waitForTimeout(150);
  const read = await page.evaluate(`window.TailorAgent.popupOptions(${idx})`);
  const steps = [];
  const isOpen = () => page.evaluate(`window.TailorAgent.popupOpen(${idx})`);
  if (await isOpen()) { await page.keyboard.press("Escape"); steps.push("escape"); }
  if (await isOpen()) { await page.evaluate(`window.TailorAgent.closePopup(${idx}, "blur")`); steps.push("blur"); }
  if (await isOpen()) { await page.evaluate(`window.TailorAgent.closePopup(${idx}, "outside")`); steps.push("outside"); }
  return { read, steps, open: await isOpen() };
}
const LAM = {
  Gender: ["Choose not to disclose", "Male", "Female"],
  Ethnicity: ["American Indian/Alaskan Native", "Asian", "Black or African American", "Choose not to disclose",
              "Hispanic or Latino", "Native Hawaiian or Other Pacific Island", "Two or More Races", "White"],
  Disability: ["Choose not to disclose", "No", "Yes"],
  Veteran: ["Choose not to disclose", "No", "Yes"],
};

test("Phenom (Lam): each dropdown read in a row gives its OWN choices, and one that ignores Escape and blur is pressed shut", async ({ open }) => {
  const page = await open("phenom_lam.html");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  for (const label of ["Gender", "Ethnicity", "Disability", "Veteran"]) {
    const i = indexOf(v.text, new RegExp(`<combobox label="${label}" value="" expanded=false required>`));
    assert.ok(i, label + "\n" + v.text);
    const r = await readLikeShellPhenom(page, i);
    assert.deepStrictEqual(r.read.options, LAM[label], label);
    assert.deepStrictEqual(r.steps, ["escape", "blur", "outside"], label + " ignores Escape and blur");
    assert.strictEqual(r.open, false, label + " is closed again");
  }
  // reading never picked anything, and nothing is left open over the next field
  assert.deepStrictEqual(await page.evaluate(() => Array.from(document.querySelectorAll("input[role=combobox]")).map((x) => x.value)),
                         ["", "", "", ""]);
  assert.strictEqual(await page.evaluate(() => document.querySelectorAll("[role=listbox]").length), 0);
  await page.close();
});

test("Phenom (Lam): another control's still-open list is never this one's choices, and closeOthers presses it shut", async ({ open }) => {
  const page = await open("phenom_lam.html");
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const gi = indexOf(v.text, /<combobox label="Gender"/), ei = indexOf(v.text, /<combobox label="Ethnicity"/);
  await realClick(page, gi);                          // Gender's list opens (and stays open)
  await page.evaluate(`window.TailorAgent.markPopups(${ei}, true)`);
  // Gender's list re-renders (new nodes), the way it did under the shell's click on Ethnicity
  await page.evaluate(() => { const l = document.getElementById("list-17"); l.replaceWith(l.cloneNode(true)); });
  const r = await page.evaluate(`window.TailorAgent.popupOptions(${ei})`);
  assert.deepStrictEqual(r.options, [], "Gender's list read as Ethnicity's: " + JSON.stringify(r));
  assert.ok(await page.evaluate(`window.TailorAgent.closeOthers(${ei})`) >= 1);
  assert.strictEqual(await page.evaluate(() => document.querySelectorAll("#list-17").length), 0);
  await page.close();
});

test("Phenom (Lam): the shell picks a saved choice with real clicks, checks it stuck, and the view says only (filled)", async ({ open }) => {
  const page = await open("phenom_lam.html");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  let v = await page.evaluate("window.TailorAgent.view()");
  const want = { Gender: "Male", Ethnicity: "Black or African American", Disability: "No", Veteran: "No" };
  for (const [label, option] of Object.entries(want)) {
    const i = indexOf(v.text, new RegExp(`<combobox label="${label}"`));
    await page.evaluate(`window.TailorAgent.closeOthers(${i})`);
    await page.evaluate(`window.TailorAgent.markPopups(${i}, true)`);
    await realClick(page, i);
    await page.waitForTimeout(100);
    const oi = await page.evaluate(`window.TailorAgent.optionIndex(${i}, ${JSON.stringify(option)})`);
    assert.ok(oi, label + ": no option index for its saved choice");
    await realClick(page, oi);
    await page.waitForTimeout(100);
    assert.strictEqual(await page.evaluate(`window.TailorAgent.chosenIs(${i}, ${JSON.stringify(option)})`), true, label);
    assert.strictEqual(await page.evaluate(`window.TailorAgent.chosenIs(${i}, "Female")`), false, label);
  }
  assert.strictEqual((await page.evaluate("fxLog()")).filter((x) => x.startsWith("pick:")).length, 4);
  v = await page.evaluate("window.TailorAgent.view()");
  for (const label of Object.keys(want)) assert.ok(new RegExp(`<combobox label="${label}" value="\\(filled\\)"`).test(v.text), label + "\n" + v.text);
  for (const word of ['value="Male"', 'value="Black', 'value="No"']) assert.ok(!v.text.includes(word), word + " leaked");
  // a near miss is never an option index ("Man" is not "Male")
  const gi = indexOf(v.text, /<combobox label="Gender"/);
  await realClick(page, gi);
  await page.waitForTimeout(100);
  assert.strictEqual(await page.evaluate(`window.TailorAgent.optionIndex(${gi}, "Man")`), null);
  await page.close();
});

test("Phenom (Lam): the engine reads each list as its own and drives the dropdown (no stale list from the one before)", async ({ open }) => {
  const page = await open("phenom_lam.html");
  await page.evaluate(CORE);
  const fields = await page.evaluate(() => window.TailorAutofill.extractFormAsync({ onlyEmpty: true }));
  for (const [label, opts] of Object.entries(LAM)) {
    const f = fields.find((x) => x.label === label);
    assert.ok(f, label + " " + JSON.stringify(fields.map((x) => x.label)));
    assert.deepStrictEqual(f.options, opts, label);
  }
  assert.strictEqual(await page.evaluate(() => document.querySelectorAll("[role=listbox]").length), 0, "every list closed again");
  const g = fields.find((x) => x.label === "Gender"), d = fields.find((x) => x.label === "Disability");
  const rep = await page.evaluate((ops) => window.TailorAutofill.applyOpsAsync(ops),
                                  [{ op: "select", ref: g.ref, text: "Male" }, { op: "select", ref: d.ref, text: "No" }]);
  assert.strictEqual(rep.filled, 2, JSON.stringify(rep));
  assert.deepStrictEqual(await page.evaluate(() => [document.querySelector("#Job_Specific_Information_q_custGender input").value,
                                                     document.querySelector("#Job_Specific_Information_disability input").value]),
                         ["Male", "No"]);
  await page.close();
});

test("terms: Lam's Data Privacy Agreement is found with its I Acknowledge button only once it opens; cookies, marketing and optional boxes never", async ({ open }) => {
  const page = await open("phenom_lam.html");
  await page.evaluate(AGENT);
  await page.evaluate("window.TailorAgent.view()");
  // closed (visibility hidden, opacity 0, like Lam's): nothing to accept; the job alerts box is marketing
  assert.deepStrictEqual(await page.evaluate("window.TailorAgent.termsItems()"), []);
  assert.deepStrictEqual(await page.evaluate("window.TailorAgent.termsItems(true)"), []);
  await page.evaluate("fxOpenPrivacy()");
  const items = await page.evaluate("window.TailorAgent.termsItems()");
  assert.strictEqual(items.length, 1, JSON.stringify(items));
  assert.strictEqual(items[0].kind, "modal");
  assert.strictEqual(items[0].title, "Data Privacy Agreement");
  assert.strictEqual(items[0].button, "I Acknowledge");
  assert.strictEqual(await page.evaluate(`window.TailorAgent.element(${items[0].index}).id`), "confirmUploadResume");
  await realClick(page, items[0].index);
  assert.strictEqual(await page.evaluate("window.fxAck"), true);
  assert.ok((await page.evaluate("fxLog()")).includes("privacy-ack"));
  assert.deepStrictEqual(await page.evaluate("window.TailorAgent.termsItems()"), []);
  // a cookie banner (even one titled "Privacy Notice") and a marketing dialog are not terms
  await page.evaluate(() => document.body.insertAdjacentHTML("beforeend",
    '<div role="dialog" aria-modal="true" aria-label="Privacy Notice" id="ck"><p>We use cookies to improve your visit.</p><button>I Accept</button></div>'
    + '<div role="dialog" aria-modal="true" aria-labelledby="tc" id="mk"><h2 id="tc">Join our talent community</h2><p>Get job alerts and agree to our privacy policy.</p><button>I Agree</button></div>'));
  assert.deepStrictEqual(await page.evaluate("window.TailorAgent.termsItems()"), []);
  await page.evaluate(() => { document.getElementById("ck").remove(); document.getElementById("mk").remove(); });
  // a required "I have read and agree to the privacy notice" box: yes; an optional one only when loose
  await page.evaluate(() => document.getElementById("next").insertAdjacentHTML("beforebegin",
    '<div class="field-3Ez-x"><input type="checkbox" id="pn" required><label for="pn">I have read and agree to the Job Application Data Privacy Notice *</label></div>'
    + '<div class="field-3Ez-x"><input type="checkbox" id="tou"><label for="tou">I accept the Terms of Use</label></div>'));
  let boxes = await page.evaluate("window.TailorAgent.termsItems()");
  assert.deepStrictEqual(boxes.map((b) => [b.kind, b.title]), [["checkbox", "Job Application Data Privacy Notice"]], JSON.stringify(boxes));
  boxes = await page.evaluate("window.TailorAgent.termsItems(true)");
  assert.deepStrictEqual(boxes.map((b) => b.title), ["Job Application Data Privacy Notice", "Terms of Use"]);
  await page.close();
});

test("a link to a privacy policy or notice is never followed (it opens a document or a dialog, not the next step)", async ({ open }) => {
  const page = await open("phenom_lam.html");
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const pi = indexOf(v.text, /<a>Privacy Policy<\/a>/);
  assert.ok(pi, v.text);
  const r = await page.evaluate(`window.TailorAgent.act({ op: "click", index: ${pi} })`);
  assert.strictEqual(r.ok, false);
  assert.ok(/^policy-link/.test(r.note), r.note);
  assert.ok(!(await page.evaluate("fxLog()")).includes("policy-link"));
  const ni = indexOf(v.text, /<button>Next<\/button>/);
  assert.strictEqual((await page.evaluate(`window.TailorAgent.act({ op: "click", index: ${ni} })`)).ok, true, "an ordinary button still clicks");
  await page.close();
});

test("mock ATS ?phenom=1: the resume opens the Data Privacy Agreement, and the four Phenom disclosures read and pick in a row", async ({ open }) => {
  const page = await open("mock_ats.html", "?phenom=1");
  await page.evaluate("fxGo('contact')");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  await page.evaluate("window.TailorAgent.view()");
  assert.deepStrictEqual(await page.evaluate("window.TailorAgent.termsItems()"), []);
  await page.setInputFiles("#cv", { name: "cv.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n%%EOF\n") });
  const items = await page.evaluate("window.TailorAgent.termsItems()");
  assert.deepStrictEqual(items.map((x) => [x.kind, x.title, x.button]), [["modal", "Data Privacy Agreement", "I Acknowledge"]]);
  await realClick(page, items[0].index);
  assert.ok((await page.evaluate("fxLog()")).includes("privacy-ack"));
  await page.evaluate("fxGo('disclosures')");
  const v = await page.evaluate("window.TailorAgent.view()");
  const want = { Gender: "Male", Ethnicity: "Black or African American", Disability: "No", Veteran: "No" };
  for (const label of Object.keys(want)) {
    const i = indexOf(v.text, new RegExp(`<combobox label="${label}"`));
    const r = await readLikeShellPhenom(page, i);
    assert.ok(r.read.options.includes(want[label]) && !r.open, label + " " + JSON.stringify(r));
  }
  for (const [label, option] of Object.entries(want)) {
    const i = indexOf(v.text, new RegExp(`<combobox label="${label}"`));
    await page.evaluate(`window.TailorAgent.closeOthers(${i})`);
    await page.evaluate(`window.TailorAgent.markPopups(${i}, true)`);
    await realClick(page, i);
    await page.waitForTimeout(100);
    await realClick(page, await page.evaluate(`window.TailorAgent.optionIndex(${i}, ${JSON.stringify(option)})`));
    assert.strictEqual(await page.evaluate(`window.TailorAgent.chosenIs(${i}, ${JSON.stringify(option)})`), true, label);
  }
  await page.click("#next");
  assert.ok((await page.evaluate("fxLog()")).includes("eeo:4"), JSON.stringify(await page.evaluate("fxLog()")));
  await page.close();
});

// ------------------------------------------------------------------ runner
(async () => {
  const { chromium } = findPlaywright();
  const a = await serve(FIXTURES), b = await serve(FIXTURES);
  const A = "http://127.0.0.1:" + a.address().port, B = b.address().port;
  const browser = await chromium.launch({ args: ["--site-per-process"] });
  const ctx = {
    B,
    open: async (fixture, query) => {
      const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
      await page.goto(A + "/" + fixture + (query || ""));
      await page.waitForLoadState("load");
      await page.waitForTimeout(100);
      return page;
    },
  };
  let passed = 0, failed = 0;
  const only = process.argv[2];
  try {
    for (const t of tests) {
      if (only && !t.name.includes(only)) continue;
      try { await t.fn(ctx); passed++; console.log("ok   " + t.name); }
      catch (e) { failed++; console.log("FAIL " + t.name + "\n     " + String(e && e.stack || e).split("\n").slice(0, 6).join("\n     ")); }
    }
  } finally {
    await browser.close();
    a.close(); b.close();
  }
  console.log(`${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
