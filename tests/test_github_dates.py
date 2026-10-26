"""Project dates from the person's own GitHub repos (ui/github_dates.py), and the honest line a
build adds when a project the template dates is still undated. Offline: FETCH is a fake."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import ui.app as app
from ui import github_dates as gd

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


class FakeGitHub:
    def __init__(self, repos, status=None):
        self.repos, self.status, self.calls = repos, status, []

    def __call__(self, method, url, token="", body=None, params=None):
        self.calls.append((url, token))
        if self.status is not None:
            return self.status, None
        key = url.rsplit("/repos/", 1)[1].lower()
        return (200, self.repos[key]) if key in self.repos else (404, {"message": "Not Found"})


@pytest.fixture()
def gh(monkeypatch):
    monkeypatch.setenv("GITHUB_DATES_FETCH", "1")
    fake = FakeGitHub({
        "sam/gate-check": {"created_at": "2024-01-10T00:00:00Z", "pushed_at": "2024-03-02T00:00:00Z"},
        "sam/ledger": {"created_at": "2025-06-01T00:00:00Z", "pushed_at": "2026-10-01T00:00:00Z"},
    })
    monkeypatch.setattr(gd, "FETCH", fake)
    return fake


def test_repo_link_or_profile_plus_name():
    assert gd.repo_for({"link": "https://github.com/sam/gate-check"}) == ("sam", "gate-check")
    assert gd.repo_for({"url": "github.com/sam/ledger.git"}) == ("sam", "ledger")
    assert gd.repo_for({"title": "Gate Check"}, "https://github.com/sam") == ("sam", "gate-check")
    assert gd.repo_for({"title": "Gate Check"}, "") is None          # no source, no guess


def test_fills_dates_from_the_repo_and_caches_per_repo(gh, tmp_path):
    projects = [{"title": "Gate Check"}, {"title": "Ledger"}, {"title": "Secret Sauce"},
                {"title": "Dated", "dates": "2022"}]
    filled = gd.fill_project_dates(projects, "https://github.com/sam", tmp_path / "c.json", now=NOW)
    assert filled == ["Gate Check", "Ledger"]
    assert projects[0]["dates"] == "Jan 2024 - Mar 2024"
    assert projects[1]["dates"] == "Jun 2025 - Present"             # pushed within 60 days
    assert "dates" not in projects[2]                                # 404: never invented
    assert projects[3]["dates"] == "2022"                            # a dated project is left alone
    assert len(gh.calls) == 3
    again = [{"title": "Gate Check"}, {"title": "Secret Sauce"}]
    gd.fill_project_dates(again, "https://github.com/sam", tmp_path / "c.json", now=NOW)
    assert len(gh.calls) == 3 and again[0]["dates"] == "Jan 2024 - Mar 2024"   # from the cache


def test_rate_limit_pauses_lookups_and_offline_fills_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_DATES_FETCH", "1")
    limited = FakeGitHub({}, status=403)
    monkeypatch.setattr(gd, "FETCH", limited)
    p = [{"title": "A"}, {"title": "B"}]
    assert gd.fill_project_dates(p, "https://github.com/sam", tmp_path / "c.json", now=NOW) == []
    assert len(limited.calls) == 1                                   # blocked after the first 403
    offline = FakeGitHub({}, status=0)
    monkeypatch.setattr(gd, "FETCH", offline)
    assert gd.fill_project_dates([{"title": "C"}], "https://github.com/sam",
                                 tmp_path / "d.json", now=NOW) == []
    assert not (tmp_path / "d.json").exists() or "sam/c" not in (tmp_path / "d.json").read_text()


def test_the_token_is_used_when_saved(gh, tmp_path):
    gd.fill_project_dates([{"link": "https://github.com/sam/ledger"}], "", tmp_path / "c.json",
                          token="ghp_x", now=NOW)
    assert gh.calls[0][1] == "ghp_x"


def test_import_fills_from_github_before_asking(gh, tmp_path, monkeypatch):
    from docx import Document
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "UPLOADS_DIR", tmp_path / "up")
    monkeypatch.setattr(app, "_DATA", tmp_path)
    monkeypatch.setattr(app, "_cred", lambda k: "")
    prof = {"identity": {"name": "Sam Rivera", "email": "s@example.com", "github": "https://github.com/sam"},
            "experience": [{"org": "Acme", "location": "Remote",
                            "roles": [{"title": "Engineer", "dates": "2020 - 2024", "bullets": ["x"]}]}],
            "projects": [{"title": "Gate Check", "bullets": ["x"]}, {"title": "Notes App", "bullets": ["y"]}]}

    class Stub:
        def extract_profile(self, text):
            return {k: (dict(v) if isinstance(v, dict) else [dict(x) for x in v]) for k, v in prof.items()}
    monkeypatch.setattr(app, "_make_llm", lambda: Stub())
    doc = Document()
    doc.add_paragraph("Sam Rivera")
    doc.save(str(tmp_path / "r.docx"))
    with open(tmp_path / "r.docx", "rb") as f:
        d = app.app.test_client().post("/api/profile/from_cv", data={"file": (f, "r.docx")},
                                       content_type="multipart/form-data").get_json()
    saved = {p["title"]: p for p in app._memory().load("default")["profile"]["projects"]}
    assert saved["Gate Check"]["dates"].startswith("Jan 2024")       # filled from the repo
    assert [s["q"] for s in d["missing"]] == ['the dates for your project "Notes App"']   # only the rest


# --- the build's honest line -----------------------------------------------------------------

def _session(projects, sections):
    from types import SimpleNamespace

    from ui.session import WebIntake
    s = WebIntake.__new__(WebIntake)
    s.sections = sections
    s.profile = {}
    s.assembled = SimpleNamespace(profile_used={"projects": projects})
    return s


def test_the_build_says_when_a_project_reads_as_undated():
    s = _session([{"title": "Gate Check"}, {"title": "Ledger", "dates": "2023"}], ["experience", "projects"])
    assert s.undated_projects_note() == ("Without dates, Gate Check reads as undated; "
                                         "recruiters may weigh it less.")
    s = _session([{"title": "A"}, {"title": "B"}], ["projects"])
    assert s.undated_projects_note() == "Without dates, A and B read as undated; recruiters may weigh them less."
    assert _session([{"title": "A"}], ["experience"]).undated_projects_note() == ""   # not shown
    assert _session([{"title": "A", "dates": "2024"}], ["projects"]).undated_projects_note() == ""


def test_build_from_saved_adds_the_line():
    import inspect

    from ui.session import WebIntake
    src = inspect.getsource(WebIntake.build_from_saved)
    assert "undated = self.undated_projects_note()" in src and "msgs.append(undated)" in src
