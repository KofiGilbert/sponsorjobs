// The recorded interview stage in a real Chromium, with a fake camera and microphone.
//
// Walks the real app.js through the session (welcome, notice, device check, practice, countdown,
// recording, playback, a real question, All done) with every /api answer stubbed here, so nothing
// depends on a model or a saved profile. It checks the rules the stage promises:
//   - focus mode: the app's chrome is hidden only while a session runs, and comes back on Exit;
//   - one composed card in the upper middle of the window, nothing clipped, no empty void;
//   - two columns that start on one line, each under a small label;
//   - the controls are attached to the video: the strip (clock, bar, action) on top of it, the
//     microphone level directly under it, all the same width; the action never moves;
//   - the recording status and the clock sit in one fixed place in both states;
//   - the question is medium, top aligned under its label, on balanced lines, never cut;
//   - narrow windows stack the question above the video;
//   - the camera and microphone menus list real devices, switch live, remember the choice,
//     survive an unplugged device, and are locked while recording;
//   - Escape never ends a recording.
//
// Usage: node interview_stage_harness.js <baseUrl> <playwrightDir> [--shots <dir>]
"use strict";
const fs = require("fs");
const path = require("path");

const [, , BASE, PW_DIR, ...REST] = process.argv;
if (!BASE || !PW_DIR) { console.error("need <baseUrl> <playwrightDir>"); process.exit(2); }
const SHOTS = REST[0] === "--shots" ? REST[1] : "";
const { chromium } = require(path.join(PW_DIR, "index.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const near = (a, b, tol) => Math.abs(a - b) <= (tol == null ? 1 : tol);

const Q = {
  short: "Why do you want this job?",
  typical: "Tell me about a time you had to learn something quickly at work.",
  long: "Describe a situation where you disagreed with a decision your team had already made, how you raised "
      + "your concern, what you did when the decision stayed the same, and what you would do differently if you faced it again today.",
};

// Two cameras and two microphones on top of Chromium's fake device, so the menus have a choice.
// getUserMedia still returns the real fake stream; the tracks report the device that was asked for.
const MULTI = () => {
  const md = navigator.mediaDevices, real = md.getUserMedia.bind(md);
  let list = [
    { kind: "audioinput", deviceId: "default", label: "Default - Built-in Microphone (Built-in)", groupId: "g1" },
    { kind: "audioinput", deviceId: "mic-built", label: "Built-in Microphone (Built-in)", groupId: "g1" },
    { kind: "audioinput", deviceId: "mic-head", label: "Headset Microphone (0b0e:24c7)", groupId: "g2" },
    { kind: "videoinput", deviceId: "cam-built", label: "Built-in Camera (05ac:8514)", groupId: "g3" },
    { kind: "videoinput", deviceId: "cam-usb", label: "USB Webcam (046d:085c)", groupId: "g4" },
  ];
  window.__gum = [];
  md.enumerateDevices = async () => list.map((d) => ({ ...d }));
  md.getUserMedia = async (c) => {
    window.__gum.push(JSON.parse(JSON.stringify(c)));
    const want = {}, plain = {};
    for (const [key, kind, def] of [["video", "videoinput", "cam-built"], ["audio", "audioinput", "mic-built"]]) {
      if (!c[key]) continue;
      const id = c[key] !== true && c[key].deviceId && c[key].deviceId.exact;
      if (id && !list.some((d) => d.kind === kind && d.deviceId === id)) {
        const e = new Error("gone"); e.name = "OverconstrainedError"; e.constraint = "deviceId"; throw e;
      }
      want[key] = id || (list.find((d) => d.kind === kind && d.deviceId !== "default") || {}).deviceId || def;
      plain[key] = true;
    }
    const s = await real(plain);
    for (const t of s.getTracks()) { const id = want[t.kind]; t.getSettings = () => ({ deviceId: id }); }
    return s;
  };
  window.__unplug = (id) => { list = list.filter((d) => d.deviceId !== id); md.dispatchEvent(new Event("devicechange")); };
};

async function stub(page, questions, opts) {
  const json = (route, body, status) => route.fulfill({ status: status || 200, contentType: "application/json", body: JSON.stringify(body) });
  const screen = { id: "s1", prep_id: "p1", i: null, config: { prep_s: 30, answer_s: 120, retakes: 1, extra_time: false },
                   questions: questions.map((text) => ({ text, q: text, kind: "behavioral" })) };
  await page.route("**/api/preps", (r) => r.request().method() === "POST"
    ? json(r, { prep: { id: "p1", role: "Data Analyst", company: "Acme Robotics", questions: [] } }) : r.continue());
  await page.route("**/api/screens", (r) => json(r, { ...screen, practice: { text: (opts && opts.practice) || Q.typical, kind: "practice" }, screen }));
  await page.route("**/api/screens/s1/practice*", (r) => json(r, { text: Q.short, kind: "practice" }));
  await page.route("**/api/screens/s1/answers/*", (r) => json(r, r.request().url().endsWith("/transcribe")
    ? { available: true, transcript: "I learned it by doing it." } : { ok: true, takes_left: 0 }));
  await page.route("**/api/screens/s1/score", () => { /* never answers: the All done screen stays up */ });
}

async function open(browser, { w, h, theme, questions, multi, practice }) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h } });
  const page = await ctx.newPage();
  // "tailor_onboarded_v1": a brand-new install jumps to Jobs once its profile has loaded, which
  // would pull the walk off the Interview view a few seconds in. This is not a first launch.
  await page.addInitScript((t) => { try { localStorage.setItem("tailor-theme", t); localStorage.setItem("tailor_onboarded_v1", "1"); } catch (e) {} }, theme || "light");
  if (multi) await page.addInitScript(MULTI);
  await stub(page, questions || [Q.typical, Q.short], { practice });
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await settle(page);
  await page.click('[data-nav="prep"]');
  await page.click("#tileRound1");
  await page.fill("#prepOpenRole", "Data Analyst");
  await page.fill("#prepOpenJd", "Analyse data and present findings to the team.");
  await page.click("#prepStart");
  await page.waitForSelector("#scrBegin");
  return { ctx, page };
}
const settle = async (page) => { await page.waitForLoadState("load"); await page.waitForTimeout(300); };
const toCheck = async (page) => { await page.click("#scrBegin"); await page.click("#scrNoticeGo", { timeout: 5000 }).catch(async (e) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "fail.png") }); throw e; }); await page.waitForSelector('#r1Stage[data-step="check"]'); await page.waitForTimeout(700); };
const record = async (page) => { await page.click("#r1Start"); await page.waitForSelector("#r1Stop", { timeout: 8000 }); };
const rect = (page, sel) => page.evaluate((s) => { const e = document.querySelector(s); if (!e) return null; const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom }; }, sel);
const shown = (page, sel) => page.evaluate((s) => { const e = document.querySelector(s); if (!e) return false; const r = e.getBoundingClientRect(); return getComputedStyle(e).display !== "none" && r.width > 0 && r.height > 0; }, sel);
let SHOT_TAG = "";
async function shot(page, name) {
  if (!SHOTS) return;
  await page.waitForTimeout(250);
  await page.screenshot({ path: path.join(SHOTS, `${SHOT_TAG}-${name}.png`) });
}
// How the question is set: its lines (words per line), the font size, and whether anything is cut.
const question = (page) => page.evaluate(() => {
  const q = document.querySelector("#r1Q"), node = q.firstChild, text = node.textContent, lines = [];
  const re = /\S+/g; let m;
  while ((m = re.exec(text))) {
    const r = document.createRange(); r.setStart(node, m.index); r.setEnd(node, m.index + m[0].length);
    const top = Math.round(r.getBoundingClientRect().top);
    const line = lines.find((l) => Math.abs(l.top - top) < 4);
    if (line) { line.words++; line.chars += m[0].length + 1; } else lines.push({ top, words: 1, chars: m[0].length });
  }
  const pane = q.closest(".r1-col-q"), cs = getComputedStyle(q);
  return { lines, size: parseFloat(cs.fontSize), weight: cs.fontWeight, align: cs.textAlign, tier: q.dataset.size,
           cut: q.scrollWidth > q.clientWidth + 1 || pane.scrollHeight > pane.clientHeight + 1 || pane.scrollWidth > pane.clientWidth + 1,
           shownText: q.textContent };
});
// Text elements a person reads on the stage (visible, own text), for the before/after count.
const strings = (page) => page.evaluate(() => {
  const out = [];
  const walk = document.createTreeWalker(document.querySelector("#r1Stage"), NodeFilter.SHOW_TEXT);
  while (walk.nextNode()) {
    const n = walk.currentNode, t = n.textContent.trim(), el = n.parentElement;
    if (!t || !el) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || getComputedStyle(el).visibility === "hidden" || el.closest("[hidden]")) continue;
    out.push(t);
  }
  return out;
});

