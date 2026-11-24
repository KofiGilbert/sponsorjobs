// The Tracker as a Jira-style board (2026-11-24), under node: the card functions are lifted out of
// ui/static/app.js and run against made-up cards. A card is compact (title, one chip, company and
// ONE date), carries no Resume / Chat / Details links, and a Ready to apply card has one Apply;
// the search box filters by company or title; the inbox strip is hidden when it has nothing.
// tests/test_tracker_jira_board.py runs it. Made-up companies only.
"use strict";
const path = require("path");
const fs = require("fs");
const SRC = fs.readFileSync(path.join(__dirname, "..", "..", "ui", "static", "app.js"), "utf8");
const assert = (c, m) => { if (!c) throw new Error(m); };
const eq = (a, b, m) => assert(JSON.stringify(a) === JSON.stringify(b), `${m}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);

function fn(name) {
  const start = SRC.indexOf(`  function ${name}(`);
  assert(start >= 0, `function ${name} not found`);
  const end = SRC.indexOf("\n  }\n", start);
  return SRC.slice(start, end + 4);
}

// The page's helpers the card code calls, as small stand-ins.
const prelude = `
  const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const trkT = (k, f) => f;
  const TRK_STAGE_EN = { saved: "Saved", in_progress: "In progress", approved: "Ready to apply", applied: "Applied", interviewing: "Interviewing", offer: "Offer", rejected: "Rejected", archived: "Archived" };
  const trkStageName = (s) => TRK_STAGE_EN[s] || s;
  const coName = (j) => (j && (j.display_name || j.company)) || "";
  const companyAvatar = (j) => '<div class="fr-logo"></div>';
  const visaBadges = (j) => (j.visa || []).map((v) => '<span class="vbadge">' + v.code + '</span>').join("");
  const sessAgo = () => "2h ago";
  const trkApplyTip = () => "Opens the application and fills it from your profile. You check it and submit.";
  let TRK_CARDS = [];
  const TRK_FILTER = { q: "", due: false };
  let INBOX_SUGG = [], INBOX_CONNECTED = false;
  const $ = (sel) => HOSTS[sel] || null;
  const HOSTS = {};
`;
const body = ["trkDay", "trkVisible", "trkChip", "trkCardMeta", "trkAskHtml", "trkApplyBtn", "trkCardHtml", "trkMetaHtml", "trkAvatar"].map(fn).join("\n");
const api = new Function(`${prelude}\n${body}\n
  return { trkVisible, trkCardMeta, trkCardHtml, setCards: (c) => { TRK_CARDS = c; }, TRK_FILTER };`)();

const ready = { source_id: "gh:1", company: "Brightwater Freight", title: "Data Analyst", stage: "approved", apply_id: 7,
  tailored_at: "2026-11-20", new: true, ask_submitted: false, visa: [{ code: "H1B" }, { code: "E3" }] };
const applied = { source_id: "gh:2", company: "Larkspur Labs", title: "Platform Engineer", stage: "applied",
  applied_at: "2026-11-23", follow_up_at: "2026-11-29", follow_up_due: false, record: { pdf: "/api/cv.pdf?job=cv-3" },
  has_session: true, session: { pdf: "/x.pdf", updated_at: 1 } };
const dueCard = { ...applied, source_id: "gh:3", company: "Harlow Press", title: "Editor", follow_up_due: true };
const closed = { source_id: "gh:4", company: "Pinecrest Studio", title: "Designer", stage: "archived", closed_at: "2026-11-24" };
const rejected = { source_id: "gh:5", company: "Quillon Ltd", title: "Writer", stage: "rejected" };

// ---- one date per card, chosen by stage
eq(api.trkCardMeta(ready).text.startsWith("Tailored "), true, "ready: Tailored <date>");
eq(api.trkCardMeta(applied).text.startsWith("Applied "), true, "applied: Applied <date>");
const due = api.trkCardMeta(dueCard);
assert(due.text.startsWith("Follow up ") && due.due === true, "a due follow-up replaces the applied date and is highlighted");
assert(api.trkCardMeta(closed).text.startsWith("Job closed "), "closed: Job closed <date>");
eq(api.trkCardMeta(rejected).text, "Not selected", "rejected: Not selected");

// ---- a narrow column shows the date alone: the label is its own span, the whole line on hover
const mh = api.trkCardHtml(applied);
assert(/<span class="trk-meta-l">Applied <\/span>/.test(mh) && /title="Applied /.test(mh), "the label can hide, the title keeps it");

// ---- the card: compact, no Resume / Chat / Details links, one chip
const html = api.trkCardHtml(applied);
assert(!/<a\b/.test(html), "a card has no links");
assert(!/Resume|Chat|Details|data-trk-chat|data-trk-details/.test(html), "no Resume, Chat or Details on a card");
assert(!/page \d+%/.test(html), "no 'Resume 50% · page 95%' line");
assert(!/data-trk-apply/.test(html), "only Ready to apply cards have Apply");
const rhtml = api.trkCardHtml(ready);
eq((rhtml.match(/class="vbadge"/g) || []).length, 1, "at most one chip on a card");
assert(/data-trk-apply/.test(rhtml) && /You check it and submit\./.test(rhtml), "Ready to apply card has Apply with its tip");
assert(/class="trk-new"/.test(rhtml), "a never-opened package says New");
assert(!/Did you submit it\?/.test(rhtml), "no question before an Autofill");
const asked = api.trkCardHtml({ ...ready, new: false, ask_submitted: true });
assert(/Did you submit it\?/.test(asked) && /data-trk-yes/.test(asked) && /data-trk-notyet/.test(asked), "asks after an Autofill");
assert(!/class="trk-new"/.test(asked), "an opened package is not New");
assert(!/data-trk-apply/.test(api.trkCardHtml({ ...ready, apply_id: null })), "no Apply without a package to fill");

// ---- the search box: every word in the company or the title; the due chip
api.setCards([ready, applied, dueCard, closed]);
eq(api.trkVisible().length, 4, "no filter: every card");
api.TRK_FILTER.q = "larkspur";
eq(api.trkVisible().map((c) => c.source_id), ["gh:2"], "matches the company");
api.TRK_FILTER.q = "data analyst";
eq(api.trkVisible().map((c) => c.source_id), ["gh:1"], "matches every word of the title");
api.TRK_FILTER.q = "nothing like this";
eq(api.trkVisible().length, 0, "no match");
api.TRK_FILTER.q = "";
api.TRK_FILTER.due = true;
eq(api.trkVisible().map((c) => c.source_id), ["gh:3"], "Follow-ups due shows only due ones");

// ---- the inbox strip hides when it has nothing, even with Gmail connected
const strip = new Function(`${prelude}\n${fn("renderInboxStrip")}\n
  HOSTS["#trkInbox"] = { hidden: false, innerHTML: "x", querySelectorAll: () => [], querySelector: () => null };
  INBOX_CONNECTED = true;
  renderInboxStrip();
  return HOSTS["#trkInbox"];`)();
assert(strip.hidden === true && strip.innerHTML === "", "the inbox strip is hidden when it has no items");

console.log("ok tracker board");
