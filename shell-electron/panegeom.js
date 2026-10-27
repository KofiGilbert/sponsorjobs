/* The in-app browser pane's geometry, pure so tests can pin it (tests/test_pane_geometry.py).
 *
 * The page measures the pane's slot with getBoundingClientRect(), in CSS pixels. Electron's
 * WebContentsView.setBounds() takes window points. The two agree only at 100% zoom: with the app
 * zoomed out to 76% (View > Zoom Out) the pane landed too far right and too wide, cut off past
 * the window edge (Kofi, 2026-10-27). So the slot is scaled by the app page's zoom factor. */
function paneRect(slot, zoom) {
  const z = Number(zoom) > 0 ? Number(zoom) : 1;
  if (!slot) return { x: 0, y: 0, width: 0, height: 0 };
  const r = (v) => Math.max(0, Math.round((Number(v) || 0) * z));
  return { x: r(slot.x), y: r(slot.y), width: r(slot.width), height: r(slot.height) };
}

module.exports = { paneRect };