// The composition the card promises, measured on whatever step is on screen.
async function composed(page, w, h, step) {
  const card = await rect(page, ".r1-card"), v = await rect(page, ".scr-video-wrap"), strip = await rect(page, ".r1-strip"), under = await rect(page, "#r1Under .rec-meter-track");
  const labels = await page.evaluate(() => [...document.querySelectorAll(".r1-card > .r1-col > .r1-label")].map((e) => { const r = e.getBoundingClientRect(); return { y: r.y, b: r.bottom }; }));
  assert(card.y >= 52 && card.b <= h && card.x >= 0 && card.r <= w, `${step}: the card is whole on screen (${JSON.stringify(card)})`);
  assert(near(card.x + card.w / 2, w / 2, 2), `${step}: the card is centred`);
  assert(card.w >= w * 0.85 && card.h >= h * 0.55, `${step}: the card uses the window, no empty void (${Math.round(card.w)}x${Math.round(card.h)} in ${w}x${h})`);
  const above = card.y - 52, below = h - card.b;
  assert(above <= below + 2, `${step}: the card sits in the upper middle (${Math.round(above)} above, ${Math.round(below)} below)`);
  assert(labels.length === 2 && near(labels[0].y, labels[1].y) && near(labels[0].b, labels[1].b), `${step}: both columns start on one line under their labels`);
  assert(near(v.w / v.h, 16 / 9, 0.01), `${step}: the video is 16:9`);
  assert(near(strip.b, v.y, 1.5) && near(strip.x, v.x, 1.5) && near(strip.w, v.w, 2), `${step}: the control strip is attached to the top of the video, the same width`);
  assert(under.y - v.b <= 30 && under.r <= v.r + 2 && under.r >= v.r - 3, `${step}: the microphone level sits directly under the video`);
  assert(strip.y - labels[1].b <= 40, `${step}: the strip sits right under the label`);
  return { card, v, strip };
}

