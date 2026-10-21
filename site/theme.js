// Light or dark. Loaded in <head> without defer so the stored choice applies before first paint.
// No stored choice: the page follows the system setting through CSS (prefers-color-scheme).
(function () {
  var KEY = "sj-theme";
  var root = document.documentElement;
  function stored() {
    try { var v = localStorage.getItem(KEY); return v === "light" || v === "dark" ? v : null; } catch (_e) { return null; }
  }
  function current() {
    var s = root.getAttribute("data-theme");
    if (s) return s;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function label(btn) {
    var next = current() === "dark" ? "light" : "dark";
    btn.setAttribute("aria-label", "Switch to " + next + " theme");
    btn.setAttribute("title", "Switch to " + next + " theme");
  }
  var s = stored();
  if (s) root.setAttribute("data-theme", s);
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.hidden = false;
    label(btn);
    btn.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(KEY, next); } catch (_e) { /* private window: the choice lasts this page only */ }
      label(btn);
    });
  });
})();
