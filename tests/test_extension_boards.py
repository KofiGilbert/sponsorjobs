"""One adapter per job board (extension/boards_core.js), each run against a trimmed fixture of
the board's markup (2026-10-11).

The extension shows the same things on every board students use: sponsor chips (H-1B, Green
Card, E-Verify, cap-exempt) on each result card and on the open job, the ad-stance chip with the
ad's own sentence quoted under it, and the "Sponsored only" toggle in the corner panel. LinkedIn
is one adapter among seven; content.js is the same for all of them.

Each fixture under tests/fixtures/extension_boards/ is the board's markup trimmed to what the
adapter keys on. Dice is cut from the live server-rendered page fetched with curl; Indeed,
Glassdoor and ZipRecruiter answer curl with a bot wall, Google with an "enable JavaScript"
shell and Handshake with its login page, so those fixtures follow each board's documented hooks
(data-testid / data-test / data-hook names, employer-link URL shapes, CSS-module prefixes), and
the adapters say so in their `evidence`.

The harness is the fake DOM from tests/test_extension_sponsored_only.py, fed a fixture instead
of a hand-built column, with content.js run the way Chrome runs it (boards_core.js first).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

EXT = Path("extension").resolve()
FIX = Path("tests/fixtures/extension_boards").resolve()
BOARDS_SRC = (EXT / "boards_core.js").read_text(encoding="utf-8")

HARNESS = r"""
const fs = require('fs'), path = require('path');
const [extDir, fixture, boardId, hostname, pathname, search, initialToggle, grant] = process.argv.slice(2);
globalThis.window = globalThis;
require(path.join(extDir, 'parity_core.js'));
require(path.join(extDir, 'adstance_core.js'));
const B = require(path.join(extDir, 'boards_core.js'));

