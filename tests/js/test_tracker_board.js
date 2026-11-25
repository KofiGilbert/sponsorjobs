// The Tracker as a Jira-style board of five STATES (2026-11-25), under node: the card and column
// functions are lifted out of ui/static/app.js and run against made-up cards.
// * five columns: To apply (saved, in progress and resume-ready jobs), Applied, Interviewing,
//   Offer, Closed (rejected and archived);
// * a card carries information only: no button and no link on its face, one status lozenge per
//   state, at most one sponsorship tag, the company and ONE date;
// * the side panel's primary action is named for the state (Apply / Continue tailoring / Tailor
//   resume) and it, not the card, asks "Did you submit it?";
// * a drop on a column maps to the right underlying stage;
// * the search box and the two quick filters; the inbox strip is hidden when it has nothing.
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
function konst(name, close) {
  const start = SRC.indexOf(`  const ${name} = `);
  assert(start >= 0, `const ${name} not found`);
  const end = SRC.indexOf(`\n  ${close};\n`, start);
  return SRC.slice(start, end + close.length + 5);
}

// The page's helpers the card code calls, as small stand-ins.
const prelude = `
  const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const trkT = (k, f) => f;
  const TRK_STAGES = ["saved", "in_progress", "approved", "applied", "interviewing", "offer", "rejected", "archived"];
  const TRK_STAGE_EN = { saved: "Saved", in_progress: "In progress", approved: "Resume ready", applied: "Applied", interviewing: "Interviewing", offer: "Offer", rejected: "Rejected", archived: "Archived" };
  const trkStageName = (s) => TRK_STAGE_EN[s] || s;
  const coName = (j) => (j && (j.display_name || j.company)) || "";
  const companyAvatar = (j) => '<div class="fr-logo"></div>';
  const visaBadges = (j) => (j.visa || []).map((v) => '<span class="vbadge">' + v.code + '</span>').join("");
  const safeHref = (u) => u;
  const trkApplyTip = () => "Opens the application and fills it from your profile. You check it and submit.";
  let TRK_CARDS = [];
  const TRK_FILTER = { q: "", due: false, ready: false };
  let INBOX_SUGG = [], INBOX_CONNECTED = false, INBOX_OPEN = false;
  const MOVES = [];
  const trkMove = (sid, stage) => MOVES.push([sid, stage]);
  const $ = (sel) => HOSTS[sel] || null;
  const HOSTS = {};
`;
const body = [konst("TRK_IC", "}"), konst("TRK_COLS", "]")].concat(["trkIcon", "trkColOf", "trkColName", "trkDropStage", "trkDropOn", "trkState",
  "trkLozHtml", "trkAction", "trkDay", "trkVisible", "trkChip", "trkCardDate", "trkDateHtml", "trkCardHtml", "trkAvatar", "trkPanelActions",
  "trkPanelAsk", "trkStatusSelect", "renderTrackerBoard", "renderTrackerTable"].map(fn)).join("\n");
const api = new Function(`${prelude}\n${body}\n
  return { TRK_COLS, trkColOf, trkDropStage, trkDropOn, trkState, trkAction, trkVisible, trkCardDate, trkCardHtml, trkPanelActions, trkPanelAsk,
    trkStatusSelect, renderTrackerBoard, renderTrackerTable, MOVES, HOSTS, setCards: (c) => { TRK_CARDS = c; }, TRK_FILTER };`)();

const saved = { source_id: "gh:0", company: "Fennel Robotics", title: "Process Engineer", stage: "saved", stage_at: "2026-11-18" };
const tailoring = { source_id: "gh:9", company: "Harlow Press", title: "Data Analyst, Subscriptions", stage: "in_progress", stage_at: "2026-11-19",
  has_session: true, session: { updated_at: 1 } };
const ready = { source_id: "gh:1", company: "Brightwater Freight", title: "Data Analyst", stage: "approved", apply_id: 7,
  tailored_at: "2026-11-20", new: true, ask_submitted: false, record: { id: 7 }, visa: [{ code: "H1B" }, { code: "E3" }] };
