// What a frame says after the person pressed Submit inside it (2026-11-26).
//
// A company site that embeds its application form in an iframe (instacart.careers with Greenhouse)
// gives the top page nothing to see when the person sends it: the frame may show its own
// confirmation, draw the empty form again, or load its page again. The walked frames are held while
// the person works (main.js holdGuideFrames); each poll's answer from a frame
// (TailorAutofill.relayTake) goes through this reader and comes out as SIGNALS for the app's one
// sent detector (shell-electron/sent_detect.js). Nothing here decides that the application was sent.
//   {kind: "submit-click", frameUrl, filled}              the person pressed the frame's Submit (a
//                                                          real click, or the form's own submit)
//   {kind: "page", how: "marker", pageUrl, frame: true}    after that, the frame shows the site's
//                                                          confirmation words or markers
//   {kind: "form-reset", frameUrl, filledBefore, filledAfter}  after that, the frame's form is back
//                                                          to empty (in place, or its page loaded again)
// Each is said once per Submit. Pure (no Electron): tests/js/test_frame_walk.js.
"use strict";

const WINDOW_MS = 5 * 60 * 1000;        // the frame is watched this long after a Submit

// o: { signal(sig) (default: sent_detect.signal), now? }
function createFrameSignalReader(o) {
  const signal = (o && o.signal) || require("./sent_detect").signal;
  const now = (o && o.now) || Date.now;
  const st = new Map();                  // frame prefix -> { at, filled, frameAt, said: Set }
  const say = (s, kind, sig) => {
    if (s.said.has(kind)) return;
    s.said.add(kind);
    try { signal(sig); } catch (e) { /* the detector's */ }
  };
  const recent = (s) => !!(s && s.at && now() - s.at <= WINDOW_MS);
  // a Submit stamp the frame reports (it survives a reload: the frame's session storage)
  const stamp = (c, got) => {
    let s = st.get(c.prefix);
    if (got.submitAt && (!s || s.frameAt !== got.submitAt)) {
      s = { at: now(), filled: Number(got.filledAtSubmit) || 0, frameAt: got.submitAt, said: new Set() };
      st.set(c.prefix, s);
      say(s, "submit-click", { kind: "submit-click", frameUrl: c.url || "", filled: s.filled });
    }
    return s;
  };
  const after = (c, s, got) => {
    if (got.success) say(s, "page", { kind: "page", how: "marker", pageUrl: c.url || "", frame: true });
    const f = got.form;
    if (f && s.filled > 0 && (f.total === 0 || f.filled === 0)) {
      say(s, "form-reset", { kind: "form-reset", frameUrl: c.url || "", filledBefore: s.filled, filledAfter: f.filled || 0 });
    }
  };
  return {
    // One poll's answer from a frame c = {prefix, url}: relayTake on its page as it stands, or on
    // its NEW page (a fresh engine after the page changed).
    seen(c, got) {
      if (!c || !got) return;
      const s = stamp(c, got);
      if (recent(s)) after(c, s, got);
    },
  };
}

module.exports = { createFrameSignalReader, WINDOW_MS };
