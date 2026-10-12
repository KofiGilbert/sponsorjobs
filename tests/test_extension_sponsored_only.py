"""The "Sponsored only" toggle on result lists (2026-10-11).

On LinkedIn and Indeed search results the extension badges each card from the sponsor index.
The toggle in our corner panel hides, with CSS only, the cards whose employer has NO
sponsorship record (no H-1B, no green card, no E-Verify) and shows "Hiding N of M". Rules:

* A card whose lookup has not answered yet stays visible. Never hide on missing data.
* Off restores everything. The choice persists in chrome.storage.local.
* It is styling on OUR side of the page: display:none on the card element. Nothing is clicked,
  scrolled, or asked of the site (CLAUDE.md s7).

The decision logic is pure (extension/parity_core.js) and tested in node directly; the DOM
wiring in content.js is run against a small fake LinkedIn results column, the way
tests/test_extension_content.py runs it against a fake posting.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

CORE = Path("extension/parity_core.js").resolve()
CONTENT = Path("extension/content.js").resolve()
EXT = Path("extension").resolve()

PURE = r"""
const P = require(require('path').resolve(process.argv[2]));
const rec = (p) => ({ ok: true, matched: true, role_in_us: true, profile: p });
const out = {
  record: {
    h1b:      P.hasSponsorRecord(rec({ h1b_approvals: 12, perm_certs: 0, e_verify: false })),
    perm:     P.hasSponsorRecord(rec({ h1b_approvals: 0, perm_certs: 3, e_verify: false })),
    everify:  P.hasSponsorRecord(rec({ h1b_approvals: 0, perm_certs: 0, e_verify: true })),
    capOnly:  P.hasSponsorRecord(rec({ h1b_approvals: 0, perm_certs: 0, e_verify: false, cap_exempt: true })),
    none:     P.hasSponsorRecord({ ok: true, matched: false }),
    offline:  P.hasSponsorRecord({ ok: false, error: 'cant_reach_app' }),
    missing:  P.hasSponsorRecord(null),
    oldApp:   P.hasSponsorRecord({ ok: true, matched: true, visa: [{ code: 'H-1B' }] }),
  },
  passes: {
    on_record:   P.cardPasses({ record: true }, { sponsoredOnly: true }),
    on_none:     P.cardPasses({ record: false }, { sponsoredOnly: true }),
    on_pending:  P.cardPasses({ record: null }, { sponsoredOnly: true }),
    on_unknown:  P.cardPasses({}, { sponsoredOnly: true }),
    off_none:    P.cardPasses({ record: false }, { sponsoredOnly: false }),
    off_default: P.cardPasses({ record: false }, {}),
    with_chips:  P.cardPasses({ record: true, codes: ['H-1B'], role_in_us: true }, { sponsoredOnly: true, chips: ['H-1B'] }),
  },
  summary: P.listSummary([{ record: true }, { record: false }, null, { record: false }, { record: null }],
                         { sponsoredOnly: true }),
  summary_off: P.listSummary([{ record: true }, { record: false }, null], { sponsoredOnly: false }),
};
console.log(JSON.stringify(out));
"""


def _node(script: str, name: str, *args: str) -> dict:
    h = Path("build") / name
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text(script, encoding="utf-8")
    r = subprocess.run(["node", str(h), *args], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip())


def test_a_record_means_h1b_or_green_card_or_e_verify_and_no_answer_is_not_a_no():
    out = _node(PURE, "_sponsored_only_pure.js", str(CORE))
    r = out["record"]
    assert r["h1b"] is True and r["perm"] is True and r["everify"] is True
    assert r["capOnly"] is False                      # cap-exempt alone is a guess, not a record
    assert r["none"] is False
    assert r["offline"] is None and r["missing"] is None   # no answer: never "no record"
    assert r["oldApp"] is True


def test_the_toggle_hides_only_a_confirmed_no_record_and_off_restores_all():
    out = _node(PURE, "_sponsored_only_pure.js", str(CORE))
    p = out["passes"]
    assert p["on_record"] is True and p["on_none"] is False
    assert p["on_pending"] is True and p["on_unknown"] is True   # still looking it up: visible
    assert p["off_none"] is True and p["off_default"] is True
    assert p["with_chips"] is True                               # composes with the chip filter
    assert out["summary"] == {"total": 5, "hidden": 2, "pending": 1}
    assert out["summary_off"] == {"total": 3, "hidden": 0, "pending": 1}


# A LinkedIn results column, the markup content.js reads by position (title / company / location
# lines in a flex column), with a tiny DOM: enough of querySelector, classList, innerHTML and
# events for content.js to badge the cards and paint the corner panel.
DOM = r"""
const fs = require('fs'), path = require('path');
const [extDir, initialToggle] = process.argv.slice(2);
globalThis.window = globalThis;
require(path.join(extDir, 'parity_core.js'));
require(path.join(extDir, 'adstance_core.js'));