const asked = { ...ready, source_id: "gh:8", new: false, ask_submitted: true };
const applied = { source_id: "gh:2", company: "Larkspur Labs", title: "Platform Engineer", stage: "applied",
  applied_at: "2026-11-23", follow_up_at: "2026-11-29", follow_up_due: false, record: { pdf: "/api/cv.pdf?job=cv-3", cover_letter: "/cl.pdf" },
  has_session: true, session: { pdf: "/x.pdf", updated_at: 1 }, url: "https://example.com/job" };
const dueCard = { ...applied, source_id: "gh:3", company: "Harlow Press", title: "Editor", follow_up_due: true };
const interviewing = { source_id: "gh:6", company: "Meridian Works", title: "Data Scientist", stage: "interviewing", stage_at: "2026-11-10" };
const offer = { source_id: "gh:7", company: "Vantor Group", title: "Analyst", stage: "offer", stage_at: "2026-11-12" };
const closed = { source_id: "gh:4", company: "Pinecrest Studio", title: "Designer", stage: "archived", closed_at: "2026-11-24" };
const passed = { source_id: "gh:10", company: "Sable Systems", title: "QA Engineer", stage: "archived", stage_at: "2026-11-21" };
const rejected = { source_id: "gh:5", company: "Quillon Ltd", title: "Writer", stage: "rejected", stage_at: "2026-11-22" };
const ALL = [saved, tailoring, ready, asked, applied, dueCard, interviewing, offer, closed, passed, rejected];

// ---- five columns, and which stages each one groups
eq(api.TRK_COLS.map((c) => c.key), ["todo", "applied", "interviewing", "offer", "closed"], "five columns, in workflow order");
eq(api.TRK_COLS.map((c) => c.name[1]), ["To apply", "Applied", "Interviewing", "Offer", "Closed"], "the column names");
eq(api.TRK_COLS[0].stages, ["saved", "in_progress", "approved"], "To apply holds saved, in progress and resume-ready jobs");
eq(api.TRK_COLS[4].stages, ["rejected", "archived"], "Closed holds rejected and archived jobs");
eq(["saved", "in_progress", "approved", "applied", "interviewing", "offer", "rejected", "archived"].map(api.trkColOf),
  ["todo", "todo", "todo", "applied", "interviewing", "offer", "closed", "closed"], "every stage key has a column");
assert(!/optional/.test(JSON.stringify(api.TRK_COLS)), "no column comes and goes");

// ---- one lozenge per state
const loz = (c) => { const s = api.trkState(c); return s ? [s.text, s.tone] : null; };
eq(loz(saved), ["No resume yet", "neutral"], "saved: No resume yet");
eq(loz(tailoring), ["Tailoring", "info"], "in progress: Tailoring");
eq(loz(ready), ["Resume ready", "success"], "resume ready");
eq(loz(asked), ["Did you submit?", "warn"], "after an Autofill the card says Did you submit?");
eq(loz(applied), null, "applied, nothing due: the column says it all");
eq(loz(dueCard), ["Follow up due", "warn"], "applied with a follow-up due");
eq(loz(interviewing), null, "interviewing: no lozenge");
eq(loz(offer), null, "offer: no lozenge");
eq(loz(rejected), ["Not selected", "neutral"], "rejected: Not selected");
eq(loz(closed), ["Job closed", "neutral"], "a closed posting: Job closed");
eq(loz(passed), ["Archived", "neutral"], "archived by hand");
eq(loz({ ...saved, stage: "approved" }), ["No resume yet", "neutral"], "moved by hand with no resume: still No resume yet");

