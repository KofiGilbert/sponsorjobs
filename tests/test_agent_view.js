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
  assert.ok(line(w.text, sel).includes('value="I do not wish to self-identify"'), line(w.text, sel));
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
