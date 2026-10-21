"""Memory: the feed list is streamed and compacted, caches are bounded (2026-10-21).

The desktop app holds the whole ~150k-row slim list in memory. These pin the compact loader's
contract: the rows come back EQUAL to a plain json.loads of the same file (same keys, values,
order), repeated values are one shared object, the parser never trusts a value cut off at the end
of its read window, and a malformed file is refused rather than half-loaded."""

from __future__ import annotations

import gzip
import io
import json

import pytest

from sourcing import feedfile
from sourcing.feedfile import compact_rows, load_feed_file, load_feed_stream


def _rows(n=50):
    sponsor = {"cap_exempt": False, "h1b_approvals": 12, "fy_range": "FY2021 to FY2025"}
    nat = [{"code": "E-3", "basis": "Australian nationals é"}, {"code": "TN", "basis": "x"}]
    out = []
    for i in range(n):
        out.append({"source_id": f"greenhouse:acme:{1000 + i}", "source": "greenhouse",
                    "company": "Acme" if i % 2 else "Globex — Labs", "title": f"Engineer {i}",
                    "location": "New York, NY", "remote": "", "url": f"https://x/{i}",
                    "posted_at": "2026-10-20", "salary": "$120k/yr" if i % 3 else "",
                    "sponsorship_stated": None, "us": True, "entry_level": i % 2 == 0,
                    "visa": [{"code": "H1B", "count": 12}] if i % 2 else [],
                    "nationality_visas": nat, "sponsor": dict(sponsor), "tags": ["Tech"],
                    "count_big": 12345678901234 + i, "ratio": 1.5e-3})
    return out


def _doc(rows):
    return {"feed_version": 1, "generated_at": "2026-10-21T00:00:00Z", "count": len(rows),
            "attribution": {"remotive": {"name": "Remotive"}}, "jobs": rows}


class _Trickle(io.StringIO):
    """A text stream that hands out a few characters per read: every value straddles a window."""

    def __init__(self, text, step):
        super().__init__(text)
        self.step = step

    def read(self, n=-1):
        return super().read(self.step)


@pytest.mark.parametrize("step", [1, 3, 7, 64, 1 << 20])
def test_stream_equals_json_loads_at_any_window(step, monkeypatch):
    doc = _doc(_rows())
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    monkeypatch.setattr(feedfile, "_CHUNK", step)
    assert load_feed_stream(_Trickle(text, step)) == json.loads(text)


def test_compact_shares_repeated_values():
    rows = compact_rows(_rows())
    assert rows == _rows()
    assert all(r["sponsor"] is rows[0]["sponsor"] for r in rows)
    assert all(r["nationality_visas"] is rows[0]["nationality_visas"] for r in rows)
    assert rows[1]["company"] is rows[3]["company"]
    assert list(rows[0])[0] is list(rows[1])[0]          # key names shared too


def test_load_feed_file_gzip_and_plain(tmp_path):
    doc = _doc(_rows(10))
    gz = tmp_path / "jobs.json.gz"
    gz.write_bytes(gzip.compress(json.dumps(doc).encode()))
    plain = tmp_path / "jobs.json"
    plain.write_text(json.dumps(doc), encoding="utf-8")
    assert load_feed_file(gz) == doc == load_feed_file(plain)


def test_empty_and_jobs_first(tmp_path):
    assert load_feed_stream(io.StringIO('{"jobs": []}')) == {"jobs": []}
    assert load_feed_stream(io.StringIO("{}")) == {}
    assert load_feed_stream(io.StringIO('{"jobs":[{"a":1}],"count":1}')) == {"jobs": [{"a": 1}],
                                                                              "count": 1}


@pytest.mark.parametrize("bad", ['{"jobs": [{"a": 1}', '{"jobs": [{"a": 1},]}', "[1, 2]",
                                 '{"count": 12', '{"jobs": [{"a": 1}] "x": 1}', ""])
def test_malformed_is_refused(bad):
    with pytest.raises(ValueError):
        load_feed_stream(io.StringIO(bad))


def test_write_feed_gz_round_trips(tmp_path):
    from sourcing.feedclient import _write_feed_gz
    doc = _doc(_rows(30))
    tmp = _write_feed_gz(tmp_path, doc)
    assert json.loads(gzip.decompress(open(tmp, "rb").read())) == doc


def test_shape_copies_only_flagged_rows():
    from autoapply.learning import empty_state, shape
    rows = [{"source_id": "a", "company": "Acme", "title": "Engineer"},
            {"source_id": "b", "company": "Globex", "title": "Engineer"}]
    st = empty_state()
    st["prioritize"] = [{"name": "Globex"}]
    out = shape(rows, st, needs_sponsor=False)
    assert out[0]["source_id"] == "b" and out[0]["priority"] is True
    assert out[0] is not rows[1] and "priority" not in rows[1]   # flagged: a copy
    assert out[1] is rows[0]                                      # unflagged: the row itself


def test_logo_cache_is_bounded(monkeypatch):
    import ui.app as appmod
    monkeypatch.setattr(appmod, "_fetch_logo", lambda d: (b"x" * 2000, "image/webp"))
    monkeypatch.setattr(appmod, "_LOGO_CACHE_MAX", 5)
    appmod._LOGO_CACHE.clear()
    client = appmod.app.test_client()
    for i in range(12):
        assert client.get(f"/api/logo?company=Company{i}").status_code == 200
    assert len(appmod._LOGO_CACHE) == 5
    appmod._LOGO_CACHE.clear()


def test_shell_tears_down_a_closed_pane():
    """A closed in-app browser pane must not keep its job site's renderer alive all session."""
    import re
    from pathlib import Path
    main = (Path(__file__).resolve().parents[1] / "shell-electron" / "main.js").read_text("utf-8")
    close = re.search(r"function closePane\(\) \{(.*?)\n\}", main, re.S).group(1)
    assert "setTimeout(destroyPane" in close
    destroy = re.search(r"function destroyPane\(\) \{(.*?)\n\}", main, re.S).group(1)
    assert "if (!pane || paneOpen) return;" in destroy
    assert "removeChildView" in destroy and "webContents.close()" in destroy
    assert "disarmSuccessWatch()" in destroy and "disarmPageWatch()" in destroy
    opener = re.search(r"\n  clearTimeout\(paneReap\);\n  paneReap = null;\n  ensurePane\(\);", main)
    assert opener, "opening the pane must cancel a pending teardown"


def test_shell_turns_off_the_macos_large_block_cache():
    from pathlib import Path
    main = (Path(__file__).resolve().parents[1] / "shell-electron" / "main.js").read_text("utf-8")
    spawn = main[main.index("async function ensureServer"):main.index("function paneState")]
    assert 'env.MallocLargeCache = "0"' in spawn
