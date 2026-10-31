""""What changed" is built in code (tailoring/changes.py), never by the model: a deterministic
diff of the tailored page against the base resume, used by the builder and by Telegram's
caption. Also: the refine chips come from the coverage report with no model call."""

from __future__ import annotations

import copy
from pathlib import Path

from intake.memory import ConversationMemory
from llm.base import FakeLLM
from notify import cvdoc
from tailoring.changes import points, reworded_bullets, skills_change, summary_line, what_changed
from ui.records import CVRecords
from ui.session import WebIntake

ROOT = Path(__file__).resolve().parents[1]

BASE = {
    "summary": "Data engineer.",
    "skills": {"Data": "SQL, Python, Airflow", "Cloud": "AWS, Docker"},
    "experience": [{"org": "Acme Bank", "roles": [{"title": "Data Engineer", "bullets": [
        "Built ETL pipelines in Python for the risk team.",
        "Ran the nightly reporting jobs.",
        "Mentored two analysts."]}]}],
}
PAGE = {
    "summary": "Data engineer.",
    "skills": {"Data": "Python, Airflow, SQL", "Cloud": "AWS, Docker"},
    "experience": [{"org": "Acme Bank", "roles": [{"title": "Data Engineer", "bullets": [
        "Python, ETL: Built ETL pipelines in Python for the risk team.",
        "Ran the nightly reporting jobs.",
        "Mentored two analysts."]}]}],
}


def test_bullets_pair_by_role_and_count_only_real_differences():
    rew = reworded_bullets(BASE, PAGE)
    assert rew == [{"label": "Acme Bank, Data Engineer",
                    "before": "Built ETL pipelines in Python for the risk team.",
                    "after": "Python, ETL: Built ETL pipelines in Python for the risk team."}]


def test_a_retitled_role_still_pairs_by_employer():
    page = copy.deepcopy(PAGE)
    page["experience"][0]["roles"][0]["title"] = "Senior Data Engineer"   # title aligned to the JD
    assert len(reworded_bullets(BASE, page)) == 1


def test_skills_reordered_is_detected_and_nothing_else():
    sk = skills_change(BASE, PAGE)
    assert sk == {"reordered": ["Data"], "added": [], "lines_moved": False}


def test_the_whole_diff_and_its_words_are_deterministic():
    trims = [{"id": "t1", "kind": "bullet", "label": "Acme Bank, Data Engineer",
              "text": "Mentored two analysts."}]
    reverted = [{"label": "Acme Bank, Data Engineer", "claims": ["AWS Certified"]}]
    cov = {"present": ["Python", "PnL", "SQL"], "missing": ["Kafka"]}
    ch = what_changed(BASE, PAGE, coverage=cov, hidden=["Viva, Analyst"], trims=trims,
                      reverted=reverted, keyword_terms=["Airflow"])
    assert ch == what_changed(BASE, PAGE, coverage=cov, hidden=["Viva, Analyst"], trims=trims,
                              reverted=reverted, keyword_terms=["Airflow"])
    assert ch["terms_added"] == ["PnL", "Airflow"]
    assert ch["trimmed"][0]["text"] == "Mentored two analysts."
    pts = points(ch)
    assert pts[0] == "1 bullet reworded toward the job."
    assert "Skills reordered to lead with what the job asks for." in pts
    assert "Hidden by your choice: Viva, Analyst." in pts
    assert any("cut to fit one page" in p for p in pts)
    assert any("kept in your own words" in p for p in pts)
    line = summary_line(ch, "Data Engineer", "Kraken")
    assert line == ("Tailored for Data Engineer at Kraken: 1 bullet reworded, skills reordered, "
                    "Viva, Analyst hidden by your choice, 1 item cut to fit. Added the job's "
                    "terms: PnL, Airflow. Missing: Kafka.")
    for text in pts + [line]:
        assert "—" not in text and "–" not in text and " CV" not in text


def test_telegram_caption_uses_the_coded_diff_and_the_saved_record_parts():
    cov = {"present": ["Python", "ETL"], "missing": []}
    saved = {"hidden": ["Viva, Analyst"], "trimmed": [{"id": "t1", "text": "Mentored two analysts."}]}
    cap = cvdoc.change_summary("Data Engineer", "Kraken", BASE, PAGE, cov, changes=saved)
    assert cap.startswith("Tailored for Data Engineer at Kraken: 1 bullet reworded, skills reordered, "
                          "Viva, Analyst hidden by your choice, 1 item cut to fit.")


def test_no_model_writes_the_change_summary():
    src = (ROOT / "tailoring" / "changes.py").read_text(encoding="utf-8")
    assert "llm" not in src.lower().replace("no model", "")


def _session(tmp_path):
    return WebIntake("Data Engineer. Python, SQL, Kubernetes, Airflow.", "x", FakeLLM(),
                     ConversationMemory(":memory:"), CVRecords(":memory:"), tmp_path,
                     jobname="cv", palace_dir=tmp_path / "p")


def test_refine_chips_come_from_supported_gaps_without_a_model_call(tmp_path):
    s = _session(tmp_path)

    class NoCalls(FakeLLM):
        def __getattribute__(self, name):
            if name in ("keyword_action", "reword_bullet", "extract_jd_skills"):
                raise AssertionError("a refine chip must not call the model")
            return object.__getattribute__(self, name)
    s.llm = NoCalls()
    cov = {"present": ["Python"], "missing_supported": ["Kubernetes", "Airflow", "dbt", "Spark", "Kafka"],
           "missing": ["Go"]}
    chips = s.refine_chips(cov)
    assert [c["label"] for c in chips] == ["Work in Kubernetes", "Work in Airflow", "Work in dbt",
                                           "Work in Spark"]
    s.keyword_actions = [{"term": "Kubernetes", "kind": "rewrite", "label": "Acme"}]
    assert "Kubernetes" not in [c["term"] for c in s.refine_chips(cov)]


def test_the_builder_renders_chips_changes_and_put_back():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="tailorWhat"' in html and 'id="twToggle"' in html
    assert "function renderTailorWhat(st)" in js
    assert 'kwPreview(chip.dataset.term, null)' in js            # the chip runs Add this keyword
    assert '"/api/cv/live/trim/restore"' in js
