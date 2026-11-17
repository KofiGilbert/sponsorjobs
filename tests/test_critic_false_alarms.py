"""The pre-submit check never calls an attached file or a filled dropdown missing (2026-11-17, Upgrade
on Greenhouse). Its two false alarms: "No resume/CV upload field appears on the form, so no resume
appears to be attached" (Greenhouse shows the attached file's name where the upload box was) and
"Demographic questions (LGBTQ+, ethnicity, ...) are still empty" (react-select keeps its choice
beside its input, so the view read value=""). The shell now sends the run's own record: the
questions it filled (labels) and the kinds of file it attached (autoapply/brain.py critic)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import brain

ROOT = Path(__file__).resolve().parents[1]
VIEW = "\n".join([
    '[1]<input type=text label="First Name" value="(filled)" required>',
    '[2]<button>Kofi Gilbert Resume.pdf</button>',
    '[3]<combobox label="Do you identify as LGBTQ+?" value="" expanded=false>',
    '[4]<combobox label="Ethnicity" value="" expanded=false>',
    '[5]<combobox label="How did you hear about us?" value="" expanded=false required>',
    '[6]<button>Submit application</button>',
])
FALSE = ["No resume/CV upload field appears on the form, so no resume appears to be attached",
         "Demographic questions (LGBTQ+, ethnicity, ...) are still empty",
         "No cover letter is attached"]
REAL = "How did you hear about us? is required and still empty"


class Critic:
    def __init__(self, issues):
        self.issues, self.users = issues, []

    def agent_critic(self, system, user):
        self.users.append(user)
        return json.dumps({"ok": False, "issues": self.issues})


def test_attached_files_and_filled_dropdowns_are_not_reported_missing():
    llm = Critic(FALSE + [REAL])
    out = brain.critic({"view": VIEW, "filled": ["Do you identify as LGBTQ+?", "Ethnicity"],
                        "files": ["resume", "cover_letter"]}, llm)
    assert out["issues"] == [REAL] and out["ok"] is False
    sent = llm.users[0]
    assert "<filled>\nDo you identify as LGBTQ+?\nEthnicity\n</filled>" in sent
    assert "<attached>\nthe cover letter\nthe resume\n</attached>" in sent
    # the view reads the filled dropdowns as filled (their choice hidden as always), the open one stays open
    assert '<combobox label="Ethnicity" value="(filled)"' in sent
    assert '<combobox label="How did you hear about us?" value=""' in sent


def test_only_alarms_the_record_contradicts_are_dropped():
    llm = Critic(FALSE)
    out = brain.critic({"view": VIEW, "filled": [], "files": []}, llm)
    assert out["issues"] == FALSE                            # nothing attached or filled: they stand
    out = brain.critic({"view": VIEW, "filled": ["Do you identify as LGBTQ+?", "Ethnicity"], "files": ["resume"]},
                       Critic(FALSE))
    assert out["issues"] == ["No cover letter is attached"]  # only the resume went in
    out = brain.critic({"view": VIEW, "filled": [], "files": ["resume", "cover_letter"]}, Critic(FALSE))
    assert out["issues"] == [FALSE[1]]                       # an empty self-identification box: true


def test_all_false_alarms_dropped_means_ok():
    out = brain.critic({"view": VIEW, "filled": ["Do you identify as LGBTQ+?", "Ethnicity"],
                        "files": ["resume", "cover_letter"]}, Critic(FALSE))
    assert out["issues"] == [] and out["ok"] is True


def test_the_shell_sends_labels_and_file_kinds_never_values_or_names():
    if not shutil.which("node"):
        pytest.skip("needs node")
    js = ("const A = require(%s); const a = A.createAnswers();"
          "A.addAnswer(a, {page: 1, label: 'Ethnicity', value: 'Zyxwv', from: 'eeo'});"
          "A.addAnswer(a, {page: 1, label: 'First Name', value: 'Kofi', from: 'profile'});"
          "A.addFile(a, {page: 1, kind: 'resume', path: '/w/uploads/3/Kofi Gilbert Resume.pdf', how: 'attached'});"
          "A.addFile(a, {page: 1, kind: 'cover_letter', path: '/w/uploads/3/cover/Kofi Cover Letter.pdf', how: 'attached'});"
          "console.log(JSON.stringify(A.criticInputs(a)));" % json.dumps(str(ROOT / "shell-electron" / "agent.js")))
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == {"filled": ["Ethnicity", "First Name"], "files": ["resume", "cover_letter"]}
    src = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    assert "filled: critIn.filled, files: critIn.files" in src