const VOID = new Set(['img', 'input', 'br']);
class El {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = { ...attrs }; this.children = [];
    this.parentElement = null; this._text = ''; this.style = {}; this.dataset = {}; this.listeners = {};
  }
  get className() { return this.attrs.class || ''; } set className(v) { this.attrs.class = v; }
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
function parseInto(parent, html) {   // our own markup only: tags, attributes, text
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
    if (!VOID.has(tag.toLowerCase()) && !rest.endsWith('/')) stack.push(el);
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
    if (!matchCompound(n, comps[comps.length - 1])) return false;
    let a = n.parentElement;
    for (let i = comps.length - 2; i >= 0; i--) {
      while (a && !matchCompound(a, comps[i])) a = a.parentElement;
      if (!a) return false; a = a.parentElement;
    }
    return true;
  });
}
const el = (tag, attrs, kids = [], text) => { const n = new El(tag, attrs); if (text) n._text = text; kids.forEach(k => n.appendChild(k)); return n; };
const line = (t) => el('div', {}, [], t);
const card = (title, company) => el('div', {}, [el('div', { 'data-flex': 'column' }, [line(title), line(company), line('Dallas, TX · 2 days ago')])]);
const col = el('div', { componentkey: 'SearchResultsMainContent' }, [
  card('Data Engineer', 'Acme Sponsor'), card('Analyst', 'No Record Co'),
  card('Nurse', 'Everify Hospital'), card('Designer', 'Slow Corp'),
]);
const body = el('body', {}, [col]);
const document = {
  body, createElement: (t) => new El(t),
  querySelectorAll: (s) => body.querySelectorAll(s), querySelector: (s) => body.querySelector(s),
};
const LOOKUPS = {
  'Acme Sponsor': { ok: true, matched: true, matched_name: 'Acme Sponsor', role_in_us: true, visa: [{ code: 'H-1B' }], profile: { h1b_approvals: 40, perm_certs: 0, e_verify: false } },
  'No Record Co': { ok: true, matched: false, visa: [], profile: null },
  'Everify Hospital': { ok: true, matched: true, matched_name: 'Everify Hospital', role_in_us: true, visa: [{ code: 'STEM-OPT' }], profile: { h1b_approvals: 0, perm_certs: 0, e_verify: true } },
};
const store = { sj_sponsored_only: initialToggle === 'on' };
const storeWrites = [];
const chrome = {
  runtime: { id: 'test-ext', lastError: null,
    sendMessage: (m, cb) => {
      if (m.type === 'lookup') { if (m.company === 'Slow Corp') return; cb(LOOKUPS[m.company] || { ok: true, matched: false }); }
      else cb({ ok: false, error: 'cant_reach_app' });
    } },
  storage: { local: {
    get: (k, cb) => cb({ [k]: store[k] }),
    set: (o) => { Object.assign(store, o); storeWrites.push({ ...o }); },
  } },
};
const location = { hostname: 'www.linkedin.com', pathname: '/jobs/search-results/' };
const MutationObserver = function () { this.observe = () => {}; };
const getComputedStyle = (n) => ({ flexDirection: n.attrs['data-flex'] || 'row', display: 'block', gridTemplateColumns: '' });
const src = fs.readFileSync(path.join(extDir, 'content.js'), 'utf8');
new Function('document', 'chrome', 'location', 'MutationObserver', 'setTimeout', 'clearTimeout', 'getComputedStyle',
  src)(document, chrome, location, MutationObserver, setTimeout, clearTimeout, getComputedStyle);

