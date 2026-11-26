// The apply agent's page view and in-page actions (extension/agent_view.js) on the local form
// fixtures in tests/fixtures/forms/, in a real headless Chromium (docs/apply-agent.md, section
// 11). Never touches a real site: two local static servers (the second is the "other origin"
// an iCIMS-style iframe is served from). Run directly (node tests/test_agent_view.js) or
// through tests/test_agent_view.py, which is how pytest runs it.
//
// What it pins: indices in document order from indexBase; `*` on an element that was not in
// the previous view; no personal VALUE in value= (a filled email reads "(filled)", a password
// never shows its contents); select options count and first four; a file input at opacity 0
// still listed; the submit guard; a covered element NOT clicked (the step names the cover); the 12000
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
    assert.ok(v.text.trim().endsWith("[End of page]") || v.text.trim().endsWith("[End of view]"), "ends with the end marker");
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
  // The anti-spam trap (Barclays' Workday, 2026-11-02: "This input is for robots only") is never
  // listed, so the model can never type into it; the engine never fills it either.
  assert.ok(!/robots only/.test(a.text), "the honeypot is left out of the view");
  assert.strictEqual(await p2.evaluate(`[...document.querySelectorAll("input")].filter((x) => window.TailorAgent.honeypotField(x)).length >= 1`), true);
  await p2.close();
});

test("Workday's live announcer is not an error; a real error still is (2026-11-02)", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(AGENT);
  await page.evaluate(`(() => {
    const live = document.createElement("div"); live.setAttribute("aria-live", "polite"); live.setAttribute("role", "alert");
    live.textContent = "Research Analyst page is loaded"; document.body.prepend(live);
    const st = document.createElement("div"); st.setAttribute("role", "status"); st.textContent = "3 results found"; document.body.appendChild(st);
    const err = document.createElement("div"); err.className = "error-message"; err.textContent = "The field Email is required"; document.body.appendChild(err);
  })()`);
  const v = await page.evaluate("window.TailorAgent.view()");
  assert.ok(!/page is loaded/.test(v.text.split("\n").filter((l) => l.startsWith("Error:")).join(" ")), v.text.slice(0, 400));
  assert.ok(/Error: "The field Email is required"/.test(v.text), "a real error stays");
  const errs = await page.evaluate("window.TailorAgent.readErrors()");
  assert.ok(errs.includes("The field Email is required") && !errs.some((e) => /page is loaded|results found/.test(e)), JSON.stringify(errs));
  await page.close();
});

