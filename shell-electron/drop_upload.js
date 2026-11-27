// A resume dropped on an upload box that takes only a dropped file (2026-11-26, audit case 9).
//
// Some upload boxes have no file input at all: they read the file from a drop. A script cannot
// make a real drop (a page's own DataTransfer can't carry a file from disk), so the shell drags
// the file in through the Chrome DevTools Protocol, the way a person drags it from Finder:
// Input.dispatchDragEvent dragEnter, dragOver, drop at the box's centre, with the file's path.
// The page sees trusted drag events carrying the real file. In sight: the page scrolls the box
// into view and the pointer says "Uploading resume" first (main.js dropResume).
//
// Pure: ``send(method, params)`` is the CDP call (Electron's wc.debugger.sendCommand, or a
// Playwright CDP session in the replay suite).
"use strict";

const STEPS = ["dragEnter", "dragOver", "drop"];

// {ok, steps} after the three drag events at (x, y) in the page's viewport (CSS pixels).
async function dropFile(send, point, filePath) {
  if (!point || !(point.x >= 0) || !(point.y >= 0) || !filePath) return { ok: false, steps: 0 };
  const data = { items: [], files: [String(filePath)], dragOperationsMask: 1 };
  let steps = 0;
  try {
    for (const type of STEPS) {
      await send("Input.dispatchDragEvent", { type, x: Math.round(point.x), y: Math.round(point.y), data, modifiers: 0 });
      steps++;
    }
  } catch (e) {
    // a drag half done is ended, so the page is never left mid-drag
    if (steps && steps < STEPS.length) { try { await send("Input.dispatchDragEvent", { type: "dragCancel", x: Math.round(point.x), y: Math.round(point.y), data, modifiers: 0 }); } catch (e2) { /* gone */ } }
    return { ok: false, steps };
  }
  return { ok: true, steps };
}

// The page side: scroll the box into view, point at it, and say where its centre is now.
const POINT_JS = (ref) => `(async () => { const T = window.TailorAutofill; if (!T) return null;
  const w = T.walkTo ? await T.walkTo(${JSON.stringify(ref)}) : null;
  if (T.pointAt) T.pointAt(${JSON.stringify(ref)}, "Uploading resume");
  const el = document.querySelector('[data-tailor-ref="' + ${JSON.stringify(ref)} + '"]');
  if (!el) return null; const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2, inView: r.bottom > 0 && r.top < innerHeight }; })()`;

module.exports = { dropFile, POINT_JS, STEPS };
