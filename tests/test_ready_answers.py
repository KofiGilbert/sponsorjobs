"""Review answers at Ready to send (2026-11-16, Uline on Workday: the owner pressed Send without
seeing that the agent had picked "Agricultural Economics" for Field of Study, "JavaScript" for
Skills, and attached a resume tailored for another Uline role).

* the fill engine logs what it set and where each value came from (tests/js/test_fill_log.js);
* the shell assembles every field the run filled, page by page, with its origin, flags the lines
  that deserve a look, and keeps self-identification values out of the feed, the debug log and
  the Telegram text (tests/js/test_ready_answers.js);
* the app shows it on demand next to Send, with every string translated.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _node(name):
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / name)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr


def test_the_fill_engine_logs_each_value_with_its_origin():
    _node("test_fill_log.js")


def test_the_shell_lists_flags_and_keeps_self_identification_local():
    _node("test_ready_answers.js")


def test_labels_keep_their_words_and_a_ticked_box_names_itself():
    """Gusto (Greenhouse), 2026-11-16: "(please select one)" read "(please )?", and a ticked
    "How did you hear" box read "Checked" instead of "Indeed"."""
    _node("test_answer_labels.js")


def test_the_bar_offers_review_answers_next_to_send():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="bbAnswers"' in html
    ready = js[js.index('} else if (info.state === "ready" && info.record_id != null) {'):]
    ready = ready[:ready.index('} else if (info.state === "parked"')]
    # after Send, before Read cover letter; the list comes from the Ready to send line
    assert ready.index('T("aa.send", "Send")') < ready.index("aaAnsToggle(") < ready.index("aa.agent.readLetter")
    assert "info.answers" in ready
    # the Ready line itself is one short line; the shell's whole message is under Details
    # (2026-11-26, tests/test_guided_fill.py runs aaBarShort)
    assert "text.textContent = short.line" in js and "short.details" in js


def test_every_review_answers_string_is_translated():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'T\("(aa\.ans\.[A-Za-z0-9_.]+)"', js))
    assert len(keys) >= 25, keys
    for code in ("en", "de", "es", "fr", "pt"):
        d = json.loads((ROOT / "ui" / "static" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        missing = keys - set(d)
        assert not missing, f"{code}.json misses {sorted(missing)}"
    en = json.loads((ROOT / "ui" / "static" / "locales" / "en.json").read_text(encoding="utf-8"))
    for k in keys:
        assert not re.search("[–—]", en[k]) and not re.search(r"\bCV\b", en[k]), k