async function layoutRun(browser, w, h, theme) {
  SHOT_TAG = `${w}x${h}-${theme}`;
  const counts = {};
  const { ctx, page } = await open(browser, { w, h, theme, multi: true });
  // The welcome and the notice are the same stage: only the card, the app's chrome hidden.
  const chrome = [".sidebar", "#view-prep > .page-head", "#prepScreen > .mock-top", ".privacy-line", ".panel-rail", "#prepProgress"];
  for (const sel of chrome) assert(!(await shown(page, sel)), `${sel} is hidden on the welcome`);
  const solo = await rect(page, ".r1-card");
  assert(near(solo.x + solo.w / 2, w / 2, 2) && solo.y >= 52 && solo.b <= h, "the welcome card is centred and whole");
  counts.welcome = await strings(page);
  await shot(page, "0a-welcome");
  await page.click("#scrBegin");
  counts.notice = await strings(page);
  await shot(page, "0b-notice");
  await page.click("#scrNoticeGo"); await page.waitForSelector('#r1Stage[data-step="check"]'); await page.waitForTimeout(700);
  for (const sel of chrome) assert(!(await shown(page, sel)), `${sel} is hidden while a session runs`);
  const stage = await rect(page, "#r1Stage");
  assert(near(stage.x, 0) && near(stage.y, 0) && near(stage.w, w) && near(stage.h, h), `the stage fills the window (${JSON.stringify(stage)})`);
  const c0 = await composed(page, w, h, "device check");
  const vCheck = c0.v, bCheck = await rect(page, "#r1Acts .r1-btn-primary");
  assert((await rect(page, ".r1-col-q")).r <= vCheck.x, "the words sit to the left of the video");
  // The meter visibly moves (the fake microphone beeps).
  let peak = 0;
  for (let i = 0; i < 30 && peak < 8; i++) { peak = Math.max(peak, await page.evaluate(() => parseFloat(document.querySelector("#r1Stage .rec-meter-fill").style.width) || 0)); await page.waitForTimeout(100); }
  assert(peak >= 8, `the microphone meter moves (${peak}%)`);
  counts.check = await strings(page);
  await shot(page, "1-device-check");
  await page.click('.r1-menu[data-kind="mic"] [data-r1-menu]');
  // An open menu is whole: its last option is the topmost thing where it is drawn.
  assert(await page.evaluate(() => { const o = document.querySelector('.r1-menu[data-kind="mic"] .r1-menu-opt:last-child'), r = o.getBoundingClientRect();
    return document.elementFromPoint(r.x + r.width / 2, r.bottom - 3) === o; }), "the open menu is not clipped");
  await shot(page, "2-device-check-microphone-menu");
  await page.keyboard.press("Escape");
  assert(await page.evaluate(() => document.querySelector('.r1-menu[data-kind="mic"] .r1-menu-list').hidden), "Escape closes the menu");
  assert(await shown(page, "#r1Stage"), "and does not leave the session");

  // Practice, prep.
  await page.click("#scrToPractice");
  await page.waitForSelector("#r1Start");
  const c1 = await composed(page, w, h, "prep");
  const stPrep = await rect(page, "#r1Status"), ckPrep = await rect(page, "#r1ClockT"), pgPrep = await rect(page, ".r1-prog"), vPrep = c1.v, bPrep = await rect(page, "#r1Start");
  assert(near(vPrep.x, vCheck.x) && near(vPrep.y, vCheck.y) && near(vPrep.w, vCheck.w), "the video does not move between steps");
  assert(near(c1.card.y, c0.card.y) && near(c1.card.h, c0.card.h, 2), "the card does not move or resize between steps");
  assert(near(bPrep.y, bCheck.y) && near(bPrep.r, bCheck.r), "the action button is in the same spot as on the device check");
  assert((await page.textContent("#r1StatusT")) === "Not recording", "status is sentence case");
  // The question is top aligned, right under its label.
  const lab = await rect(page, ".r1-col-q .r1-label"), qr = await rect(page, "#r1Q");
  assert(qr.y - lab.b <= 40 && near(qr.x, lab.x), `the question starts right under its label (${Math.round(qr.y - lab.b)}px)`);
  const gearR = await rect(page, "#r1Gear");
  assert(near(gearR.x, lab.x, 8) && gearR.y > qr.b && gearR.b <= c1.card.b, "Settings sits at the bottom left of the words column");
  counts.prep = await strings(page);
  await page.waitForTimeout(2200);
  await shot(page, "3-practice-prep");
  await page.click("#r1Gear");
  assert(await shown(page, "#r1Pop .r1-menu"), "Settings opens the same two menus");
  await shot(page, "4-practice-settings-open");
  await page.keyboard.press("Escape");
  assert(!(await shown(page, "#r1Pop")), "Escape closes the menus");
  // Countdown, then recording.
  await page.click("#r1Start");
  await page.waitForSelector("#r1Count:not([hidden])");
  await shot(page, "5-countdown");
  await page.waitForSelector("#r1Stop", { timeout: 8000 });
  await composed(page, w, h, "recording");
  const stRec = await rect(page, "#r1Status"), ckRec = await rect(page, "#r1ClockT"), pgRec = await rect(page, ".r1-prog"), bRec = await rect(page, "#r1Stop");
  assert(near(stRec.x, stPrep.x) && near(stRec.y, stPrep.y) && near(stRec.w, stPrep.w), `the status pill does not move when recording starts (${JSON.stringify([stPrep, stRec])})`);
  assert(near(ckRec.x, ckPrep.x) && near(ckRec.y, ckPrep.y) && near(ckRec.h, ckPrep.h), `the response clock sits exactly where the prep clock sat (${ckPrep.x} then ${ckRec.x})`);
  assert(near(pgRec.x, pgPrep.x) && near(pgRec.y, pgPrep.y), "and so does its progress bar");
  assert(near(bRec.y, bPrep.y) && near(bRec.r, bPrep.r), "Done answering is where Start recording was");
  assert((await page.textContent("#r1Stop")) === "Done answering", "the stop button reads Done answering");
  assert((await page.evaluate(() => getComputedStyle(document.querySelector("#r1Clock")).fontVariantNumeric)).includes("tabular-nums"), "the clock uses tabular numbers");
  assert(await page.evaluate(() => document.querySelector("#r1Gear").disabled && !!document.querySelector("#r1Gear").title), "Settings is locked, with a reason, while recording");
  counts.recording = await strings(page);
  await page.waitForTimeout(1600);
  const drained = await page.evaluate(() => parseFloat(document.querySelector("#r1Prog").style.width));
  assert(drained < 100 && drained > 90, `the progress bar drains with the clock (${drained}%)`);
  await shot(page, "6-recording");
  // Escape asks; it never ends the recording.
  await page.keyboard.press("Escape");
  assert(await shown(page, "#r1Leave"), "Escape while recording asks before leaving");
  assert(await shown(page, "#r1Stop") && (await page.textContent("#r1StatusT")) === "Recording", "and the recording carries on");
  await shot(page, "7-recording-exit-asks");
  await page.click("#r1LeaveStay");
  await page.waitForTimeout(1200);
  await page.click("#r1Stop");
  await page.waitForSelector("#r1StartReal");
  const stRev = await rect(page, "#r1Status"), bRev = await rect(page, "#r1StartReal");
  assert(near(stRev.x, stPrep.x) && near(stRev.w, stPrep.w), "the status pill is back where it was");
  assert(near(bRev.y, bPrep.y) && near(bRev.r, bPrep.r), "the review actions end at the same spot");
  assert(await page.evaluate(() => { const v = document.querySelector("#scrVideo"); return v.controls && !v.muted && v.src.startsWith("blob:"); }), "a practice answer can be played back with sound");
  counts.review = await strings(page);
  await shot(page, "8-practice-playback");
  // The real questions.
  await page.click("#r1StartReal");
  await page.waitForSelector("#r1Start");
  assert((await page.textContent("#r1Step")) === "Question 1 of 2", "progress is the words column's label");
  counts.question = await strings(page);
  await shot(page, "9-question-prep");
  await record(page);
  await page.waitForTimeout(2600);
  await shot(page, "9b-question-recording");
  await page.click("#r1Stop");
  await page.waitForSelector("#r1Next");
  const bNext = await rect(page, "#r1Next");
  assert(near(bNext.y, bPrep.y) && near(bNext.r, bPrep.r), "Next question ends at the same spot");
  counts.saved = await strings(page);
  await shot(page, "10-question-saved");
  // Exit with answers recorded asks first; Leave brings the app's chrome back.
  await page.click("#r1Next");
  await page.waitForSelector("#r1Start");
  await record(page); await page.waitForTimeout(600); await page.click("#r1Stop");
  await page.waitForSelector("#r1Next");
  await page.click("#r1Next");
  await page.waitForSelector('#r1Stage[data-step="done"]');
  const done = await rect(page, ".r1-card");
  assert(near(done.x + done.w / 2, w / 2, 2), "All done is centred");
  counts.done = await strings(page);
  await shot(page, "11-all-done");
  await page.click("#r1Exit");
  assert(await shown(page, "#r1Leave"), "Exit asks once answers are recorded");
  await page.click("#r1LeaveGo");
  await page.waitForSelector("#prepChoices");
  assert(await shown(page, ".sidebar") && await shown(page, "#view-prep > .page-head"), "leaving brings the side menu and the page title back");
  assert(!(await page.$("#r1Stage")), "the stage is gone");
  await ctx.close();
  return counts;
}

