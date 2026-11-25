// The recorded interview stage: the rules that do not need the page.
//
// Kept apart from app.js so they are plain functions: which cameras and microphones to offer and
// in what order, which one to use (the remembered one, else the system default), how to ask the
// browser for exactly that device, how large a question may be set, and when a microphone counts
// as silent. app.js wires them to the screen; tests/js/test_interview_stage.js runs them in Node.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.SJStage = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // "FaceTime HD Camera (05ac:8514)" and "Default - MacBook Pro Microphone (Built-in)" are how
  // Chromium names devices; people know them without the vendor ids and prefixes.
  function cleanLabel(label) {
    return String(label || "")
      .replace(/^(Default|Communications)\s*-\s*/i, "")
      .replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)\s*$/i, "")
      .replace(/\s*\((Built-in|Virtual)\)\s*$/i, "")
      .trim();
  }

  // One list per kind, the system default first. Chromium adds pseudo entries ("default",
  // "communications") that repeat a real device; they are folded into the device they point at so
  // nothing is listed twice. Entries without a label (permission not granted yet) are left out.
  function groupDevices(infos) {
    const out = { cam: [], mic: [] };
    const kinds = { videoinput: "cam", audioinput: "mic" };
    for (const kind of Object.keys(kinds)) {
      const all = (infos || []).filter((d) => d && d.kind === kind && d.deviceId && d.label);
      const pseudo = (d) => d.deviceId === "default" || d.deviceId === "communications";
      const real = all.filter((d) => !pseudo(d));
      const def = all.find((d) => d.deviceId === "default");
      let list = (real.length ? real : all).map((d) => ({ id: d.deviceId, label: cleanLabel(d.label) || d.label, groupId: d.groupId || "" }));
      if (def && real.length) {
        const want = cleanLabel(def.label);
        const k = list.findIndex((d) => d.label === want || (def.groupId && d.groupId === def.groupId));
        if (k > 0) list.unshift(list.splice(k, 1)[0]);
      }
      const seen = new Set();
      out[kinds[kind]] = list.filter((d) => (seen.has(d.id) ? false : (seen.add(d.id), true)));
    }
    return out;
  }
  async function listDevices(mediaDevices) {
    if (!mediaDevices || typeof mediaDevices.enumerateDevices !== "function") return { cam: [], mic: [] };
    try { return groupDevices(await mediaDevices.enumerateDevices()); } catch (e) { return { cam: [], mic: [] }; }
  }

  // The remembered device when it is still plugged in (by id, then by name: ids can change when a
  // device is re-paired), otherwise the first one, which is the system default.
  function pickDevice(list, saved) {
    if (!list || !list.length) return null;
    if (saved) {
      const byId = saved.id && list.find((d) => d.id === saved.id);
      if (byId) return byId;
      const byLabel = saved.label && list.find((d) => d.label === saved.label);
      if (byLabel) return byLabel;
    }
    return list[0];
  }

  // `exact` so a missing device fails loudly (the caller then falls back to the default and says
  // so) instead of the browser quietly recording from a different one.
  function streamConstraints(camId, micId) {
    return {
      video: camId ? { deviceId: { exact: camId } } : true,
      audio: micId ? { deviceId: { exact: micId } } : true,
    };
  }

  // A menu is a choice; with one device there is nothing to choose, so it is shown as plain text.
  function menuMode(list) { return !list || !list.length ? "none" : list.length === 1 ? "text" : "menu"; }

  // The question is the most important text on the screen. It is set large and broken into two or
  // three balanced lines; a longer question steps the size down instead of running long or being
  // cut. Tiers by length (characters, spaces included); the sizes and line lengths live in CSS
  // (.r1-q[data-size]).
  const QUESTION_TIERS = [["l", 48], ["m", 110], ["s", 190], ["xs", Infinity]];
  function questionTier(text) {
    const n = String(text || "").trim().replace(/\s+/g, " ").length;
    return QUESTION_TIERS.find(([, max]) => n <= max)[0];
  }

  // Loudness of one analyser frame (0..1 RMS), and how far the meter moves for it. Speech sits
  // around 0.02 to 0.15 RMS, so a straight scale barely moves; the square root makes a normal
  // voice fill a third to two thirds of the bar.
  function rms(bytes) {
    let sum = 0;
    for (let i = 0; i < bytes.length; i++) { const v = (bytes[i] - 128) / 128; sum += v * v; }
    return bytes.length ? Math.sqrt(sum / bytes.length) : 0;
  }
  function meterPercent(level) { return Math.max(0, Math.min(100, Math.round(Math.sqrt(Math.max(0, level)) * 220))); }

  // "No sound from this microphone": true once nothing above the floor has been heard for `ms`.
  // Any sound clears it at once.
  function silenceWatch(ms, floor) {
    const limit = ms == null ? 5000 : ms, min = floor == null ? 0.012 : floor;
    let since = null;
    return {
      feed(level, now) {
        if (level > min) { since = now; return false; }
        if (since == null) since = now;
        return now - since >= limit;
      },
      reset() { since = null; },
    };
  }

  return { cleanLabel, groupDevices, listDevices, pickDevice, streamConstraints, menuMode,
           questionTier, QUESTION_TIERS, rms, meterPercent, silenceWatch };
});
