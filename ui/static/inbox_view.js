// The Inbox screen's pure parts (docs/mail-alias.md): label names, times, the row a message
// becomes, the request a filter makes, which empty state applies, and the page a sanitised mail
// body is shown in. No DOM and no network here, so tests/js/test_inbox_view.js drives all of it
// under node. app.js does the fetching and the painting.
(function (root) {
  "use strict";

  // The order the filter shows them in. The wording is ours: a rejection reads "Not this time".
  const LABELS = ["received", "rejected", "interview", "action", "other"];
  const LABEL_TEXT = {
    received: ["mbox.label.received", "Application received"],
    rejected: ["mbox.label.rejected", "Not this time"],
    interview: ["mbox.label.interview", "Interview request"],
    action: ["mbox.label.action", "Action needed"],
    other: ["mbox.label.other", "Other"],
  };
  const known = (label) => (LABEL_TEXT[label] ? label : "other");

  /** The label in the person's language. ``t`` is the app's translator (key, English). */
  function labelName(label, t) {
    const [key, en] = LABEL_TEXT[known(label)];
    return t ? t(key, en) : en;
  }

  /** The chip's class: received is the quiet positive, rejected muted (never an alarm),
   *  interview the one in ink, action the warning, other neutral. */
  function chipClass(label) {
    return "chip ib-chip ib-chip-" + known(label);
  }

  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

  /** The list's time: today a time ("3:04 PM"), this year a date ("Nov 12"), older with its year. */
  function listTime(at, now, locale) {
    const sec = Number(at);
    if (!sec || !isFinite(sec)) return "";
    const d = new Date(sec * 1000), n = new Date(now == null ? Date.now() : now);
    if (isNaN(d)) return "";
    if (sameDay(d, n)) return d.toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" });
    const opts = { month: "short", day: "numeric" };
    if (d.getFullYear() !== n.getFullYear()) opts.year = "numeric";
    return d.toLocaleDateString(locale, opts);
  }

  /** The reader's time: always the date and the time ("Nov 12, 3:04 PM"), the year when it is not this one. */
  function fullTime(at, now, locale) {
    const sec = Number(at);
    if (!sec || !isFinite(sec)) return "";
    const d = new Date(sec * 1000), n = new Date(now == null ? Date.now() : now);
    if (isNaN(d)) return "";
    const opts = { month: "short", day: "numeric" };
    if (d.getFullYear() !== n.getFullYear()) opts.year = "numeric";
    return d.toLocaleDateString(locale, opts) + ", " + d.toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" });
  }

  /** Who a message is from, as one name: the display name, else the address. */
  function senderName(m) {
    return String((m && (m.sender_name || m.sender)) || "").trim();
  }

  /** One list row: everything the row shows, already worded. */
  function rowModel(m, now, t, locale) {
    const tt = t || ((k, en) => en);
    const unread = !m.read;
    const from = senderName(m) || tt("mbox.unknownSender", "Unknown sender");
    const subject = String(m.subject || "").trim() || tt("mbox.noSubject", "(no subject)");
    const time = listTime(m.at, now, locale);
    const label = known(m.label);
    const labelText = labelName(label, tt);
    const company = String(m.company || "").trim();
    return { id: m.id, unread, from, subject, snippet: String(m.snippet || "").trim(), time, label, labelText,
             chip: chipClass(label), company };
  }

  /** The query string for the list request. ``f``: {label, unread, q, archived, job, page, per}. */
  function listQuery(f) {
    f = f || {};
    const p = [];
    const add = (k, v) => p.push(k + "=" + encodeURIComponent(v));
    if (LABEL_TEXT[f.label]) add("label", f.label);
    if (f.unread) add("unread", 1);
    const q = String(f.q || "").trim();
    if (q) add("q", q);
    if (f.archived) add("archived", 1);
    if (f.job) add("job_id", f.job);
    add("page", Math.max(1, Number(f.page) || 1));
    add("per_page", Math.max(1, Number(f.per) || 50));
    return p.join("&");
  }

  /** Is anything narrowing the list? */
  function isFiltered(f) {
    f = f || {};
    return !!(LABEL_TEXT[f.label] || f.unread || String(f.q || "").trim() || f.archived || f.job);
  }

  /** How many messages the mailbox holds (not archived): the label counts added up. */
  function totalOf(labels) {
    return LABELS.reduce((a, k) => a + (Number(labels && labels[k]) || 0), 0);
  }

  /** Which empty state applies, or "" when there are rows to show.
   *  "noaddr": no address yet and no mail; "nomail": an address, nothing has arrived;
   *  "noresults": there is mail, none of it matches the filter or the search. */
  function emptyKind(s) {
    s = s || {};
    if ((s.rows || 0) > 0) return "";
    if (isFiltered(s.filter)) return "noresults";
    return s.alias ? "nomail" : "noaddr";
  }

  /** "1 message" / "3 messages". */
  function countText(n, t) {
    const tt = t || ((k, en) => en);
    n = Number(n) || 0;
    return n === 1 ? tt("mbox.count1", "1 message") : tt("mbox.countN", "{n} messages").replace("{n}", String(n));
  }

  // The page a mail body is shown in, inside a sandboxed frame with no scripts. Mail HTML assumes
  // a white page, so it gets one in both themes. The policy lets nothing load (no images, fonts,
  // frames or scripts; only the inline styles the sanitiser left), and <base target="_blank">
  // means a link the app did not catch cannot replace the message inside the frame: the sandbox
  // has no popups, so such a click goes nowhere.
  const FRAME_CSP = "default-src 'none'; style-src 'unsafe-inline'";
  const FRAME_CSS = "html{background:#fff;overflow-y:hidden}"
    + "body{margin:0;padding:18px 20px;background:#fff;color:#1b1b1f;"
    + "font:14px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    + "overflow-wrap:anywhere;word-break:break-word}"
    + "a{color:#1b5fd1}table{max-width:100%;border-collapse:collapse}td,th{vertical-align:top}"
    + "pre{white-space:pre-wrap}blockquote{margin:0 0 0 4px;padding-left:12px;border-left:2px solid #d4d4d8;color:#3f3f46}"
    + "hr{border:0;border-top:1px solid #e5e5e8}";
  function frameDoc(htmlSafe) {
    return "<!doctype html><html><head><meta charset=\"utf-8\">"
      + "<meta http-equiv=\"Content-Security-Policy\" content=\"" + FRAME_CSP + "\">"
      + "<base target=\"_blank\"><style>" + FRAME_CSS + "</style></head><body>"
      + String(htmlSafe || "") + "</body></html>";
  }

  /** What a link in a message may do: "open" an http(s) page in the person's browser, "copy" a
   *  mailto address (there is no compose here), or nothing. */
  function linkAction(href) {
    const u = String(href || "").trim();
    if (/^https?:\/\//i.test(u)) return { kind: "open", url: u };
    const m = /^mailto:([^?#]+)/i.exec(u);
    if (m) {
      let addr = m[1];
      try { addr = decodeURIComponent(addr); } catch (e) { /* keep it as written */ }
      return { kind: "copy", text: addr.trim() };
    }
    return { kind: "none" };
  }

  const api = { LABELS, labelName, chipClass, listTime, fullTime, senderName, rowModel, listQuery, isFiltered,
                totalOf, emptyKind, countText, frameDoc, linkAction, FRAME_CSP };
  root.SJInboxView = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