test("a text box parked off the canvas or labelled for robots is never in the view", async ({ open }) => {
  const page = await open("job_page.html");
  await page.evaluate(AGENT);
  await page.evaluate(`(() => {
    const f = document.createElement("form"); document.body.appendChild(f);
    f.innerHTML = '<label for="em2">Email Address</label><input id="em2" type="text" required>'
      + '<label for="hp">Enter website. This input is for robots only, do not enter if you are human.</label><input id="hp" type="text">'
      + '<input id="off" name="website" type="text" style="position:absolute;left:-9999px" tabindex="-1">';
  })()`);
  const v = await page.evaluate("window.TailorAgent.view()");
  assert.ok(/label="Email Address"/.test(v.text), "the real box stays");
  assert.ok(!/robots only/.test(v.text) && !/label="website"/i.test(v.text), v.text.slice(-400));
  const chk = await page.evaluate("window.TailorAgent.pageCheck()");
  assert.ok(chk.required_empty.includes("Email Address") && chk.required_empty.length === 1, JSON.stringify(chk));
  await page.close();
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
  // (2026-11-26, owner decision) a self-identification choice reads like any other choice
  assert.ok(/value="I do not wish to self-identify"/i.test(line(w.text, sel)), line(w.text, sel));
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

// (2026-11-18) The first real Greenhouse run: the brain's own hands did not work on a react-select.
// Typing "did not filter" (four times), list_options read nothing from a closed list (five
// comboboxes), and a click on an option "did not select" (three times). Each of the agent's own
// ops now goes through the engine's machinery; these run the OPS, never the engine's fill.
const combo = (text, label) => indices(text).find((i) => line(text, i).includes(`<combobox label="${label}`));

test("the agent's type on a react-select opens it, filters, picks the best match and says what it picked", async ({ open }) => {
  const page = await open("react_select.html");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  // a static list
  const c = combo(v.text, "Country");
  const r = await page.evaluate(`window.TailorAgent.act({op: "type", index: ${c}, text: "United States"})`);
  assert.deepStrictEqual([r.ok, r.note, r.picked], [true, "picked", "United States"], JSON.stringify(r));
  assert.strictEqual((await page.evaluate("window.fxValues()")).country, "United States");
  // a search-as-you-type list (nothing shows until something is typed)
  const loc = combo(v.text, "Location (City)");
  const r2 = await page.evaluate(`window.TailorAgent.act({op: "type", index: ${loc}, text: "Chicago, Illinois"})`);
  assert.strictEqual(r2.ok, true, JSON.stringify(r2));
  assert.strictEqual((await page.evaluate("window.fxValues()")).location, "Chicago, Illinois, United States");
  // the list is closed again and no filter text is left behind
  assert.strictEqual(await page.evaluate(() => document.querySelectorAll(".select__menu").length), 0);
  // nothing matches: said plainly, with what the list holds, and the box left clean
  const src = combo(v.text, "How did you hear about this job?");
  const miss = await page.evaluate(`window.TailorAgent.act({op: "type", index: ${src}, text: "Billboard", answer: true})`);
  assert.strictEqual(miss.ok, false, JSON.stringify(miss));
  assert.ok(/^no result for what was typed; options: .*LinkedIn.*Referral.*Other/.test(miss.note), miss.note);
  assert.strictEqual((await page.evaluate("window.fxValues()")).source, null);
  assert.strictEqual(await page.evaluate(() => document.getElementById("question_103").value), "");
  // the brain's own type (no saved answer): nothing matches, so the list's own "Other", said so
  const other = await page.evaluate(`window.TailorAgent.act({op: "type", index: ${src}, text: "Billboard"})`);
  assert.strictEqual(other.ok, true, JSON.stringify(other));
  assert.ok(/Other choice/.test(other.note) && other.picked === "Other", JSON.stringify(other));
  await page.close();
});

test("list_options on a closed react-select opens it, reads every choice and closes it again", async ({ open }) => {
  const page = await open("react_select.html");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const c = combo(v.text, "Country");
  assert.ok(/expanded=false/.test(line(v.text, c)), line(v.text, c));
  const got = await page.evaluate(`window.TailorAgent.listOptions(${c})`);
  assert.deepStrictEqual(got.options, ["Canada", "Mexico", "United Kingdom", "United States", "United States Minor Outlying Islands"]);
  assert.strictEqual(await page.evaluate(() => document.querySelectorAll(".select__menu").length), 0, "closed again");
  assert.strictEqual((await page.evaluate("window.fxValues()")).country, null, "nothing was chosen by reading");
  // options with no role=option (react-select's own divs) are read too
  const g = await page.evaluate(`window.TailorAgent.listOptions(${combo(v.text, "Gender")})`);
  assert.deepStrictEqual(g.options, ["Male", "Female", "Non-binary", "Decline To Self Identify"]);
  // a native select or a plain box is not this tool's
  const first = indices(v.text).find((i) => /label="First Name/.test(line(v.text, i)));
  assert.strictEqual(await page.evaluate(`window.TailorAgent.listOptions(${first})`), null);
  await page.close();
});

test("a click on a role=option of an open react-select selects it, and says so", async ({ open }) => {
  const page = await open("react_select.html");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  let v = await page.evaluate("window.TailorAgent.view()");
  const q = combo(v.text, "Are you legally authorized to work");
  await page.evaluate(`window.TailorAgent.act({op: "click", index: ${q}})`);          // the brain opens the list
  v = await page.evaluate("window.TailorAgent.view()");
  const yes = indices(v.text).find((i) => /^\*?\[\d+\]<option[^>]*>Yes</.test(line(v.text, i)));
  assert.ok(yes, v.text);
  const r = await page.evaluate(`window.TailorAgent.act({op: "pick_option", index: ${yes}})`);
  assert.deepStrictEqual([r.ok, r.note, r.picked], [true, "picked", "Yes"], JSON.stringify(r));
  assert.strictEqual((await page.evaluate("window.fxValues()")).auth, "Yes");
  assert.strictEqual(await page.evaluate(() => document.querySelectorAll(".select__menu").length), 0);
  // something that is no option of any list: not this tool's (the shell then clicks it plainly)
  const btn = indices(v.text).find((i) => /Submit application/.test(line(v.text, i)));
  assert.strictEqual(await page.evaluate(`window.TailorAgent.act({op: "pick_option", index: ${btn}})`), null);
  await page.close();
});

test("a remote school search: the first distinctive words find it; a school not listed takes the list's Other", async ({ open }) => {
  const page = await open("react_select.html");
  await page.evaluate(CORE);
  await page.evaluate(() => {
    // thousands of schools behind a search: nothing shows until something is typed, and the
    // list holds the school under a shorter name than the resume gives it
    const names = ["Harlow State", "Harlow Community College", "Pinecrest College", "Other"];
    for (let i = 0; i < 3000; i++) names.push("Institute " + i);
    window.fx.school = window.FixtureWidgets.ReactSelect(document.getElementById("widgets"),
      { id: "school", label: "School *", required: true, async: true, delayMs: 120, options: names });
  });
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const s = combo(v.text, "School");
  assert.deepStrictEqual(await page.evaluate("window.TailorAutofill.agentQueries('Harlow State University of Applied Sciences')"),
    ["Harlow State University of Applied Sciences", "Harlow State University", "Harlow State", "Harlow"]);
  const r = await page.evaluate(`window.TailorAgent.act({op: "type", index: ${s}, text: "Harlow State University of Applied Sciences"})`);
  assert.deepStrictEqual([r.ok, r.picked], [true, "Harlow State"], JSON.stringify(r));
  assert.strictEqual((await page.evaluate("window.fx.school.value")), "Harlow State");
  // a school the list does not hold: the list's own "Other", and the result says so
  await page.evaluate(() => { document.querySelector("#school").closest(".field").remove();
    window.fx.school = window.FixtureWidgets.ReactSelect(document.getElementById("widgets"),
      { id: "school2", label: "School attended *", async: true, delayMs: 120, options: ["Pinecrest College", "Other"] }); });
  const v2 = await page.evaluate("window.TailorAgent.view()");
  const r2 = await page.evaluate(`window.TailorAgent.act({op: "type", index: ${combo(v2.text, "School attended")}, text: "Zennor Polytechnic"})`);
  assert.strictEqual(r2.ok, true, JSON.stringify(r2));
  assert.ok(r2.picked === "Other" && /nothing matched what was typed/.test(r2.note), JSON.stringify(r2));
  await page.close();
});

test("Greenhouse's live react-select (a slow location search, a required hidden input) through the agent's ops", async ({ open }) => {
  const page = await open("greenhouse_live.html?delay=900");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const c = combo(v.text, "Country");
  const lo = await page.evaluate(`window.TailorAgent.listOptions(${c})`);
  assert.deepStrictEqual(lo.options, ["Canada +1", "United Kingdom +44", "United States +1"]);
  const r = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${c}, option: "United States +1"})`);
  assert.strictEqual(r.ok, true, JSON.stringify(r));
  const loc = combo(v.text, "Location (City)");
  const r2 = await page.evaluate(`window.TailorAgent.act({op: "type", index: ${loc}, text: "Chicago, Illinois"})`);
  assert.deepStrictEqual([r2.ok, r2.picked], [true, "Chicago, Illinois, United States"], JSON.stringify(r2));
  const vals = await page.evaluate("window.fxValues()");
  assert.deepStrictEqual([vals.country, vals.location], ["United States +1", "Chicago, Illinois, United States"]);
  await page.close();
  // a search that answers with nothing: said, never a hang and never stray text in the box
  const p2 = await open("greenhouse_live.html?delay=300&empty=1");
  await p2.evaluate(CORE);
  await p2.evaluate(AGENT);
  const v2 = await p2.evaluate("window.TailorAgent.view()");
  const t0 = Date.now();
  const miss = await p2.evaluate(`window.TailorAgent.act({op: "type", index: ${combo(v2.text, "Location (City)")}, text: "Chicago, Illinois", answer: true})`);
  assert.strictEqual(miss.ok, false, JSON.stringify(miss));
  assert.ok(/^no result for what was typed/.test(miss.note), miss.note);
  assert.ok(Date.now() - t0 < 12000, "inside its budget: " + (Date.now() - t0) + " ms");
  assert.strictEqual(await p2.evaluate(() => document.getElementById("candidate-location").value), "");
  await p2.close();
});

test("a saved answer never lands on a choice that adds a qualifier (Phoenix Contact, 2026-11-05)", async ({ open }) => {
  const page = await open("react_select.html");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  await page.evaluate(() => {
    const s = document.createElement("select");
    s.id = "heard"; s.setAttribute("aria-label", "How did you hear about this specific opening?");
    for (const o of ["Select...", "College Job Board", "Company Website", "Employee Referral"]) {
      const e = document.createElement("option"); e.textContent = o; e.value = o === "Select..." ? "" : o; s.appendChild(e);
    }
    document.body.prepend(s);
  });
  const v = await page.evaluate("window.TailorAgent.view()");
  const sel = indices(v.text).find((i) => /<select label="How did you hear/.test(line(v.text, i)));
  assert.ok(sel, v.text);
  // the person's saved answer: no choice says the same and none names no source, so nothing is
  // picked and no list is offered ("Other" is not on this list yet)
  const miss = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${sel}, option: "Job board", answer: true})`);
  assert.strictEqual(miss.ok, false);
  assert.ok(/^no choice on this list says the same/.test(miss.note), miss.note);
  assert.strictEqual(await page.evaluate(() => document.getElementById("heard").value), "");
  await page.evaluate(() => { const e = document.createElement("option"); e.textContent = e.value = "Other"; document.getElementById("heard").appendChild(e); });
  // without the flag (the brain's own select), a miss still lists the choices
  const m2 = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${sel}, option: "Job board"})`);
  assert.ok(/^no such option; options: .*College Job Board/.test(m2.note), m2.note);
  // (2026-11-18) with "Other" on the list, a how-did-you-hear answer takes it by itself: never
  // "College Job Board", and never a question for the person
  const hit = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${sel}, option: "Job board", answer: true})`);
  assert.strictEqual(hit.ok, true, JSON.stringify(hit));
  assert.strictEqual(await page.evaluate(() => document.getElementById("heard").value), "Other");
  // any other question keeps the refusal, whatever the list holds
  await page.evaluate(() => { const s = document.getElementById("heard"); s.value = ""; s.setAttribute("aria-label", "Which team are you applying to?"); });
  const other = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${sel}, option: "Job board", answer: true})`);
  assert.strictEqual(other.ok, false, JSON.stringify(other));
  assert.ok(/^no choice on this list says the same/.test(other.note), other.note);
  assert.strictEqual(await page.evaluate(() => document.getElementById("heard").value), "");
  // (2026-11-18, the agent decides) the miss no longer says "do not pick one for them": it hands
  // the list's own choices back, and the brain's own pick of one of them is applied as asked
  assert.ok(/its options: .*College Job Board.*Company Website.*Other/.test(other.note), other.note);
  assert.ok(/answer protocol/.test(other.note) && !/do not pick one for them/.test(other.note), other.note);
  const own = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${sel}, option: "Company Website"})`);
  assert.strictEqual(own.ok, true, JSON.stringify(own));
  assert.strictEqual(await page.evaluate(() => document.getElementById("heard").value), "Company Website");
  // a react-select: "States" is not "United States" (the loose contains match took it)
  const c = indices(v.text).find((i) => /<combobox label="Country"/.test(line(v.text, i)));
  const r = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${c}, option: "States", answer: true})`);
  assert.strictEqual(r.ok, false, JSON.stringify(r));
  assert.notStrictEqual((await page.evaluate("window.fxValues()")).country, "United States");
  await page.close();
});

// ------------------------------------------------------------------ how did you hear (2026-11-18)
const SOURCE = "workday_source.html";
const srcQuery = (cats, label) => "?cats=" + encodeURIComponent(JSON.stringify(cats)) + (label ? "&label=" + encodeURIComponent(label) : "");
async function sourceBox(page) {
  const v = await page.evaluate("window.TailorAgent.view()");
  const i = indices(v.text).find((n) => /<combobox\b/.test(line(v.text, n)));
  assert.ok(i, v.text);
  return i;
}

test("how did you hear: the rule on the page is the rule in Python (the shared cases)", async ({ open }) => {
  const cases = JSON.parse(fs.readFileSync(path.join(ROOT, "tests", "fixtures", "heard_about_cases.json"), "utf8")).cases;
  assert.ok(cases.length >= 15);
  const page = await open(SOURCE);
  await page.evaluate(CORE);
  const got = await page.evaluate((cs) => cs.map((c) => window.TailorAutofill.heardChoices(c.answer, c.options)[0] || null), cases);
  cases.forEach((c, i) => assert.strictEqual(got[i], c.want, JSON.stringify(c) + " -> " + got[i]));
  assert.strictEqual(await page.evaluate(() => window.TailorAutofill.isHeardLabel("How Did You Hear About Us?")), true);
  assert.strictEqual(await page.evaluate(() => window.TailorAutofill.isHeardLabel("Which team are you applying to?")), false);
  // the education placeholder ("Master's degree", autoapply/commonsense.py EDU_WORDS) lands on the
  // list's choice for that level through the strict scorer, never a neighbour
  const edu = ["No High School Diploma", "High School Diploma or GED", "Some College, No Degree", "Associate Degree (AA, AS)",
               "Bachelor's Degree (BA, BS)", "Master's Degree (MA, MS)", "Doctorate (PhD)"];
  const best = await page.evaluate(([opts, words]) => words.map((w) => {
    const sc = window.TailorAutofill.answerScorer(w);
    return opts.map((o) => [sc(o.toLowerCase()), o]).filter((x) => x[0] > 0).sort((a, b) => b[0] - a[0]).map((x) => x[1]);
  }), [edu, ["Master's degree", "Bachelor's degree", "Associate degree", "High school diploma", "Doctorate"]]);
  assert.deepStrictEqual(best, [["Master's Degree (MA, MS)"], ["Bachelor's Degree (BA, BS)"], ["Associate Degree (AA, AS)"],
                                ["High School Diploma or GED"], ["Doctorate (PhD)"]]);
  await page.close();
});

test("how did you hear: Workday's two-level list, 'Job board' ends on 'Other Job Board' (engine fill, model action, plan op)", async ({ open }) => {
  // 1. the engine's own fill: no model step at all
  let page = await open(SOURCE);
  await page.evaluate(CORE);
  const out = await page.evaluate(() => window.TailorAutofill.fillWidgets({ answers: { hear_about_us: "Job board" } }));
  assert.strictEqual(out.filled, 1, JSON.stringify(out));
  assert.deepStrictEqual(await page.evaluate("window.fxValue()"), ["Other Job Board"]);
  assert.deepStrictEqual(await page.evaluate("window.fxOpened()"), ["Job Sites"]);
  assert.strictEqual(await page.evaluate("window.fxOpen()"), false);            // the list is closed again
  // filled once: a second pass leaves it alone
  await page.evaluate(() => window.TailorAutofill.fillWidgets({ answers: { hear_about_us: "Job board" } }));
  assert.deepStrictEqual(await page.evaluate("window.fxValue()"), ["Other Job Board"]);
  await page.close();

  // 2. the model's action: the saved answer typed into the list (the shell's select with answer)
  page = await open(SOURCE);
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  const box = await sourceBox(page);
  const r = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${box}, option: "Job board", answer: true})`);
  assert.strictEqual(r.ok, true, JSON.stringify(r));
  assert.deepStrictEqual(await page.evaluate("window.fxValue()"), ["Other Job Board"]);
  await page.close();

  // 3. the plan's op: Python read the first list and chose its board-like category
  page = await open(SOURCE);
  await page.evaluate(CORE);
  const fields = await page.evaluate(() => window.TailorAutofill.extractFormAsync({ onlyEmpty: true }));
  const f = fields.find((x) => /How Did You Hear/.test(x.label));
  assert.ok(f && f.options.includes("Job Sites"), JSON.stringify(fields));
  const rep = await page.evaluate((ref) => window.TailorAutofill.applyOpsAsync([{ ref, op: "select", text: "Job Sites", from: "library" }]), f.ref);
  assert.strictEqual(rep.filled, 1, JSON.stringify(rep));
  assert.deepStrictEqual(await page.evaluate("window.fxValue()"), ["Other Job Board"]);
  await page.close();
});

