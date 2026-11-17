"""Never skip the right resume (2026-11-16, Uline on Workday).

PR #282 stopped the agent uploading a file the form already lists, matched by name (and size when
shown). Every tailored resume leaves the app as "<Name> Resume.pdf" (tests/test_outgoing_filenames.py
keeps that name on purpose), so a Workday draft that kept an EARLIER application's resume looked
like "already there" and the resume tailored for this job was never attached. Now
(shell-electron/uploads.js, extension/autofill_core.js fileListing):

* same name, same size (within the rounding the page shows) -> this file, not uploaded again;
* same name, another size -> uploaded, the earlier one left, flagged in Review answers;
* same name, no size shown -> uploaded, the earlier one left and flagged, unless this run itself
  put that very file there.

The checks run under node with a small fake page (no browser): tests/js/test_upload_identity.js.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_upload_identity_rule_listing_and_flag():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_upload_identity.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr


def test_the_employer_facing_name_is_unchanged():
    """The fix relies on size, not a per-application file name: the outgoing name stays."""
    from submit.filenames import export_filename
    assert export_filename("Avery Example") == "Avery Example Resume.pdf"


def test_the_earlier_file_flag_is_translated():
    import json
    for code in ("en", "de", "es", "fr", "pt"):
        d = json.loads((ROOT / "ui" / "static" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        assert d.get("aa.ans.flag.earlier"), code
        assert "–" not in d["aa.ans.flag.earlier"] and "—" not in d["aa.ans.flag.earlier"]
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'earlier_file: T("aa.ans.flag.earlier"' in js
