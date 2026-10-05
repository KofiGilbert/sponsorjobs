"""P5: the assisted-apply FILL ENGINE (extension/autofill_core.js) reports "filled X of Y" and
ABORTS cleanly on a captcha / auth wall (drops to the person, never evades, CLAUDE.md §7).

Runs the real autofill_core.js in a minimal fake DOM under node (skipped without node), the same
approach tests/test_extension_content.py uses for content.js.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

CORE = Path("extension/autofill_core.js")

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

// Fake element prototypes so autofill_core's native value setter works on our objects.
global.HTMLInputElement = function () {};
global.HTMLTextAreaElement = function () {};
global.HTMLSelectElement = function () {};
for (const P of [HTMLInputElement, HTMLTextAreaElement, HTMLSelectElement]) {
  Object.defineProperty(P.prototype, 'value', {
    configurable: true, get() { return this._value || ''; }, set(v) { this._value = v; },
  });
}
function mkInput(opts) {
  const el = Object.create(HTMLInputElement.prototype);
  Object.assign(el, {
    tagName: 'INPUT', type: opts.type || 'text', name: opts.name || '', id: opts.id || '',
    placeholder: opts.placeholder || '', disabled: false, readOnly: false, _value: opts.value || '',
    _attrs: opts.attrs || {},
    getAttribute(a) { return this._attrs[a] || null; },
    getBoundingClientRect() { return opts.rect || { width: 20, height: 20 }; },
    closest() { return null; },
    classList: { add() {}, remove() {} },
    dispatchEvent() { return true; },
    options: [],
  });
  return el;
}

// Scenarios -----------------------------------------------------------------
let inputs = [], iframes = [], passwordEl = null, bodyText = '';
if (scenario === 'form') {
  inputs = [
    mkInput({ type: 'email', placeholder: 'Email address' }),
    mkInput({ name: 'first_name', placeholder: 'First name' }),
    mkInput({ type: 'tel', placeholder: 'Phone number' }),
    mkInput({ name: 'city', placeholder: 'City', value: 'Chicago' }),   // already typed -> counts to Y not X
  ];
} else if (scenario === 'honeypot') {
  // A second "email" input parked off the canvas: the anti-spam trap a bot fills and a
  // person never sees. Only the visible one may be filled.
  inputs = [
    mkInput({ type: 'email', placeholder: 'Email address' }),
    mkInput({ type: 'email', name: 'email_confirm_hp', placeholder: 'Email',
              rect: { width: 20, height: 20, right: -500, bottom: -500 } }),
  ];
} else if (scenario === 'captcha') {
  iframes = [{ getAttribute: () => 'https://www.google.com/recaptcha/api2/anchor' }];
  inputs = [mkInput({ type: 'email', placeholder: 'Email' })];
} else if (scenario === 'auth') {
  passwordEl = mkInput({ type: 'password', placeholder: 'Password' });
  bodyText = 'Please sign in to apply for this role.';
  inputs = [mkInput({ type: 'email', placeholder: 'Email' })];
}

const document = {
  querySelectorAll(sel) {
    if (sel === 'iframe') return iframes;
    if (sel.includes('radio') || sel === 'select' || sel.includes('file')) return [];
    if (sel.includes('input') && sel.includes('textarea')) return inputs;
    return [];
  },
  querySelector(sel) {
    if (sel === 'input[type="password"]') return passwordEl;
    return null;
  },
  getElementById() { return null; },
  body: { textContent: bodyText },
};
const window = {};
const location = { hostname: 'boards.greenhouse.io' };
const CSS = { escape: (s) => s };
global.Event = function () {};

new Function('window', 'document', 'location', 'CSS', 'Event', 'setTimeout', src)(
  window, document, location, CSS, Event, (f) => {});

const payload = { fields: { email: 'me@x.com', first_name: 'Sam', phone: '555-1212', city: 'NYC' } };
console.log(JSON.stringify(window.TailorAutofill.fill(payload)));
"""


def _run(scenario):
    harness = Path("build/_autofill_harness.js")
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(["node", str(harness), str(CORE), scenario],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def test_fill_reports_filled_of_recognized_total():
    r = _run("form")
    assert r["ok"] is True
    # 4 recognized profile fields (email, first, phone, city); city was already typed so 3 filled.
    assert r["filled"] == 3 and r["total"] == 4, r


def test_fill_proceeds_on_a_captcha_but_reports_it():
    # A captcha gates the SUBMIT click, not the form. The fill still happens (that is the
    # help), and the captcha is reported so the caller hands the submit back to the person
    # rather than clicking it. Nothing here touches, solves or bypasses the challenge.
    r = _run("captcha")
    assert r["ok"] is True and r["captcha"] is True
    assert "filled" in r


def test_fill_aborts_on_an_auth_wall():
    r = _run("auth")
    assert r["ok"] is False and r["blocker"] == "auth"


def test_off_canvas_honeypot_inputs_are_never_filled():
    r = _run("honeypot")
    assert r["ok"] is True
    # Two email inputs on the page, one parked off-screen: exactly one recognised, one filled.
    assert r["filled"] == 1 and r["total"] == 1, r