test("how did you hear: a category with nothing acceptable is left by the back arrow for 'Other'; nothing at all is still refused", async ({ open }) => {
  // the board-like category holds only sources the person did not give: back, then "Other"
  let page = await open(SOURCE, srcQuery({ "Job Sites": ["College Job Board", "Compliance_HireAHero", "Job Board: Indeed"], "Recruiter": ["Staffing Agency"], "Other": null }));
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  let box = await sourceBox(page);
  let r = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${box}, option: "Job board", answer: true})`);
  assert.strictEqual(r.ok, true, JSON.stringify(r));
  assert.deepStrictEqual(await page.evaluate("window.fxValue()"), ["Other"]);
  assert.deepStrictEqual(await page.evaluate("window.fxOpened()"), ["Job Sites"]);
  await page.close();

  // "Other" is itself a category: its own "Other"
  page = await open(SOURCE, srcQuery({ "Internet": ["College Job Board"], "Other": ["Radio", "Other"] }));
  await page.evaluate(CORE);
  const out = await page.evaluate(() => window.TailorAutofill.fillWidgets({ answers: { hear_about_us: "Job board" } }));
  assert.strictEqual(out.filled, 1, JSON.stringify(out));
  assert.deepStrictEqual(await page.evaluate("window.fxValue()"), ["Other"]);
  assert.deepStrictEqual(await page.evaluate("window.fxOpened()"), ["Internet", "Other"]);
  await page.close();

  // "College Job Board" alone, flat or under a category: refused, nothing picked, the list closed
  for (const cats of [{ "College Job Board": null, "Staffing Agency": null },
                      { "Job Sites": ["College Job Board", "Compliance_HireAHero"], "Recruiter": ["Staffing Agency"] }]) {
    page = await open(SOURCE, srcQuery(cats));
    await page.evaluate(CORE);
    await page.evaluate(AGENT);
    box = await sourceBox(page);
    r = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${box}, option: "Job board", answer: true})`);
    assert.strictEqual(r.ok, false, JSON.stringify(r));
    assert.ok(/^no choice on this list says the same/.test(r.note), r.note);
    assert.deepStrictEqual(await page.evaluate("window.fxValue()"), []);
    assert.strictEqual(await page.evaluate("window.fxOpen()"), false);
    const eng = await page.evaluate(() => window.TailorAutofill.fillWidgets({ answers: { hear_about_us: "Job board" } }));
    assert.strictEqual(eng.filled, 0, JSON.stringify(eng));
    assert.deepStrictEqual(await page.evaluate("window.fxValue()"), []);
    await page.close();
  }

  // another question on the very same widget and list: refused as before, no category is opened
  page = await open(SOURCE, srcQuery({ "Job Sites": ["Other Job Board"], "Other": null }, "Which office referred you?"));
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  box = await sourceBox(page);
  r = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${box}, option: "Job board", answer: true})`);
  assert.strictEqual(r.ok, false, JSON.stringify(r));
  assert.ok(/^no choice on this list says the same/.test(r.note), r.note);
  assert.deepStrictEqual(await page.evaluate("window.fxValue()"), []);
  assert.deepStrictEqual(await page.evaluate("window.fxOpened()"), []);
  await page.close();
});

test("how did you hear: the pick costs nothing on other dropdowns, stays in its budget, is not repeated, and leaves the list closed", async ({ open }) => {
  const PAY = { fields: { country: "United States" }, answers: { hear_about_us: "Job board" } };
  const stats = (page) => page.evaluate(() => window.TailorAutofill.heardStats());
  const timed = (page, expr) => page.evaluate(async (e) => { const t = performance.now(); const r = await eval(e); return { ms: performance.now() - t, r }; }, expr);
  const fill = "window.TailorAutofill.fillWidgets(" + JSON.stringify(PAY) + ")";

  // 1. pages with several dropdowns and no how-did-you-hear question: the pick never runs, for
  //    the engine's fill, the form read, or a plan op on every dropdown (a count, not a stopwatch)
  for (const fx of ["verify_widgets.html", "phenom_lam.html", "workday_experience.html", SOURCE + srcQuery({ "Job Sites": ["Other Job Board"], "Other": null }, "Which office referred you?")]) {
    const page = await open(fx);
    await page.evaluate(CORE);
    await page.evaluate(fill);
    const fields = await page.evaluate(() => window.TailorAutofill.extractFormAsync({ onlyEmpty: true }));
    const combos = fields.filter((f) => f.widget === "combobox");
    assert.ok(combos.length >= 1, fx + ": " + combos.length + " dropdowns");
    assert.ok(!combos.some((f) => /hear about/i.test(f.label)), fx);
    await page.evaluate((ops) => window.TailorAutofill.applyOpsAsync(ops), combos.slice(0, 3).map((f) => ({ ref: f.ref, op: "select", text: "Job board", from: "ai" })));
    assert.deepStrictEqual(await stats(page), { picks: 0, skipped: 0, ms: 0 }, fx);
    await page.close();
  }

  // 1b. pages with other dropdowns AND a how-did-you-hear one (react-select, Greenhouse): the
  //     pick runs for that one field only, once over three fill passes, inside its budget
  for (const fx of ["react_select.html", "greenhouse_live.html"]) {
    const page = await open(fx);
    await page.evaluate(CORE);
    for (let i = 0; i < 3; i++) await page.evaluate(fill);
    const st = await stats(page);
    assert.ok(st.picks <= 1, fx + " " + JSON.stringify(st));
    assert.ok(st.ms < 4500, fx + " " + JSON.stringify(st));
    await page.close();
  }

  // 2. the two-level list: one pick, well under the 4 s budget, the list closed
  let page = await open(SOURCE);
  await page.evaluate(CORE);
  let t = await timed(page, fill);
  assert.strictEqual(t.r.filled, 1, JSON.stringify(t));
  assert.ok(t.ms < 2000, "two-level pick took " + Math.round(t.ms) + " ms");
  assert.strictEqual((await stats(page)).picks, 1);
  assert.strictEqual(await page.evaluate("window.fxOpen()"), false);
  await page.close();

  // 3. a list that never opens: given up early, never the full budget, and not opened again on
  //    the next fill passes or by the plan's op for the same answer
  page = await open(SOURCE, "?never=1");
  await page.evaluate(CORE);
  t = await timed(page, fill);
  assert.strictEqual(t.r.filled, 0, JSON.stringify(t));
  assert.ok(t.ms < 2500, "a list that never opens took " + Math.round(t.ms) + " ms");
  const shows = await page.evaluate("window.fxShows()");
  for (let i = 0; i < 3; i++) {
    t = await timed(page, fill);
    assert.ok(t.ms < 500, "pass " + (i + 2) + " took " + Math.round(t.ms) + " ms");
  }
  const f3 = (await page.evaluate(() => window.TailorAutofill.extractFormAsync({ onlyEmpty: true }))).find((f) => /How Did You Hear/.test(f.label));
  const shows2 = await page.evaluate("window.fxShows()");                    // the form read opens it once, as on main
  t = await timed(page, "window.TailorAutofill.applyOpsAsync(" + JSON.stringify([{ ref: f3.ref, op: "select", text: "Job board", from: "library" }]) + ")");
  assert.ok(t.ms < 500, "the plan op took " + Math.round(t.ms) + " ms");
  assert.strictEqual(await page.evaluate("window.fxShows()"), shows2);
  let st = await stats(page);
  assert.strictEqual(st.picks, 1, JSON.stringify(st));
  assert.strictEqual(st.skipped, 4, JSON.stringify(st));
  assert.ok(shows <= 4, "opened " + shows + " times");
  await page.close();

  // 4. a list with no choice for the answer: refused once in well under the budget, closed, and
  //    the next passes do not open it again
  page = await open(SOURCE, srcQuery({ "Job Sites": ["College Job Board", "Compliance_HireAHero"], "Recruiter": ["Staffing Agency"] }));
  await page.evaluate(CORE);
  t = await timed(page, fill);
  assert.strictEqual(t.r.filled, 0, JSON.stringify(t));
  assert.ok(t.ms < 2500, "a refusal took " + Math.round(t.ms) + " ms");
  assert.strictEqual(await page.evaluate("window.fxOpen()"), false);
  const n4 = await page.evaluate("window.fxShows()");
  for (let i = 0; i < 3; i++) await page.evaluate(fill);
  assert.strictEqual(await page.evaluate("window.fxShows()"), n4);
  st = await stats(page);
  assert.strictEqual(st.picks, 1, JSON.stringify(st));
  assert.strictEqual(st.skipped, 3, JSON.stringify(st));
  assert.ok(st.ms < 4000, JSON.stringify(st));
  await page.close();
});

// ------------------------------------------------------------------ the cost of a fill (2026-11-18)
// Real long forms ran 30 to 45 s per fill and were cut by a 40 s stopwatch. The engine now
// remembers what a dropdown already told it, stops waiting on a settled list, keeps to a budget
// and reports a heartbeat. Bounds here are generous (several times the measured value).
const PAYLOAD = { loaded: true,
  fields: { first_name: "Sam", last_name: "Example", name: "Sam Example", email: "sam@example.com", phone: "555-010-0000",
            city: "Springfield", state: "Illinois", country: "United States", zip: "62701", address: "1 Main St" },
  answers: { years_experience: "5" }, screening: { work_authorized: "Yes", needs_sponsorship: "No" },
  eeo: { decline: true }, history: [], education: [], skills: [] };
const timedEval = (page, fn, arg) => page.evaluate(async ([src, a]) => {
  const t = performance.now(); const r = await (0, eval)("(" + src + ")")(a); return { ms: Math.round(performance.now() - t), r };
}, [fn.toString(), arg]);
const opens = (page) => page.evaluate(() => window.TailorAutofill.openCounts().map((x) => x.n));

test("fill cost: a second pass over an unchanged page costs next to nothing, and no dropdown is opened again", async ({ open }) => {
  for (const [fx, firstMax] of [["react_select.html", 6000], ["greenhouse_live.html", 12000]]) {
    const page = await open(fx);
    await page.evaluate(CORE);
    const p1 = await timedEval(page, (pl) => window.TailorAutofill.fillAsync(pl), PAYLOAD);
    assert.ok(p1.r.ok && p1.r.filled > 0 && p1.r.timed_out === false, fx + " " + JSON.stringify(p1.r).slice(0, 200));
    assert.ok(p1.ms < firstMax, fx + " pass 1 took " + p1.ms + " ms");
    const x1 = await timedEval(page, () => window.TailorAutofill.extractFormAsync({ onlyEmpty: true }));
    assert.ok(x1.ms < 8500, fx + " form read took " + x1.ms + " ms");
    const before = await opens(page);
    assert.ok(Math.max(...before) <= 2, fx + ": at most one pick and one read per dropdown " + JSON.stringify(before));
    // the agent fills again after every step: nothing on the page changed
    const p2 = await timedEval(page, (pl) => window.TailorAutofill.fillAsync(pl), PAYLOAD);
    assert.ok(p2.ms < 1500, fx + " pass 2 took " + p2.ms + " ms");
    assert.ok(p2.r.from_memory >= 1 && p2.r.opens === 0, fx + " pass 2 came from memory " + JSON.stringify({ m: p2.r.from_memory, o: p2.r.opens }));
    const x2 = await timedEval(page, () => window.TailorAutofill.extractFormAsync({ onlyEmpty: true }));
    assert.ok(x2.ms < 1500, fx + " second form read took " + x2.ms + " ms");
    const p3 = await timedEval(page, (pl) => window.TailorAutofill.fillAsync(pl), PAYLOAD);
    assert.ok(p3.ms < 1500, fx + " pass 3 took " + p3.ms + " ms");
    const after = await opens(page);
    // the one dropdown whose list only shows after typing is read once more by the form read (it
    // holds no list to remember); nothing else is opened again
    const again = after.reduce((n, v, i) => n + (v - before[i]), 0);
    assert.ok(again <= 1, fx + ": opened again " + again + " times " + JSON.stringify(before) + " -> " + JSON.stringify(after));
    assert.strictEqual(await page.evaluate(() => Array.from(document.querySelectorAll("[role='option']")).filter((o) => o.getBoundingClientRect().height > 0).length), 0, fx + ": every list is closed");
    await page.close();
  }
});

test("fill cost: a miss is remembered per field and wanted answer; a different answer or a changed form is tried again", async ({ open }) => {
  const page = await open("react_select.html");
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  const pick = (text) => timedEval(page, async (t) => {
    const E = window.TailorAutofill;
    const el = E.comboboxes().find((c) => !E.comboValue(c) && /country/i.test(c.getAttribute("aria-label") || (c.labels && c.labels[0] && c.labels[0].textContent) || c.id || ""))
            || E.comboboxes().find((c) => !E.comboValue(c));
    window.__el = window.__el || el;
    const r = await E.pickCombo(window.__el, { text: t, score: E.answerScorer(t) });
    return { why: r.why || "ok", skipped: !!r.skipped, n: E.openCounts()[E.comboboxes().indexOf(window.__el)].n };
  }, text);
  const a = await pick("Narnia");
  assert.strictEqual(a.r.why, "no-match", JSON.stringify(a));
  assert.ok(a.ms < 2500, "a settled list is not waited out: " + a.ms + " ms");      // was 2.5 s + 2 typed searches
  const b = await pick("Narnia");
  assert.ok(b.r.skipped && b.r.n === a.r.n && b.ms < 200, "the same miss is not tried again " + JSON.stringify(b));
  const c = await pick("Atlantis");                                               // a different wanted answer
  assert.ok(!c.r.skipped && c.r.n === a.r.n + 1, "a different answer is tried " + JSON.stringify(c));
  const d = await pick("United States");                                          // and one the list has is still picked
  assert.strictEqual(d.r.why, "ok", JSON.stringify(d));
  await page.close();
});

test("fill cost: past its budget the engine starts nothing new and returns what it filled; the heartbeat moves while it works", async ({ open }) => {
  // Greenhouse's slow location search (3.2 s per query): a 600 ms budget cuts the fill short
  let page = await open("greenhouse_live.html");
  await page.evaluate(CORE);
  const cut = await timedEval(page, (pl) => window.TailorAutofill.fillAsync(pl, { budgetMs: 600 }), PAYLOAD);
  assert.strictEqual(cut.r.timed_out, true, JSON.stringify(cut.r).slice(0, 200));
  assert.ok(cut.r.ok && cut.r.filled >= 2, "what was filled stands " + cut.r.filled);
  assert.ok(cut.ms < 2500, "it returned soon after its budget: " + cut.ms + " ms");
  assert.strictEqual(await page.evaluate(() => Array.from(document.querySelectorAll("[role='option']")).filter((o) => o.getBoundingClientRect().height > 0).length), 0, "the list is closed");
  // cut short is not a miss: with time, the same field is tried again
  const full = await timedEval(page, (pl) => window.TailorAutofill.fillAsync(pl), PAYLOAD);
  assert.strictEqual(full.r.timed_out, false);
  assert.ok(full.r.opens >= 1, "the cut field was tried again " + JSON.stringify({ o: full.r.opens, m: full.r.from_memory }));
  await page.close();

  // the heartbeat: read while the fill runs, it moves; after the fill it rests
  page = await open("greenhouse_live.html");
  await page.evaluate(CORE);
  const idle = await page.evaluate(() => window.TailorAutofill.fillProgress());
  assert.strictEqual(idle.done, 0);
  const running = page.evaluate((pl) => window.TailorAutofill.fillAsync(pl), PAYLOAD);
  await page.waitForTimeout(300);
  const mid = await page.evaluate(() => window.TailorAutofill.fillProgress());
  assert.strictEqual(mid.phase, "engine", JSON.stringify(mid));
  assert.ok(mid.at > 0 && mid.done >= 1, "fields done so far " + JSON.stringify(mid));
  await running;
  const end = await page.evaluate(() => window.TailorAutofill.fillProgress());
  assert.ok(end.phase === "" && end.done >= mid.done && end.at >= mid.at, JSON.stringify(end));
  await page.close();

  // the plan's ops under a budget: the ones not reached are counted, the rest stands
  page = await open("react_select.html");
  await page.evaluate(CORE);
  const fields = (await page.evaluate(() => window.TailorAutofill.extractFormAsync({ onlyEmpty: true }))).filter((f) => f.widget === "combobox");
  assert.ok(fields.length >= 3, "dropdowns: " + fields.length);
  const ops = fields.map((f) => ({ ref: f.ref, op: "select", text: "Narnia", from: "ai" }));
  const rep = await timedEval(page, (o) => window.TailorAutofill.applyOpsAsync(o, { budgetMs: 1 }), ops);
  assert.strictEqual(rep.r.timed_out, true, JSON.stringify(rep.r));
  assert.ok(rep.r.pending >= fields.length - 1 && rep.ms < 1500, JSON.stringify(rep));
  const all = await timedEval(page, (o) => window.TailorAutofill.applyOpsAsync(o), ops);
  assert.ok(all.r.timed_out === false && all.r.pending === 0, JSON.stringify(all.r));
  const again = await timedEval(page, (o) => window.TailorAutofill.applyOpsAsync(o), ops);
  assert.ok(again.ms < 500 && again.r.from_memory >= fields.length - 1 && again.r.opens === 0, "the same ops again come from memory " + JSON.stringify(again));
  await page.close();
});

test("fill cost: the form read keeps to its cap and still returns the fields", async ({ open }) => {
  const page = await open("verify_widgets.html");
  await page.evaluate(CORE);
  const cut = await timedEval(page, () => window.TailorAutofill.extractFormAsync({ onlyEmpty: true, budgetMs: 300 }));
  assert.ok(cut.ms < 1200, "a 300 ms cap took " + cut.ms + " ms");
  assert.ok(cut.r.length >= 1, "the fields are still returned");
  const st = await page.evaluate(() => window.TailorAutofill.lastExtract());
  assert.ok(st.opens <= 1, "no dropdown opened without time to read it " + JSON.stringify(st));
  const full = await timedEval(page, () => window.TailorAutofill.extractFormAsync({ onlyEmpty: true }));
  assert.ok(full.ms < 8500 && full.r.length >= cut.r.length, "the full read " + full.ms + " ms");
  await page.close();
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

test("a sign-in form's own button called Submit is clicked; next to application fields it stays guarded", async ({ open }) => {
  // (2026-11-18) A Workday tenant labels its sign-in button "Submit".
  const page = await open("react_select.html");
  await page.setContent(`<main><h2>Sign In</h2><div id="f">
    <label>Email Address <input type="text" id="e"></label>
    <label>Password <input type="password" id="p"></label>
    <button id="go" onclick="window.__signed = true">Submit</button></div>
    <button>Create Account</button><button>Forgot your password?</button></main>`);
  await page.evaluate(AGENT);
  let v = await page.evaluate("window.TailorAgent.view()");
  let b = indices(v.text).find((i) => /<button[^>]*>Submit<\/button>/.test(line(v.text, i)));
  assert.ok(b, v.text);
  let r = await page.evaluate(`window.TailorAgent.act({op: "click", index: ${b}})`);
  assert.strictEqual(r.ok, true, JSON.stringify(r));
  assert.strictEqual(await page.evaluate("window.__signed === true"), true, "the sign-in button was clicked");
  // the same button beside an application field (a text area) is the application's Submit
  await page.evaluate(`window.__signed = false; document.getElementById("f").insertAdjacentHTML("afterbegin", '<label>Why us? <textarea></textarea></label>')`);
  v = await page.evaluate("window.TailorAgent.view()");
  b = indices(v.text).find((i) => /<button[^>]*>Submit<\/button>/.test(line(v.text, i)));
  r = await page.evaluate(`window.TailorAgent.act({op: "click", index: ${b}})`);
  assert.deepStrictEqual([r.ok, r.note], [false, "submit-guard"]);
  assert.strictEqual(await page.evaluate("window.__signed === true"), false, "never clicked");
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

test("a covered element is not clicked: the step fails and names the cover; an uncovered one has no note", async ({ open }) => {
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
  // (agent-browser's interaction.rs does the same) a click an overlay would swallow fails and says
  // what covers the element, so the brain dismisses it instead of believing the click landed.
  assert.strictEqual(cov.ok, false, JSON.stringify(cov));
  assert.ok(/^covered by div#cookie-banner "We use cookies"/.test(cov.note), cov.note);
  assert.strictEqual(await page.evaluate("window.__applied"), 1);
  // The site's own search boxes are listed but flagged, so the brain can ignore them.
  assert.ok(/<input type=text label="Search for jobs by title or keyword" value="" search=true>/.test(v.text), v.text);
  assert.ok(/<combobox label="City, state, or country" value="" expanded=false search=true>/.test(v.text), v.text);
  await page.close();
});

// ------------------------------------------------------------------ verified actions (2026-11-16)
// From a study of vercel-labs/agent-browser (Apache-2.0): hidden-input cards are promoted into the
// view, required groups show in the page check, clicks on boxes are read back, and a dropdown pick
// reads only its own list, waits for a late one, and is read back (an async popup, a decoy listbox
// and a disabled option, as in its e2e widget tests).
test("card-style choices with hidden inputs are listed with their state, and a click lands through the card", async ({ open }) => {
  const page = await open("verify_widgets.html");
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const basic = indices(v.text).find((i) => /<radio [^>]*option="Basic"/.test(line(v.text, i)));
  const pro = indices(v.text).find((i) => /<radio [^>]*option="Pro"/.test(line(v.text, i)));
  assert.ok(basic && pro, v.text);
  assert.ok(/checked=false/.test(line(v.text, pro)), line(v.text, pro));
  const r = await page.evaluate(`window.TailorAgent.act({op: "click", index: ${pro}})`);
  assert.strictEqual(r.ok, true, JSON.stringify(r));
  assert.strictEqual(await page.evaluate("document.querySelector('input[value=pro]').checked"), true);
  const v2 = await page.evaluate("window.TailorAgent.view()");
  assert.ok(/checked=true/.test(line(v2.text, pro)), line(v2.text, pro));
  await page.close();
});

test("required radio and checkbox groups show in the page check until one is chosen", async ({ open }) => {
  const page = await open("verify_widgets.html");
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  let pc = await page.evaluate("window.TailorAgent.pageCheck()");
  assert.ok(pc.required_empty.includes("Which plan do you want?"), JSON.stringify(pc));
  assert.ok(pc.required_empty.includes("Which days can you work?"), JSON.stringify(pc));
  const mon = indices(v.text).find((i) => /<checkbox label="Monday"/.test(line(v.text, i)));
  const basic = indices(v.text).find((i) => /<radio [^>]*option="Basic"/.test(line(v.text, i)));
  assert.ok(/required/.test(line(v.text, mon)), line(v.text, mon));     // required from its fieldset
  await page.evaluate(`window.TailorAgent.act({op: "click", index: ${mon}})`);
  await page.evaluate(`window.TailorAgent.act({op: "click", index: ${basic}})`);
  pc = await page.evaluate("window.TailorAgent.pageCheck()");
  assert.deepStrictEqual(pc.required_empty, [], JSON.stringify(pc));
  await page.close();
});

test("a required file box that already lists an attached file is not empty in the page check", async ({ open }) => {
  const page = await open("verify_widgets.html");
  await page.evaluate(AGENT);
  await page.evaluate(`(() => {
    const f = document.querySelector("form") || document.body;
    const mk = (label, listed) => {
      const box = document.createElement("div");
      box.innerHTML = '<label>' + label + '<input type="file" required></label>'
        + (listed ? '<div><span>resume.pdf</span><button type="button" aria-label="Delete resume.pdf">x</button></div>' : '');
      f.appendChild(box);
    };
    mk("Upload your resume", true);
    mk("Upload a transcript", false);
  })()`);
  await page.evaluate("window.TailorAgent.view()");
  const pc = await page.evaluate("window.TailorAgent.pageCheck()");
  assert.ok(!pc.required_empty.some((x) => /resume/i.test(x)), JSON.stringify(pc));     // listed: answered
  assert.ok(pc.required_empty.some((x) => /transcript/i.test(x)), JSON.stringify(pc));  // nothing attached: still held
  await page.close();
});

test("a box whose click does not take is reported, never ok", async ({ open }) => {
  const page = await open("verify_widgets.html");
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  const stuck = indices(v.text).find((i) => /<checkbox label="A box that ignores clicks"/.test(line(v.text, i)));
  const r = await page.evaluate(`window.TailorAgent.act({op: "click", index: ${stuck}})`);
  assert.deepStrictEqual([r.ok, /did not change/.test(r.note)], [false, true], JSON.stringify(r));
  await page.close();
});

test("a dropdown pick waits for a late list, reads only its own, skips a disabled choice, and is read back", async ({ open }) => {
  const page = await open("verify_widgets.html");
  await page.evaluate(AGENT);                                   // the plain path (no engine)
  const v = await page.evaluate("window.TailorAgent.view()");
  const fruit = indices(v.text).find((i) => /<combobox label="Favourite fruit"/.test(line(v.text, i)));
  const tier = indices(v.text).find((i) => /<combobox label="Tier"/.test(line(v.text, i)));
  assert.ok(fruit && tier, v.text);
  // the sidebar's "Apple" (a decoy listbox) is never clicked; the popup's comes 600 ms late
  const r = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${fruit}, option: "Apple"})`);
  assert.strictEqual(r.ok, true, JSON.stringify(r));
  assert.deepStrictEqual(await page.evaluate("[window.fruit, window.sideClicks]"), ["Apple", 0]);
  // a disabled option is not offered: the step fails with the choices it has
  const t = await page.evaluate(`window.TailorAgent.act({op: "select", index: ${tier}, option: "Premium"})`);
  assert.strictEqual(t.ok, false, JSON.stringify(t));
  assert.ok(/options: Basic/.test(t.note) && !/Premium/.test(t.note.replace(/^.*options:/, "")), t.note);
  assert.strictEqual(await page.evaluate("window.tier"), "");
  await page.close();
});