// ---- the card face: information only
for (const c of ALL) {
  const html = api.trkCardHtml(c);
  assert(!/<button|<a\b|<select|<input/.test(html), `no button, link or control on a card (${c.stage})`);
  assert(!/data-trk-apply|data-trk-yes|data-trk-notyet|trk-new|>Apply<|>New<|>Yes<|>Not yet</.test(html), `no Apply, New, Yes or Not yet on a card (${c.stage})`);
  assert(/class="trk-card-t"/.test(html) && /class="trk-card-foot"/.test(html) && /class="trk-card-co"/.test(html), `same anatomy on every card (${c.stage})`);
  assert((html.match(/class="trk-loz /g) || []).length <= 1 && (html.match(/class="vbadge"/g) || []).length <= 1, `at most one lozenge and one tag (${c.stage})`);
  assert(html.indexOf("trk-card-t") < html.indexOf("trk-card-foot"), "the title comes first, the footer last");
}
const rhtml = api.trkCardHtml(ready);
assert(/data-loz="ready">Resume ready</.test(rhtml) && /class="vbadge">H1B</.test(rhtml), "status lozenge and one sponsorship tag");
assert(rhtml.indexOf("trk-loz") < rhtml.indexOf("vbadge"), "the status lozenge comes before the tag");
assert(/data-loz="ask">Did you submit\?</.test(api.trkCardHtml(asked)), "the asked card shows the amber lozenge");
assert(!/trk-card-loz/.test(api.trkCardHtml(applied)), "no lozenge row when there is nothing to say");
assert(/<svg class="trk-ic"/.test(rhtml) && !/[\u{1F300}-\u{1FAFF}☀-➿]/u.test(ALL.map(api.trkCardHtml).join("")), "icons are inline SVG, never emoji");

// ---- one date per card, chosen by stage; a due follow-up takes its place with a flag
eq(api.trkCardDate(ready).title.startsWith("Tailored "), true, "ready: Tailored <date>");
eq(api.trkCardDate(applied).title.startsWith("Applied "), true, "applied: Applied <date>");
eq(api.trkCardDate(saved).icon, "calendar", "a plain date has the calendar");
const due = api.trkCardDate(dueCard);
assert(due.title.startsWith("Follow up ") && due.due === true && due.icon === "flag", "a due follow-up replaces the applied date, with a flag");
assert(/trk-card-date is-due/.test(api.trkCardHtml(dueCard)), "and is highlighted");
assert(api.trkCardDate(closed).title.startsWith("Job closed "), "closed: Job closed <date>");

// ---- the panel's primary action, by state
eq([saved, tailoring, ready, asked, applied, interviewing, offer, rejected].map(api.trkAction),
  ["tailor", "continue", "apply", "apply", "", "", "", ""], "the action per state");
eq(api.trkAction({ ...saved, has_session: true }), "continue", "a saved job with a resume chat continues it");
const main = (c) => (api.trkPanelActions(c).match(/trk-p-main" type="button" data-trk-act="(\w+)"[^>]*>([^<]+)</) || []).slice(1);
eq(main(saved), ["tailor", "Tailor resume"], "saved: Tailor resume");
eq(main(tailoring), ["continue", "Continue tailoring"], "in progress: Continue tailoring");
eq(main(ready), ["apply", "Apply"], "resume ready: Apply");
eq(main(applied), [], "no primary action once applied");
for (const c of [saved, tailoring, ready, applied]) eq((api.trkPanelActions(c).match(/btn-primary/g) || []).length <= 1, true, "one primary at most");
const acts = api.trkPanelActions(applied);
assert(/Resume/.test(acts) && /Cover letter/.test(acts) && /Chat/.test(acts) && /Job posting/.test(acts), "the quiet links live in the panel");
assert(/You check it and submit\./.test(api.trkPanelActions(ready)), "Apply says what it does");
assert(/Did you submit it\?/.test(api.trkPanelAsk(asked)) && /data-trk-yes/.test(api.trkPanelAsk(asked)) && /data-trk-notyet/.test(api.trkPanelAsk(asked)),
  "the panel asks Did you submit it? Yes / Not yet");
eq(api.trkPanelAsk(ready), "", "no question before an Autofill");

// ---- drop mapping
eq(api.trkDropStage(applied, "todo"), "approved", "back to To apply with a resume: resume ready");
eq(api.trkDropStage({ ...interviewing, has_session: true }, "todo"), "in_progress", "with only a resume chat: in progress");
eq(api.trkDropStage(interviewing, "todo"), "saved", "with no resume: saved");
eq(api.trkDropStage(ready, "applied"), "applied", "onto Applied: applied");
eq(api.trkDropStage(applied, "interviewing"), "interviewing", "onto Interviewing");
eq(api.trkDropStage(interviewing, "offer"), "offer", "onto Offer");
eq(api.trkDropStage(applied, "closed"), "rejected", "an applied job dropped on Closed was not selected");
eq(api.trkDropStage(saved, "closed"), "archived", "a job never applied to is archived");
eq([api.trkDropStage(saved, "todo"), api.trkDropStage(tailoring, "todo"), api.trkDropStage(passed, "closed"), api.trkDropStage(rejected, "closed")],
  ["", "", "", ""], "a drop on its own column changes nothing");
api.setCards(ALL);
api.trkDropOn("gh:1", "applied"); api.trkDropOn("gh:0", "todo"); api.trkDropOn("nope", "offer");
eq(api.MOVES, [["gh:1", "applied"]], "only a real move reaches the engine");

// ---- the board: five columns always, counts, To apply ordered by what can be done next
const host = { innerHTML: "", querySelectorAll: () => [] };
api.HOSTS["#trkBoard"] = host;
api.renderTrackerBoard();
eq((host.innerHTML.match(/<section class="trk-col/g) || []).length, 5, "five columns");
eq((host.innerHTML.match(/data-col="(\w+)"/g) || []), ['data-col="todo"', 'data-col="applied"', 'data-col="interviewing"', 'data-col="offer"', 'data-col="closed"'], "in order");
eq((host.innerHTML.match(/class="trk-col-n">(\d+)</g) || []).map((x) => Number(x.match(/\d+/)[0])), [4, 2, 1, 1, 3], "each header counts its cards");
const todo = host.innerHTML.split('data-col="applied"')[0];
assert(todo.indexOf("Resume ready") < todo.indexOf("Tailoring") && todo.indexOf("Tailoring") < todo.indexOf("No resume yet"), "ready first, then tailoring, then saved");
assert(/trk-col-ok/.test(host.innerHTML.split('data-col="offer"')[1].split("</h2>")[0]), "Offer carries a quiet check");
api.setCards([saved]);
api.renderTrackerBoard();
eq((host.innerHTML.match(/<section class="trk-col/g) || []).length, 5, "empty columns stay");
eq((host.innerHTML.match(/class="trk-col-hint"/g) || []).length, 4, "each empty column says one line");

// ---- the table: the same lozenges, no controls in a row
api.setCards(ALL);
const thost = { innerHTML: "", querySelectorAll: () => [] };
api.HOSTS["#trkTable"] = thost;
api.renderTrackerTable();
const tbody = thost.innerHTML.split("<tbody>")[1];
assert(!/<button|<select|<a\b|<input/.test(tbody), "no buttons or pickers in table rows");
assert(/data-loz="ready"/.test(tbody) && /data-loz="due"/.test(tbody) && /trk-td-go/.test(tbody), "the same lozenges and a quiet chevron");
const sel = api.trkStatusSelect(rejected);
eq((sel.match(/<option/g) || []).length, 6, "the panel's status picker: five columns, Closed as its two outcomes");
assert(/value="rejected" selected/.test(sel) && /value="todo" selected/.test(api.trkStatusSelect(saved)), "it shows where the job is");

// ---- the search box and the quick filters
eq(api.trkVisible().length, ALL.length, "no filter: every card");
api.TRK_FILTER.q = "larkspur";
eq(api.trkVisible().map((c) => c.source_id), ["gh:2"], "matches the company");
api.TRK_FILTER.q = "data analyst";
eq(api.trkVisible().map((c) => c.source_id), ["gh:9", "gh:1", "gh:8"], "matches every word of the title");
api.TRK_FILTER.q = "nothing like this";
eq(api.trkVisible().length, 0, "no match");
api.TRK_FILTER.q = "";
api.TRK_FILTER.due = true;
eq(api.trkVisible().map((c) => c.source_id), ["gh:3"], "Follow-ups due shows only due ones");
api.TRK_FILTER.due = false;
api.TRK_FILTER.ready = true;
eq(api.trkVisible().map((c) => c.source_id), ["gh:1", "gh:8"], "Resume ready shows the jobs that can be applied to now");
api.TRK_FILTER.ready = false;

// ---- the inbox strip hides when it has nothing, even with Gmail connected
const strip = new Function(`${prelude}\n${fn("renderInboxStrip")}\n
  HOSTS["#trkInbox"] = { hidden: false, innerHTML: "x", querySelectorAll: () => [], querySelector: () => null };
  INBOX_CONNECTED = true;
  renderInboxStrip();
  return HOSTS["#trkInbox"];`)();
assert(strip.hidden === true && strip.innerHTML === "", "the inbox strip is hidden when it has no items");

console.log("ok tracker board");