async function questionRun(browser, w, h) {
  SHOT_TAG = `${w}x${h}-light`;
  const seen = {};
  for (const [name, text] of Object.entries(Q)) {
    const { ctx, page } = await open(browser, { w, h, practice: text });
    await toCheck(page); await page.click("#scrToPractice"); await page.waitForSelector("#r1Q");
    await page.waitForTimeout(200);
    const q = seen[name] = await question(page);
    assert(q.shownText === text && !q.cut, `${name} question at ${w}: shown whole, never cut (${JSON.stringify(q)})`);
    assert(q.weight === "500" && (q.align === "left" || q.align === "start"), `${name}: medium weight, left aligned`);
    // About 38 characters a line at the normal size; the small size may run to a normal reading line.
    const cap = { m: 44, s: 58 }[q.tier];
    assert(q.lines.every((l) => l.chars <= cap), `${name} at ${w}: no very long line for tier ${q.tier} (${JSON.stringify(q.lines)})`);
    if (q.lines.length > 1) assert(q.lines[q.lines.length - 1].words >= 2, `${name} at ${w}: no orphan word (${JSON.stringify(q.lines)})`);
    const card = await rect(page, ".r1-card");
    assert(card.b <= h, `${name} at ${w}: the card still fits`);
    await shot(page, `question-${name}`);
    await ctx.close();
  }
  assert(seen.short.lines.length === 1, `a short question is one line (${seen.short.lines.length})`);
  assert(seen.typical.lines.length >= 2 && seen.typical.lines.length <= 3, `a typical question sits on two or three lines (${seen.typical.lines.length})`);
  assert(seen.short.size === seen.typical.size && seen.typical.size > seen.long.size, `only a very long question steps the size down (${seen.short.size}, ${seen.typical.size}, ${seen.long.size})`);
  const zoomed = w > 2000;
  assert(seen.typical.size >= (zoomed ? 28 : 20) && seen.typical.size <= (zoomed ? 34 : 26), `the question is medium, not a headline (${seen.typical.size}px at ${w})`);
  return seen;
}

