/* SponsorJobs i18n — a small, dependency-free client-side layer.

   English is the SOURCE: the text already written in index.html and passed as fallbacks in
   JS is the canonical copy, so nothing breaks if a translation is missing. Other languages
   live in /static/locales/<code>.json, keyed by the same dot-keys.

   Static markup opts in with attributes:
     data-i18n="key"          -> element.textContent
     data-i18n-ph="key"       -> element placeholder
     data-i18n-title="key"    -> element title
     data-i18n-aria="key"     -> element aria-label
   Dynamic JS strings use  window.I18N.t("key", "English fallback").

   Loading the chosen dictionary is async; on a local app it resolves in a few ms. Missing
   keys fall back to the English already on the page, so a partial translation degrades
   gracefully instead of showing blanks. */
(function () {
  "use strict";
  var KEY = "tailor-lang";
  var DICT = {};   // current non-English dictionary; {} means English (use the source text)

  // The languages offered in the picker. Keep names in their OWN language (endonyms).
  var LANGS = [
    { code: "en", name: "English" },
    { code: "es", name: "Español" },
    { code: "fr", name: "Français" },
    { code: "de", name: "Deutsch" },
    { code: "pt", name: "Português" },
  ];

  function getLang() { return localStorage.getItem(KEY) || "en"; }

  function t(key, fallback) {
    if (DICT && DICT[key] != null) return DICT[key];
    return fallback != null ? fallback : key;
  }

  function _set(el, attr, k) { if (DICT[k] != null) el.setAttribute(attr, DICT[k]); }

  function apply(root) {
    root = root || document;
    root.querySelectorAll("[data-i18n]").forEach(function (el) {
      var k = el.getAttribute("data-i18n");
      if (DICT[k] != null) el.textContent = DICT[k];
    });
    root.querySelectorAll("[data-i18n-ph]").forEach(function (el) { _set(el, "placeholder", el.getAttribute("data-i18n-ph")); });
    root.querySelectorAll("[data-i18n-title]").forEach(function (el) { _set(el, "title", el.getAttribute("data-i18n-title")); });
    root.querySelectorAll("[data-i18n-aria]").forEach(function (el) { _set(el, "aria-label", el.getAttribute("data-i18n-aria")); });
  }

  function load(lang) {
    // Load the dictionary for EVERY language, English included: apply() sets textContent from
    // DICT, so switching back to English must have the English strings on hand to restore
    // (the source text on the page was already overwritten by the previous language).
    return fetch("/static/locales/" + (lang || "en") + ".json")
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (d) { DICT = d || {}; })
      .catch(function () { DICT = {}; });
  }

  function setLang(lang) {
    localStorage.setItem(KEY, lang);
    document.documentElement.setAttribute("lang", lang);
    return load(lang).then(function () {
      apply(document);
      // Views re-render after language changes so JS-built strings pick up t() too.
      document.dispatchEvent(new CustomEvent("i18n:changed", { detail: { lang: lang } }));
    });
  }

  // Apply the stored language as early as possible (before app.js renders its views).
  var ready = load(getLang()).then(function () {
    document.documentElement.setAttribute("lang", getLang());
    if (document.readyState !== "loading") apply(document);
    else document.addEventListener("DOMContentLoaded", function () { apply(document); });
  });

  window.I18N = { t: t, apply: apply, setLang: setLang, getLang: getLang, langs: LANGS, ready: ready };
})();