const VOID = new Set(['img', 'input', 'br']);
class El {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = { ...attrs }; this.children = [];
    this.parentElement = null; this._text = ''; this.style = {}; this.dataset = {}; this.listeners = {};
  }
  get className() { return this.attrs.class || ''; } set className(v) { this.attrs.class = v; }
  get id() { return this.attrs.id || ''; }
  get classList() {
    const self = this, has = (c) => self.className.split(/\s+/).includes(c);
    return { contains: has,
      add: (c) => { if (!has(c)) self.className = (self.className + ' ' + c).trim(); },
      remove: (c) => { self.className = self.className.split(/\s+/).filter(x => x && x !== c).join(' '); },
      toggle: (c, on) => { (on === undefined ? !has(c) : on) ? self.classList.add(c) : self.classList.remove(c); } };
  }
  get hidden() { return 'hidden' in this.attrs; } set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  get checked() { return 'checked' in this.attrs; } set checked(v) { if (v) this.attrs.checked = ''; else delete this.attrs.checked; }
  get textContent() { return this._text + this.children.map(k => k.textContent).join(''); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get innerText() { return [this._text, ...this.children.map(k => k.innerText)].filter(Boolean).join('\n'); }
  get innerHTML() { return this._html || ''; }
  set innerHTML(h) { this._html = h; this.children = []; this._text = ''; parseInto(this, h); }
  get firstElementChild() { return this.children[0] || null; }
  appendChild(k) { k.parentElement = this; this.children.push(k); return k; }
  insertAdjacentElement(where, el) {
    if (where === 'afterbegin') { el.parentElement = this; this.children.unshift(el); }
    else if (where === 'beforeend') this.appendChild(el);
    else if (where === 'afterend' && this.parentElement) {
      const sib = this.parentElement.children, i = sib.indexOf(this);
      el.parentElement = this.parentElement; sib.splice(i + 1, 0, el);
    }
  }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(k => k !== this); }
  getAttribute(k) { return this.attrs[k]; } setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  fire(t, ev) { for (const fn of this.listeners[t] || []) fn(ev || {}); }
  contains(n) { for (; n; n = n.parentElement) if (n === this) return true; return false; }
  closest(sel) { for (let n = this; n; n = n.parentElement) if (matches(n, sel)) return n; return null; }
  querySelectorAll(sel) { const out = []; (function walk(n) { for (const k of n.children) { if (matches(k, sel)) out.push(k); walk(k); } })(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
function parseInto(parent, html) {   // tags, attributes, text; the fixtures carry no comments or entities
  const re = /<(\/?)([a-zA-Z0-9]+)([^>]*)>|([^<]+)/g;
  const stack = [parent]; let m;
  while ((m = re.exec(html))) {
    if (m[4] !== undefined) { stack[stack.length - 1]._text += m[4]; continue; }
    const [, close, tag, rest] = m;
    if (close) { if (stack.length > 1) stack.pop(); continue; }
    const attrs = {};
    for (const a of rest.matchAll(/([a-zA-Z-]+)(?:="([^"]*)")?/g)) attrs[a[1]] = a[2] === undefined ? '' : a[2];
    const el = new El(tag, attrs);
    stack[stack.length - 1].appendChild(el);
    if (!VOID.has(tag.toLowerCase()) && !rest.trim().endsWith('/')) stack.push(el);
  }
}
function matchCompound(n, c) {
  const tag = (c.match(/^[a-zA-Z0-9]+/) || [''])[0];
  if (tag && n.tagName !== tag.toUpperCase()) return false;
  for (const p of c.slice(tag.length).matchAll(/\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:([*^$]?)=["']([^"']*)["'])?\]/g)) {
    if (p[1] !== undefined) { if (!n.classList.contains(p[1])) return false; }
    else if (p[2] !== undefined) { if (n.attrs.id !== p[2]) return false; }
    else {
      const v = n.attrs[p[3]];
      if (v === undefined) return false;
      if (p[5] !== undefined) {
        if (p[4] === '*' ? !v.includes(p[5]) : p[4] === '^' ? !v.startsWith(p[5]) : p[4] === '$' ? !v.endsWith(p[5]) : v !== p[5]) return false;
      }
    }
  }
  return true;
}
function matches(n, sel) {
  return sel.split(',').some(part => {
    const comps = part.trim().split(/\s+/).filter(Boolean);
    if (!comps.length || !matchCompound(n, comps[comps.length - 1])) return false;
    let a = n.parentElement;
    for (let i = comps.length - 2; i >= 0; i--) {
      while (a && !matchCompound(a, comps[i])) a = a.parentElement;
      if (!a) return false; a = a.parentElement;
    }
    return true;
  });
}

const body = new El('body');
parseInto(body, fs.readFileSync(fixture, 'utf8'));
const document = {
  body, createElement: (t) => new El(t),
  querySelectorAll: (s) => body.querySelectorAll(s), querySelector: (s) => body.querySelector(s),
};
const getComputedStyle = (n) => ({ flexDirection: n.attrs['data-flex'] || 'row', display: n.attrs['data-display'] || 'block', gridTemplateColumns: '' });
const location = { hostname, pathname, search };

// What the adapter alone reads off the fixture, before content.js touches it.
const A = B.byId(boardId);
B.setEnv({ getComputedStyle });
const pick = (i) => i && { company: i.company, title: i.title || '', location: i.location || '' };
const pre = {
  detected: (B.detect(location) || {}).id || null,
  cards: A.cards(document).map(c => pick(A.cardInfo(c))),
  posting: pick(A.posting(document)),
  header: (() => { const h = A.header(document); return h ? { tag: h.tagName, cls: h.className, parentCls: h.parentElement && h.parentElement.className } : null; })(),
  descriptionLen: (A.description(document) || '').length,
};

const LOOKUPS = {
  'Acme Sponsor': { ok: true, matched: true, matched_name: 'Acme Sponsor', role_in_us: true, visa: [{ code: 'H-1B' }, { code: 'GREEN-CARD' }], profile: { h1b_approvals: 40, perm_certs: 3, e_verify: false } },
  'No Record Co': { ok: true, matched: false, visa: [], profile: null },
  'Everify Hospital': { ok: true, matched: true, matched_name: 'Everify Hospital', role_in_us: true, visa: [{ code: 'STEM-OPT' }], profile: { h1b_approvals: 0, perm_certs: 0, e_verify: true } },
};
const store = { sj_sponsored_only: initialToggle === 'on' };
const sent = [];
const chrome = {
  runtime: { id: 'test-ext', lastError: null,
    sendMessage: (m, cb) => {
      sent.push(m);
      if (m.type === 'lookup') return cb(LOOKUPS[m.company] || { ok: true, matched: false });
      if (m.type === 'board_grant') return cb({ ok: true, granted: grant !== 'no' });
      if (m.type === 'board_pending') return cb({ ok: true });
      cb({ ok: false, error: 'cant_reach_app' });
    } },
  storage: { local: { get: (k, cb) => cb({ [k]: store[k] }), set: (o) => Object.assign(store, o) } },
};
const MutationObserver = function () { this.observe = () => {}; };
const src = fs.readFileSync(path.join(extDir, 'content.js'), 'utf8');
new Function('document', 'chrome', 'location', 'MutationObserver', 'setTimeout', 'clearTimeout', 'getComputedStyle',
  src)(document, chrome, location, MutationObserver, setTimeout, clearTimeout, getComputedStyle);

setTimeout(() => {
  const cards = A.cards(document);
  const cardState = cards.map((c, i) => ({ company: (pre.cards[i] || {}).company, display: c.style.display || '',
    badges: c.querySelectorAll('.tailor-card-badges .tailor-badge').map(b => b.textContent) }));
  const main = body.querySelectorAll('.tailor-badges').find(r => !r.classList.contains('tailor-card-badges')) || null;
  const header = A.header(document);
  let headerOk = false, mainParent = null;
  if (main && main.parentElement) {
    const sib = main.parentElement.children;
    headerOk = !!header && sib.indexOf(main) === sib.indexOf(header) + 1;
    mainParent = main.parentElement.className || main.parentElement.tagName;
  }
  const stance = main && main.querySelector('.tb-stance');
  const quote = main && main.querySelector('.tailor-ad-quote');
  const lookups = sent.filter(m => m.type === 'lookup');
  const corner = body.querySelector('.tailor-corner');
  corner.querySelector('.tailor-corner-btn').fire('click');
  const panel = corner.querySelector('.tailor-corner-panel');
  const note = corner.querySelector('.tailor-corner-note');
  const box = panel.querySelector('.tailor-cp-sponsored');
  const opened = { checked: box ? box.checked : null, count: box ? panel.querySelector('.tailor-cp-count').textContent : null };
  if (box) { box.checked = !box.checked; panel.fire('change', { target: box }); }
  const after = A.cards(document).map((c, i) => ({ company: (pre.cards[i] || {}).company, display: c.style.display || '' }));
  console.log(JSON.stringify({ pre, cards: cardState, headerOk, mainParent, mainChips: main ? main.querySelectorAll('.tailor-badge').map(b => b.textContent) : [],
    stance: stance ? stance.textContent : null, quote: quote ? quote.textContent : null,
    lookup: lookups[0] ? { company: lookups[0].company, location: lookups[0].location } : null,
    opened, after, store: store.sj_sponsored_only, sentTypes: sent.map(m => m.type),
    note: note.hidden ? null : note.textContent, panelText: panel.textContent,
    cardBadges: body.querySelectorAll('.tailor-card-badges').length }));
}, 30);
"""

# board id -> (hostname, pathname, search, expected posting location substring, expected header parent class)
BOARDS = {
    "linkedin":    ("www.linkedin.com", "/jobs/search-results/", "", "TX", "top-card"),
    "indeed":      ("www.indeed.com", "/jobs", "?q=data+engineer&l=Dallas%2C+TX", "Dallas, TX", "jobsearch-RightPane"),
    "glassdoor":   ("www.glassdoor.com", "/Job/dallas-tx-data-engineer-jobs-SRCH_IL.0,9_IC1139977_KO10,23.htm", "", "Dallas, TX", "JobDetails_jobDetailsHeader__Hd9M3"),
    "handshake":   ("app.joinhandshake.com", "/job-search/101", "", "Dallas, TX", "DIV"),
    "dice":        ("www.dice.com", "/jobs", "?q=data%20engineer&location=Dallas%2C%20TX", "Dallas, Texas", "grid grid-rows-4"),
    "google":      ("www.google.com", "/search", "?q=data+engineer+jobs+dallas&udm=8", "Dallas, TX", "whazf"),
    "ziprecruiter": ("www.ziprecruiter.com", "/jobs-search", "?search=data+engineer&location=Dallas%2C+TX", "Dallas, TX", "job_details"),
}
CARDS = [("Acme Sponsor", "Data Engineer", "Dallas"), ("No Record Co", "Analyst", "Austin"), ("Everify Hospital", "Nurse", "Chicago")]


def _run(board: str, toggle: str, grant: str = "yes") -> dict:
    h = Path("build") / f"_boards_{board}_{toggle}_{grant}.js"
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text(HARNESS, encoding="utf-8")
    host, p, q, _, _ = BOARDS[board]
    r = subprocess.run(["node", str(h), str(EXT), str(FIX / f"{board}.html"), board, host, p, q, toggle, grant],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip())


@pytest.mark.parametrize("board", list(BOARDS))
def test_the_adapter_reads_every_card_and_the_open_job(board):
    out = _run(board, "off")
    pre = out["pre"]
    assert pre["detected"] == board
    assert [(c["company"], c["title"]) for c in pre["cards"]] == [(c, t) for c, t, _ in CARDS]
    for got, (_, _, city) in zip(pre["cards"], CARDS):
        assert city in got["location"], (board, got)
    assert pre["posting"] and pre["posting"]["company"] == "Acme Sponsor", (board, pre["posting"])
    assert pre["posting"]["title"] == "Data Engineer"
    assert pre["descriptionLen"] > 200
    # What content.js then asks the sponsor index: this employer, this role's location.
    assert out["lookup"]["company"] == "Acme Sponsor"
    assert BOARDS[board][3] in out["lookup"]["location"], (board, out["lookup"])


@pytest.mark.parametrize("board", list(BOARDS))
def test_the_chip_row_sits_where_the_adapter_header_says_with_the_stance_and_the_quote(board):
    out = _run(board, "off")
    assert out["pre"]["header"], board
    assert out["headerOk"], (board, out["mainParent"], out["pre"]["header"])
    assert BOARDS[board][4] in (out["mainParent"] or ""), (board, out["mainParent"])
    # The ad's own words come first in the row, with the sentence quoted under the chips.
    assert out["stance"] == "No sponsorship for this role", (board, out["stance"])
    assert out["quote"] and "not eligible for visa sponsorship" in out["quote"], (board, out["quote"])
    assert out["mainChips"][0] == "No sponsorship for this role"
    assert "H-1B" in out["mainChips"] and "Green Card" in out["mainChips"]


@pytest.mark.parametrize("board", list(BOARDS))
def test_each_card_is_badged_and_sponsored_only_hides_the_no_record_card(board):
    out = _run(board, "on")
    by = {c["company"]: c for c in out["cards"]}
    assert by["Acme Sponsor"]["badges"][:2] == ["H-1B", "Green Card"] and by["Acme Sponsor"]["display"] == ""
    assert by["Everify Hospital"]["badges"] == ["E-Verify"] and by["Everify Hospital"]["display"] == ""
    assert by["No Record Co"]["badges"] == ["No sponsor record"]
    assert by["No Record Co"]["display"] == "none"                      # the toggle, as CSS on our side
    assert out["opened"] == {"checked": True, "count": "Hiding 1 of 3"}
    # Flipped off in the panel: every card back, choice saved.
    assert all(c["display"] == "" for c in out["after"]), out["after"]
    assert out["store"] is False


@pytest.mark.parametrize("board", [b for b in BOARDS if b != "linkedin"])
def test_a_board_that_is_not_enabled_yet_is_not_read_at_all(board):
    """Each board beyond LinkedIn is an optional host permission. Until the background confirms
    the grant, content.js asks for nothing (no lookup, no badge, no toggle) and the corner mark
    only says where to enable it. It remembers the board so the popup offers it first."""
    out = _run(board, "on", grant="no")
    assert out["sentTypes"] == ["board_grant", "board_pending"]
    assert out["cardBadges"] == 0 and out["stance"] is None and out["lookup"] is None
    assert all(c["display"] == "" for c in out["cards"])             # nothing hidden either
    assert out["opened"]["checked"] is None                           # no toggle offered
    name = {"indeed": "Indeed", "glassdoor": "Glassdoor", "handshake": "Handshake", "dice": "Dice",
            "google": "Google Jobs", "ziprecruiter": "ZipRecruiter"}[board]
    assert out["note"] == f"Enable on {name} from the SponsorJobs icon"
    assert f"Enable on {name}" in out["panelText"]


def test_linkedin_needs_no_grant():
    out = _run("linkedin", "off", grant="no")
    assert "board_grant" not in out["sentTypes"] and out["lookup"]["company"] == "Acme Sponsor"


def test_google_jobs_is_read_only_in_the_jobs_panel_never_on_a_plain_search():
    r = subprocess.run(["node", "-e",
        "const B=require(process.argv[1]);const d=(s)=>{const b=B.detect({hostname:'www.google.com',pathname:'/search',search:s});return b&&b.id};"
        "console.log(JSON.stringify([d('?q=data+engineer+jobs'), d('?q=data+engineer+jobs&udm=8'), d('?q=x&ibp=htl;jobs'), d('?q=x&ibp=htl%3Bjobs')]))",
        str(EXT / "boards_core.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [None, "google", "google", "google"]


def test_no_adapter_clicks_scrolls_fetches_or_fires_events():
    """CLAUDE.md s7: the adapters read what is on screen and nothing else. On Handshake (login
    gated, React-rendered) that means coping with lazy markup by being re-run on mutation, never
    by asking the page to render more."""
    code = "\n".join(l for l in BOARDS_SRC.splitlines() if not l.lstrip().startswith("//"))
    for forbidden in (".click(", "scrollIntoView", "scrollTo(", "scrollBy(", "scrollTop", "fetch(", "XMLHttpRequest",
                      "dispatchEvent", ".focus(", ".submit(", "requestSubmit", "window.open", "location.href",
                      "addEventListener", "setTimeout", "setInterval", "MutationObserver", "innerHTML", "sendMessage"):
        assert forbidden not in code, forbidden
    # And the list code in content.js stays styling-only, as before.
    content = (EXT / "content.js").read_text(encoding="utf-8")
    lst = content[content.index("// ---- In-list:"):content.index("// ---- Our mark in the corner")]
    for forbidden in (".click()", "scrollIntoView", "scrollTo(", "scrollBy(", "fetch(", "XMLHttpRequest", "dispatchEvent"):
        assert forbidden not in lst, forbidden