async function narrowRun(browser) {
  // 900 wide still holds two columns; below 820 the columns stack.
  SHOT_TAG = "900x806-light";
  let { ctx, page } = await open(browser, { w: 900, h: 806, multi: true });
  await toCheck(page);
  await shot(page, "two-column-device-check");
  await page.click("#scrToPractice"); await page.waitForSelector("#r1Q");
  await composed(page, 900, 806, "900 wide").catch((e) => { if (!/no empty void/.test(e.message)) throw e; });
  assert(!(await question(page)).cut, "900 wide: the question is not cut");
  await page.waitForTimeout(1200);
  await shot(page, "two-column-practice-prep");
  await ctx.close();

  SHOT_TAG = "700x806-light";
  // Opened wider, then narrowed: this is the Browser pane opening beside a running session (the
  // app's own navigation is not laid out for a 700px window).
  ({ ctx, page } = await open(browser, { w: 1000, h: 806, multi: true }));
  await page.setViewportSize({ width: 700, height: 806 });
  await toCheck(page);
  await shot(page, "stacked-device-check");
  await page.click("#scrToPractice"); await page.waitForSelector("#r1Q");
  const q = await rect(page, "#r1Q"), v = await rect(page, ".scr-video-wrap"), b = await rect(page, "#r1Start"), card = await rect(page, ".r1-card");
  assert(q.b <= v.y, "narrow: the question is above the video");
  assert(near(v.w / v.h, 16 / 9, 0.01) && b.b <= 806 && card.b <= 806, `narrow: the video stays 16:9 and the whole card stays on screen (${JSON.stringify(card)})`);
  assert(!(await question(page)).cut, "narrow: the question is not cut");
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "narrow: no sideways scroll");
  await page.waitForTimeout(1200);
  await shot(page, "stacked-practice-prep");
  await ctx.close();
}

