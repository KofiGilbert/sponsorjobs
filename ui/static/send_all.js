// Send all's pure parts (docs/apply-agent.md section 31): which applications hold a best guess on
// a legal question, the ONE confirmation that names them before anything is sent, and what each
// of its three choices sends. No DOM and no network, so tests/js/test_send_all.js drives it under
// node; app.js paints the confirmation and calls the shell.
(function (root) {
  "use strict";

  const CHOICES = ["review", "others", "all"];

  /** {held, clear}: the applications with a best guess on a legal question, and the rest. */
  function split(rows) {
    const held = [], clear = [];
    for (const r of rows || []) ((Number(r && r.guessed_legal) || 0) > 0 ? held : clear).push(r);
    return { held, clear };
  }

  /** Is a confirmation needed before Send all? Only when one of them holds a guessed legal answer. */
  function needsConfirm(rows) {
    return split(rows).held.length > 0;
  }

  const roleAt = (r, t) => {
    const role = String((r && r.role) || "").trim(), co = String((r && r.company) || "").trim();
    if (role && co) return t("aa.ans.roleAt", "{role} at {company}").replace("{role}", role).replace("{company}", co);
    return role || co || t("sendall.unnamed", "An application");
  };

  /** What the confirmation says, in the person's language (``t``: the app's translator). */
  function confirmModel(rows, t) {
    t = t || ((k, en) => en);
    const { held, clear } = split(rows);
    const n = held.length;
    const title = n === 1 ? t("sendall.title1", "1 application has best guesses on legal questions")
                          : t("sendall.titleN", "{n} applications have best guesses on legal questions").replace("{n}", n);
    const lead = t("sendall.lead", "I answered these with my best guess because nothing you saved said. Check them before they are sent.");
    const items = held.map((r) => {
      const k = Number(r.guessed_legal) || 0;
      return { id: r.id, text: roleAt(r, t),
               count: k === 1 ? t("sendall.count1", "1 guessed legal answer") : t("sendall.countN", "{n} guessed legal answers").replace("{n}", k) };
    });
    const choices = [
      { id: "review", label: t("sendall.review", "Review them first"), primary: true },
      { id: "others", label: clear.length === 1 ? t("sendall.others1", "Send the other 1, keep these")
                                                : t("sendall.othersN", "Send the other {n}, keep these").replace("{n}", clear.length),
        disabled: clear.length === 0 },
      { id: "all", label: t("sendall.all", "Send all anyway") },
    ];
    return { title, lead, items, choices };
  }

  /** What a choice does: {send: [rows to send], review: id of the application to open, or null}.
   *  Anything else (closed, Escape) sends nothing. */
  function decide(choice, rows) {
    const { held, clear } = split(rows);
    if (choice === "all") return { send: (rows || []).slice(), review: null };
    if (choice === "others") return { send: clear, review: null };
    if (choice === "review") return { send: [], review: held.length ? held[0].id : null };
    return { send: [], review: null };
  }

  const api = { CHOICES, split, needsConfirm, confirmModel, decide };
  root.SJSendAll = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
