// The recorded interview stage: the rules behind the device menus, the question size and the
// microphone meter (ui/static/interview_stage.js), run in Node with a stubbed enumerateDevices.
"use strict";
const path = require("path");
const S = require(path.resolve(__dirname, "..", "..", "ui", "static", "interview_stage.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const eq = (a, b, m) => assert(JSON.stringify(a) === JSON.stringify(b), `${m}: ${JSON.stringify(a)} !== ${JSON.stringify(b)}`);
const dev = (kind, deviceId, label, groupId) => ({ kind, deviceId, label, groupId: groupId || "" });

(async () => {
  // A laptop with a USB webcam and a headset, as Chromium reports it: pseudo "default" and
  // "communications" entries, vendor ids in the labels, speakers mixed in.
  const many = { enumerateDevices: async () => [
    dev("audioinput", "default", "Default - Headset Microphone (0b0e:24c7)", "g2"),
    dev("audioinput", "communications", "Communications - Headset Microphone (0b0e:24c7)", "g2"),
    dev("audioinput", "mic-built", "Built-in Microphone (Built-in)", "g1"),
    dev("audioinput", "mic-head", "Headset Microphone (0b0e:24c7)", "g2"),
    dev("videoinput", "cam-built", "Built-in Camera (05ac:8514)", "g3"),
    dev("videoinput", "cam-usb", "USB Webcam (046d:085c)", "g4"),
    dev("audiooutput", "spk", "Speakers", "g1"),
  ] };
  const d = await S.listDevices(many);
  eq(d.mic.map((x) => x.id), ["mic-head", "mic-built"], "the system default microphone is listed first, pseudo entries folded away");
  eq(d.mic.map((x) => x.label), ["Headset Microphone", "Built-in Microphone"], "labels lose the prefixes and vendor ids");
  eq(d.cam.map((x) => x.label), ["Built-in Camera", "USB Webcam"], "cameras keep the browser's order");
  assert(S.menuMode(d.cam) === "menu" && S.menuMode(d.mic) === "menu", "two devices of a kind get a menu");

  // One of each: its name as plain text, no menu.
  const one = await S.listDevices({ enumerateDevices: async () => [
    dev("audioinput", "default", "Default - Built-in Microphone", "g1"), dev("audioinput", "m1", "Built-in Microphone", "g1"),
    dev("videoinput", "c1", "Built-in Camera", "g3")] });
  eq([one.cam.length, one.mic.length], [1, 1], "one device of each kind");
  assert(S.menuMode(one.cam) === "text" && S.menuMode(one.mic) === "text", "a single device is plain text");
  assert(S.menuMode([]) === "none", "no device, no menu");

  // Before permission the browser hides ids and labels: nothing to list, and nothing throws.
  eq(await S.listDevices({ enumerateDevices: async () => [dev("audioinput", "", ""), dev("videoinput", "", "")] }), { cam: [], mic: [] }, "unlabelled entries are left out");
  eq(await S.listDevices({ enumerateDevices: async () => { throw new Error("no"); } }), { cam: [], mic: [] }, "a failing enumerate is an empty list");
  eq(await S.listDevices(undefined), { cam: [], mic: [] }, "no mediaDevices is an empty list");
  // Only a pseudo entry (some drivers): it is still offered rather than nothing.
  eq((await S.listDevices({ enumerateDevices: async () => [dev("audioinput", "default", "Default - Line In")] })).mic, [{ id: "default", label: "Line In", groupId: "" }], "a lone default entry is kept");

  // The remembered choice: by id, then by name, else the default (first).
  eq(S.pickDevice(d.cam, { id: "cam-usb", label: "x" }).id, "cam-usb", "remembered by id");
  eq(S.pickDevice(d.cam, { id: "gone", label: "USB Webcam" }).id, "cam-usb", "remembered by name when the id changed");
  eq(S.pickDevice(d.cam, { id: "gone", label: "Unplugged Cam" }).id, "cam-built", "an unplugged device falls back to the default");
  eq(S.pickDevice(d.cam, null).id, "cam-built", "no memory means the default");
  assert(S.pickDevice([], { id: "x" }) === null, "nothing to pick from");

  // Asking for a device is exact, so a missing one fails instead of silently swapping.
  eq(S.streamConstraints("c", "m"), { video: { deviceId: { exact: "c" } }, audio: { deviceId: { exact: "m" } } }, "exact device ids");
  eq(S.streamConstraints(null, ""), { video: true, audio: true }, "no choice means the system default");

  // The question size rule: one calm size, and a smaller one for a very long question.
  const words = (n) => Array.from({ length: n }, (_, i) => ["tell", "me", "about", "something", "you", "learned", "recently"][i % 7]).join(" ") + "?";
  eq([S.questionTier("Why do you want this?"), S.questionTier("Tell me about a time you had to learn something quickly at work."), S.questionTier(words(35))],
     ["m", "m", "s"], "5, 12 and 35 words");
  eq(S.questionTier("x".repeat(150)), "m", "150 characters keeps the normal size");
  eq(S.questionTier("x".repeat(151)), "s", "151 steps down");
  eq(S.questionTier("  spaced \n  out  "), "m", "whitespace is not length");
  eq(S.questionTier(""), "m", "an empty question does not throw");
  const order = S.QUESTION_TIERS.map(([, max]) => max);
  assert(order.every((v, i) => i === 0 || v > order[i - 1]), "tiers only ever step down as the text grows");

  // The meter visibly moves for a normal voice, and stays inside the bar.
  const frame = (amp) => Uint8Array.from({ length: 256 }, (_, i) => 128 + Math.round(amp * 127 * Math.sin(i / 3)));
  assert(S.rms(frame(0)) === 0 && S.meterPercent(0) === 0, "silence is zero");
  const quiet = S.meterPercent(S.rms(frame(0.04))), loud = S.meterPercent(S.rms(frame(0.3)));
  assert(quiet >= 20 && quiet < loud && loud <= 100, `a quiet voice moves the bar a fifth or more (${quiet}), a loud one further (${loud})`);
  assert(S.meterPercent(5) === 100, "never past the end");

  // "No sound from this microphone" after five quiet seconds; any sound clears it.
  const w = S.silenceWatch(5000);
  assert(w.feed(0, 0) === false && w.feed(0.001, 4999) === false, "not before five seconds");
  assert(w.feed(0, 5000) === true, "silent at five seconds");
  assert(w.feed(0.2, 5100) === false, "a sound clears it");
  assert(w.feed(0, 9000) === false && w.feed(0, 10100) === true, "and the five seconds start again");

  console.log("ok");
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