async function deviceRun(browser) {
  SHOT_TAG = "1440x806-light";
  // Several devices: menus, live switching, memory, an unplugged headset.
  let { ctx, page } = await open(browser, { w: 1440, h: 806, multi: true });
  await toCheck(page);
  const names = (kind) => page.evaluate((k) => [...document.querySelectorAll(`.r1-col-q .r1-menu[data-kind="${k}"] [data-r1-opt]`)].map((o) => o.textContent), kind);
  assert(JSON.stringify(await names("cam")) === JSON.stringify(["Built-in Camera", "USB Webcam"]), "the camera menu lists the real cameras");
  assert(JSON.stringify(await names("mic")) === JSON.stringify(["Built-in Microphone", "Headset Microphone"]), "the microphone menu lists the real microphones, default first, nothing twice");
  await page.click('.r1-menu[data-kind="cam"] [data-r1-menu]');
  await shot(page, "device-check-camera-menu");
  await page.click('[data-r1-opt="cam-usb"]');
  await page.waitForFunction(() => document.querySelector('.r1-menu[data-kind="cam"] .r1-menu-v')?.textContent === "USB Webcam");
  let gum = await page.evaluate(() => window.__gum[window.__gum.length - 1]);
  assert(gum.video && gum.video.deviceId.exact === "cam-usb" && !("audio" in gum), "switching the camera asks for exactly that camera and leaves the microphone alone");
  assert(await page.evaluate(() => { const v = document.querySelector("#scrVideo"); return v.srcObject.getVideoTracks()[0].readyState === "live" && v.srcObject.getAudioTracks()[0].readyState === "live"; }), "the preview is live on the new camera");
  await page.click('.r1-menu[data-kind="mic"] [data-r1-menu]');
  await page.keyboard.press("ArrowDown"); await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector('.r1-menu[data-kind="mic"] .r1-menu-v')?.textContent === "Headset Microphone");
  assert(JSON.parse(await page.evaluate(() => localStorage.getItem("sj.r1.mic"))).id === "mic-head", "the choice is remembered");
  // The headset is unplugged mid-check: back to the default, one quiet line.
  await page.evaluate(() => window.__unplug("mic-head"));
  await page.waitForFunction(() => /disconnected/i.test(document.querySelector(".r1-col-q .r1-dev-note")?.textContent || ""));
  assert(await page.evaluate(() => document.querySelector(".r1-col-q .r1-devs").querySelectorAll(".r1-dev-name").length === 1), "with one microphone left its name is plain text, no menu");
  assert(await page.evaluate(() => document.querySelector("#scrVideo").srcObject.getAudioTracks()[0].readyState === "live"), "and the default microphone is live");
  await shot(page, "device-check-headset-unplugged");
  // The session keeps the choice: the question records from the chosen camera.
  await page.click("#scrToPractice"); await page.waitForSelector("#r1Start");
  assert(await page.evaluate(() => document.querySelector("#scrVideo").srcObject.getVideoTracks()[0].getSettings().deviceId === "cam-usb"), "the chosen camera is used for the questions");
  await ctx.close();

  // Next time: the remembered camera is asked for first.
  ({ ctx, page } = await open(browser, { w: 1440, h: 806, multi: true }));
  await page.addInitScript(() => { try { localStorage.setItem("sj.r1.cam", JSON.stringify({ id: "cam-usb", label: "USB Webcam" })); } catch (e) {} });
  await page.reload({ waitUntil: "domcontentloaded" });
  await settle(page);
  await page.click('[data-nav="prep"]'); await page.click("#tileRound1");
  await page.fill("#prepOpenRole", "Data Analyst"); await page.fill("#prepOpenJd", "Analyse data."); await page.click("#prepStart");
  await page.waitForSelector("#scrBegin"); await toCheck(page);
  gum = await page.evaluate(() => window.__gum[0]);
  assert(gum.video.deviceId && gum.video.deviceId.exact === "cam-usb", "the remembered camera is used next time");
  assert((await page.textContent('.r1-menu[data-kind="cam"] .r1-menu-v')) === "USB Webcam", "and shown as selected");
  await ctx.close();

  // Chromium's own fake devices, nothing stubbed: the lists come from the real enumerateDevices.
  ({ ctx, page } = await open(browser, { w: 1440, h: 806 }));
  await toCheck(page);
  const real = await page.evaluate(() => ({ rows: document.querySelectorAll(".r1-col-q .r1-dev").length,
    named: [...document.querySelectorAll(".r1-col-q .r1-menu-v, .r1-col-q .r1-dev-name")].map((e) => e.textContent) }));
  assert(real.rows === 2 && real.named.length === 2 && real.named.every((n) => n && !/^No /.test(n)), `real devices are listed with their names (${JSON.stringify(real)})`);
  await ctx.close();
  return real;
}

(async () => {
  if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch({ args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"] });
  const report = {};
  try {
    report.counts = await layoutRun(browser, 1440, 806, "light");
    await layoutRun(browser, 2491, 1557, "light");
    if (SHOTS) { await layoutRun(browser, 1440, 806, "dark"); await layoutRun(browser, 2491, 1557, "dark"); }
    report.q1440 = await questionRun(browser, 1440, 806);
    report.q2491 = await questionRun(browser, 2491, 1557);
    await narrowRun(browser);
    report.realDevices = await deviceRun(browser);
  } finally { await browser.close(); }
  if (SHOTS) fs.writeFileSync(path.join(SHOTS, "report.json"), JSON.stringify(report, null, 2));
  console.log("ok");
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