test("a view cut by the cap still names the form's Submit further down (the ready gate needs it)", async ({ open }) => {
  const page = await open("verify_widgets.html");
  await page.evaluate(() => {
    const f = document.getElementById("f");
    const jd = document.createElement("div");
    for (let i = 0; i < 400; i++) { const p = document.createElement("p"); p.textContent = "About the role " + i + " with a long sentence about the team."; jd.appendChild(p); }
    for (let i = 0; i < 150; i++) { const b = document.createElement("button"); b.type = "button"; b.textContent = "Option " + i; jd.appendChild(b); }
    document.body.insertBefore(jd, f);
    const s = document.createElement("button"); s.textContent = "Submit application"; f.appendChild(s);
  });
  await page.evaluate(AGENT);
  const v = await page.evaluate("window.TailorAgent.view()");
  assert.ok(v.truncated, "the view should be cut");
  assert.ok(/The page's submit, further down:\n\[\d+\]<button>Submit application<\/button>/.test(v.text), v.text.slice(-600));
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
  const m = v.text.match(/\((\d+) more elements below; read_page offset (\d+) shows them\)\n\[End of view\]$/);
  assert.ok(m, v.text.slice(-200));
  assert.strictEqual(Number(m[1]), v.count - indices(v.text).length);
  assert.strictEqual(Number(m[2]), Math.max(...indices(v.text)) + 1, "the offset is the first element cut");
  assert.ok(!/pages below/.test(v.text), "never pages below next to the end marker");
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
  assert.ok(s.some((l) => /<input type=text label="How many years of experience do you have\?" value=""( required)?>/.test(l)), b.text);
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
  assert.ok(/\[End of page\]$/.test(v.text.trim()) && !/pages below/.test(v.text), v.text.slice(-60));   // the whole page is in the view
  const r = await page.evaluate("window.TailorAgent.act({op: 'scroll', direction: 'down'})");
  assert.strictEqual(r.ok, true);
  const y = await page.evaluate("window.scrollY");
  const vh = await page.evaluate("window.innerHeight");
  assert.ok(Math.abs(y - Math.round(vh * 0.8)) <= 2, `scrolled ${y} of ${vh}`);
  const w = await page.evaluate("window.TailorAgent.view()");
  assert.ok(!/pages (above|below)/.test(w.text) && /\[End of page\]$/.test(w.text.trim()), w.text.slice(-80));   // scrolled or not, the whole page is in the view
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
    // the person's decline, once chosen, is a value: the field no longer looks unanswered (since
    // 2026-11-26 a self-identification choice reads like any other choice)
    await page.click("[data-automation-id='gender']");
    await page.click("text=Choose not to disclose");
    v = await page.evaluate("window.TailorAgent.view()");
    assert.ok(/<combobox label="Gender" value="Choose not to disclose"/i.test(v.text), v.text);
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

// (Updated 2026-11-26, owner decision: the agent reads self-identification answers like any other
// profile fact, so the view shows the choice made, as for any other question.)
test("a self-identification choice reads like any other choice", async ({ open }) => {
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
  const sid = v.text.split("\n").filter((l) => /Gender|Ethnicity|Veteran|disability|Race/.test(l)).join("\n");
  assert.ok(/<select label="Gender" value="Male" options=3/.test(v.text), sid);
  assert.ok(/<select label="Ethnicity" value="Asian/.test(v.text), sid);
  assert.ok(/<radio label="Veteran Status" option="I identify as one or more of the classifications of protected veterans" checked=true/.test(v.text), sid);
  assert.ok(/<checkbox label="Yes, I have a disability, or have had one in the past" checked=true/.test(v.text), sid);
  assert.ok(/<checkbox label="No, I do not have a disability" checked=false/.test(v.text), sid);
  assert.ok(!/checked=hidden|answered=/.test(v.text), sid);
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

test("Phenom (Lam): the shell picks a saved choice with real clicks, checks it stuck, and the view shows it", async ({ open }) => {
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
  for (const label of Object.keys(want)) assert.ok(new RegExp(`<combobox label="${label}" value="[^"]+"`).test(v.text), label + "\n" + v.text.split("\n").filter((l) => /combobox/.test(l)).join("\n"));
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
