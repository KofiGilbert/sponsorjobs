"""Optional self-identification questions are filled, never left on "Select..." (2026-11-17, Upgrade
on Greenhouse: "What is your veteran status?" and "What is your disability status?" stayed empty
although both answers were saved). The rule: a self-identification question, required or
optional, gets the saved answer when a choice says it, else the page's decline choice.

The engine side (the ask route's known_only check, autoapply/asks.known_answer) already picks the
right choice; the shell only sent it the REQUIRED open questions (shell-electron/agent.js
fillSavedEeo). It now adds every empty self-identification control in the view (emptySelfId).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import asks
from autoapply.answers import normalize
from autoapply.fields import FormField
from autoapply.planner import KnownContext, plan_known

ROOT = Path(__file__).resolve().parents[1]
VET_Q, DIS_Q = "What is your veteran status?", "What is your disability status?"
VET = ["I am a protected veteran", "I am not a protected veteran", "I prefer not to say"]
DIS = ["Yes, I have a disability, or have had one in the past",
       "No, I do not have a disability and have not had one in the past", "I prefer not to say"]


class Lib:
    def __init__(self, d):
        self.d = d

    def lookup(self, q):
        k = normalize(q)
        return {"key": k, "answer": self.d[k]} if k in self.d else None

    def best(self, k):
        return {"key": k, "answer": self.d[k]} if k in self.d else None


SAVED = {"@veteran": "I am not a protected veteran", "@disability": "No"}


def test_the_saved_answers_pick_the_greenhouse_choices():
    for lib, want in ((Lib(SAVED), (VET[1], DIS[1])), (Lib({}), (VET[2], DIS[2]))):
        got = (asks.known_answer(VET_Q, VET, library=lib, eeo_fallback=True),
               asks.known_answer(DIS_Q, DIS, library=lib, eeo_fallback=True))
        assert tuple(g[0] for g in got) == want


def test_the_planner_fills_them_optional_or_not():
    for req in (False, True):
        fields = [FormField("v", VET_Q, "select", VET, required=req), FormField("d", DIS_Q, "select", DIS, required=req)]
        res = plan_known(fields, KnownContext(library=Lib(SAVED)))
        assert {a.ref: a.value for a in res.actions} == {"v": VET[1], "d": DIS[1]} and not res.left


def test_the_shell_sends_optional_empty_self_identification_questions_too():
    if not shutil.which("node"):
        pytest.skip("needs node")
    view = "\n".join([
        '[3]<combobox label="Gender" value="(filled)" expanded=false>',
        '[4]<combobox label="What is your veteran status?" value="" expanded=false>',
        '[5]<combobox label="What is your disability status?" value="" expanded=false>',
        '[6]<combobox label="How did you hear about us?" value="" expanded=false>',
        '[7]<radio label="Are you Hispanic/Latino?" option="Yes" checked=hidden answered=false>',
        '[8]<radio label="Are you Hispanic/Latino?" option="No" checked=hidden answered=false>',
        '[9]<radio label="Do you identify as LGBTQ+?" option="Yes" checked=hidden answered=true>',
        '[10]<select label="Race" value="(filled)" options=8 first="Asian">',
    ])
    js = ("const A = require(%s); console.log(JSON.stringify(A.emptySelfId(%s)));"
          % (json.dumps(str(ROOT / "shell-electron" / "agent.js")), json.dumps(view)))
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [VET_Q, DIS_Q, "Are you Hispanic/Latino?"]
    src = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    body = src[src.index("async function fillSavedEeo("):]
    assert "emptySelfId(v.text)" in body[:body.index("\n  }\n")]
