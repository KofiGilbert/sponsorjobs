// The Answers section of an application's panel on Auto-apply (Waiting on you): what the agent
// put on the form, read from the application's own record, so a quiet (Auto) run can be checked
// before Send. The pure part: the order (the lines to check first), the counts, and the collapse
// of a long list to those lines plus "Show all N". No DOM and no network, so
// tests/js/test_answers_panel.js drives it under node; app.js paints it with the bar's own rows
// and words (aaAnsRowHTML, aaAnsFrom, aaAnsFlag).
(function (root) {
  "use strict";

  const LIMIT = 8;

  /** Does this row count toward "N to check"? Every flag but an optional box left empty. */
  const counts = (flag) => !!flag && flag !== "left_empty";

  /**
   * The panel's model of a Review answers list ({rows, files, flagged}).
   * opts.from(code): the origin's words; opts.note(row): the reason line for a flagged row
   * ("" for none); opts.fileNote(file), opts.fileFrom(file), opts.fileKind(file): the same for a
   * file; opts.empty: the words for an empty value; opts.held: the words for a
   * self-identification value the record does not keep; opts.page(n), opts.filesHead: group
   * headings; opts.expanded: show every line; opts.limit: the longest list shown whole.
   * Returns {total, check, items, groups, shown, more, collapsed}: ``check`` lines to check,
   * ``groups`` [{head, items}] in paint order (the lines to check first, then the rest by page,
   * then the files), ``more`` lines hidden behind "Show all N".
   */
  function model(list, opts) {
    const o = opts || {};
    const L = list || {};
    const limit = Number(o.limit) > 0 ? Number(o.limit) : LIMIT;
    const items = [];
    (L.rows || []).forEach((r, i) => {
      const note = r.flag ? String((o.note && o.note(r)) || "") : "";
      const held = !!(r.held && !r.value);
      items.push({
        kind: "row", i, page: Number(r.page) || 1, q: String(r.label || ""),
        v: held ? (o.held || "") : (r.value ? String(r.value) : (o.empty || "")),
        from: String((o.from && o.from(r.from)) || r.from || ""),
        note, flag: note !== "", check: counts(r.flag), held,
      });
    });
    (L.files || []).forEach((f, i) => {
      const note = String((o.fileNote && o.fileNote(f)) || "");
      items.push({
        kind: "file", i, page: 0, q: String((o.fileKind && o.fileKind(f)) || f.kind || ""),
        v: String(f.name || ""), from: String((o.fileFrom && o.fileFrom(f)) || ""),
        note, flag: note !== "", check: !!f.flag, held: false,
      });
    });
    const check = items.filter((x) => x.check).length;
    const total = items.length;
    const first = items.filter((x) => x.check);
    const rest = items.filter((x) => !x.check);
    const collapsed = !o.expanded && total > limit;
    // Collapsed: the lines to check, else (nothing to check) the first lines of the list.
    const keep = collapsed ? (first.length ? [] : rest.slice(0, limit)) : rest;
    const groups = [];
    if (first.length) groups.push({ head: "", items: first });
    let cur = null;
    for (const x of keep) {
      const head = x.kind === "file" ? (o.filesHead || "") : (o.page ? o.page(x.page) : "");
      if (!cur || cur.head !== head) { cur = { head, items: [] }; groups.push(cur); }
      cur.items.push(x);
    }
    const shown = groups.reduce((n, g) => n + g.items.length, 0);
    return { total, check, items, groups, shown, more: total - shown, collapsed };
  }

  const api = { LIMIT, model };
  root.SJAnswersPanel = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