const cards = () => col.children.map(c => ({ company: c.querySelectorAll('div')[2].innerText, display: c.style.display || '', badged: !!c.querySelector('.tailor-card-badges') }));
setTimeout(() => {            // let the async card scan finish
  const corner = body.querySelector('.tailor-corner');
  const note = corner.querySelector('.tailor-corner-note');
  const before = { cards: cards(), note: note.hidden ? null : note.textContent };
  corner.querySelector('.tailor-corner-btn').fire('click');          // open the panel
  const panel = corner.querySelector('.tailor-corner-panel');
  const box = panel.querySelector('.tailor-cp-sponsored');
  const opened = { checked: box.checked, count: panel.querySelector('.tailor-cp-count').textContent,
                   label: panel.querySelector('.tailor-cp-toggle').textContent.trim() };
  box.checked = !box.checked;                                       // the person flips it
  panel.fire('change', { target: box });
  const after = { cards: cards(), store: store.sj_sponsored_only, writes: storeWrites,
                  count: panel.querySelector('.tailor-cp-count').textContent,
                  note: corner.querySelector('.tailor-corner-note').hidden ? null : corner.querySelector('.tailor-corner-note').textContent };
  console.log(JSON.stringify({ before, opened, after, lookups: Object.keys(LOOKUPS) }));
}, 20);
"""


def _dom(initial: str) -> dict:
    return _node(DOM, f"_sponsored_only_dom_{initial}.js", str(EXT), initial)


def test_with_the_saved_toggle_on_only_no_record_cards_are_hidden_and_the_count_says_so():
    out = _dom("on")
    by = {c["company"]: c for c in out["before"]["cards"]}
    assert by["Acme Sponsor"]["display"] == "" and by["Acme Sponsor"]["badged"]
    assert by["Everify Hospital"]["display"] == ""            # E-Verify alone is a record
    assert by["No Record Co"]["display"] == "none"             # no H-1B, PERM or E-Verify
    assert by["Slow Corp"]["display"] == ""                    # lookup never answered: visible
    assert not by["Slow Corp"]["badged"]
    assert out["before"]["note"] == "Sponsored only: hiding 1 of 4"
    o = out["opened"]
    assert o["checked"] is True and o["count"] == "Hiding 1 of 4"
    assert o["label"].startswith("Sponsored only")


def test_flipping_the_toggle_off_restores_every_card_and_saves_the_choice():
    out = _dom("on")
    a = out["after"]
    assert all(c["display"] == "" for c in a["cards"]), a["cards"]
    assert a["store"] is False and a["writes"] == [{"sj_sponsored_only": False}]
    assert a["count"] == "" and a["note"] is None


def test_with_the_toggle_off_nothing_is_hidden_and_turning_it_on_hides_the_no_record_card():
    out = _dom("off")
    assert all(c["display"] == "" for c in out["before"]["cards"])
    assert out["before"]["note"] is None and out["opened"]["checked"] is False
    a = out["after"]
    by = {c["company"]: c for c in a["cards"]}
    assert by["No Record Co"]["display"] == "none"
    assert by["Acme Sponsor"]["display"] == "" and by["Slow Corp"]["display"] == ""
    assert a["store"] is True and a["count"] == "Hiding 1 of 4"


def test_the_toggle_is_styling_only_and_never_touches_the_site():
    """CLAUDE.md s7: the extension reads what is on screen and styles its own side of it. The
    list code must not click, scroll, or request anything from LinkedIn."""
    src = CONTENT.read_text(encoding="utf-8")
    lst = src[src.index("// ---- In-list:"):src.index("// ---- Our mark in the corner")]
    for forbidden in (".click()", "scrollIntoView", "scrollTo(", "scrollBy(", "fetch(", "XMLHttpRequest",
                      "dispatchEvent"):
        assert forbidden not in lst, forbidden
    assert 'style.display = P().cardPasses' in lst             # CSS on the card element, nothing else
    assert "sj_sponsored_only" in src and "chrome.storage.local" in src
