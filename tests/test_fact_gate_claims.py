"""The wider fact gate (tailoring/fact_gate.py): a tailored bullet that names an employer, a
school, a tool, an acronym or a credential the person's own material never states is reverted
to the person's wording, never published. Mirroring a JD tool the person's material does name
passes. Realistic cases, the FakeLLM test double only."""

from __future__ import annotations

from llm.base import FakeLLM
from tailoring.assembler import _tailor_bullets
from tailoring.fact_gate import claims, describe, gate_rewrite, new_claims
from tailoring.keyword_action import validate_draft

SOURCE = "Built ETL pipelines in Python on AWS for the credit risk team."
PROFILE = {
    "identity": {"name": "Ama Owusu"},
    "education": [{"school": "Ohio State University",
                   "degree": "Bachelor of Science in Computer Science"}],
    "skills": {"Cloud": "AWS, Docker, Kubernetes", "Data": "Python, SQL, Airflow"},
    "experience": [{"org": "Acme Bank", "roles": [{"title": "Data Engineer", "bullets": [
        SOURCE, "Deployed reporting services with Kubernetes and Airflow."]}]}],
}
JD = "Senior Data Engineer. AWS Certified preferred. Kubernetes, Airflow, Snowflake, dbt."


def test_an_invented_aws_certification_is_caught():
    # The profile has AWS as a skill, and no certification of any kind.
    bad = gate_rewrite(SOURCE, "AWS Certified engineer who built ETL pipelines in Python.", PROFILE)
    assert bad == ["AWS Certified"]


def test_a_certification_the_person_holds_passes():
    held = dict(PROFILE, certifications=["AWS Certified Solutions Architect"])
    assert gate_rewrite(SOURCE, "AWS Certified engineer who built ETL pipelines in Python.", held) == []


def test_mirroring_a_jd_tool_the_profile_has_passes():
    # Kubernetes is in the JD and in the person's skills line: mirroring it is the point.
    assert gate_rewrite(SOURCE, "Built ETL pipelines in Python on AWS and Kubernetes for the "
                                "credit risk team.", PROFILE) == []


def test_a_jd_tool_the_profile_lacks_is_caught():
    assert gate_rewrite(SOURCE, "Built ETL pipelines in Python and dbt on Snowflake for the "
                                "credit risk team.", PROFILE) == ["Snowflake"]


def test_an_invented_employer_school_or_degree_is_caught():
    assert gate_rewrite(SOURCE, "Built ETL pipelines in Python at Google for the risk team.",
                        PROFILE) == ["Google"]
    assert gate_rewrite(SOURCE, "Applied Stanford coursework to ETL pipelines in Python.",
                        PROFILE) == ["Stanford"]
    assert gate_rewrite(SOURCE, "PhD-trained engineer who built ETL pipelines in Python.",
                        PROFILE) == ["PhD-trained"]


def test_credentials_by_name_cpa_pmp_licensed():
    assert new_claims("Licensed CPA who reconciled the ledgers.", PROFILE) == ["Licensed CPA"]
    assert new_claims("PMP certified lead for the data platform.", PROFILE) == ["PMP certified"]
    with_pmp = dict(PROFILE, certifications=["PMP"])
    assert new_claims("PMP-certified lead for the data platform.", with_pmp) == []


def test_a_degree_written_differently_still_traces_back():
    phd = dict(PROFILE, education=[{"school": "MIT", "degree": "Doctor of Philosophy in Physics"}])
    assert new_claims("Physics PhD who built ETL pipelines.", phd) == []
    assert new_claims("Holds an MBA and built ETL pipelines.", phd) == ["MBA"]


def test_a_brain_fact_counts_as_the_persons_material():
    # The person told the brain about a certification the profile form never captured.
    fact = "certification AWS Certified Data Engineer, Associate"
    assert gate_rewrite(SOURCE, "AWS Certified engineer who built ETL pipelines in Python.",
                        PROFILE, fact) == []


def test_ordinary_capitals_are_not_claims():
    # Sentence starts and months are not names; known words in capitals are fine too.
    assert claims("Led the migration in March. Cut costs by a third.") == []
    assert gate_rewrite(SOURCE, "Python, AWS: Built ETL pipelines in Python on AWS for the "
                                "Credit Risk team.", PROFILE) == []


def test_an_invented_figure_is_reverted_too():
    assert gate_rewrite(SOURCE, "Built ETL pipelines in Python on AWS, cutting runtime 40%.",
                        PROFILE) == ["40"]


class _CertLLM(FakeLLM):
    """A model that 'strengthens' the first bullet with a certification nobody holds."""

    def reword_bullet(self, original_text, supported_jd_terms, target_len_chars, jd_text):
        if original_text == SOURCE:
            return "AWS Certified engineer who built ETL pipelines in Python on AWS."
        return super().reword_bullet(original_text, supported_jd_terms, target_len_chars, jd_text)


def test_the_tailoring_pass_reverts_the_bullet_and_records_why():
    reverted: list = []
    out = _tailor_bullets(PROFILE, JD, _CertLLM(), reverted=reverted)
    bullets = out["experience"][0]["roles"][0]["bullets"]
    assert bullets[0] == SOURCE                                  # the person's own wording
    assert "Certified" not in " ".join(bullets)                  # never published
    assert reverted and reverted[0]["claims"] == ["AWS Certified"]
    assert reverted[0]["label"] == "Acme Bank, Data Engineer"
    # The second bullet mirrors JD tools the role really shows, so its rewrite stands.
    assert "Kubernetes" in bullets[1]
    note = describe(reverted)
    assert "kept your own wording" in note and "AWS Certified" in note
    assert "—" not in note and "–" not in note


def test_add_this_keyword_drafts_are_gated_too():
    problems = validate_draft("Kubernetes", "AWS Certified, deployed reporting services with "
                              "Kubernetes and Airflow.", "Deployed reporting services with "
                              "Kubernetes and Airflow.", "rewrite", PROFILE,
                              "Deployed reporting services with Kubernetes and Airflow.", JD)
    assert any("AWS Certified" in p for p in problems)


def test_the_session_gate_reads_the_assembled_profile_and_brain_facts_never_eeo(tmp_path, monkeypatch):
    from intake.memory import ConversationMemory
    from ui.records import CVRecords
    from ui.session import WebIntake
    s = WebIntake(JD, "x", FakeLLM(), ConversationMemory(":memory:"), CVRecords(":memory:"),
                  tmp_path, jobname="cv", palace_dir=tmp_path / "p")
    s.saved_profile = dict(PROFILE, certifications=["AWS Certified Data Engineer"])   # Phase 6 assembled

    class _Brain:
        def facts(self, store=None, status=None):
            return [{"key": "certification", "subject": "", "value": "Licensed CPA"},
                    {"key": "@gender", "subject": "", "value": "Prefersnotosay Zandoria"}]
    monkeypatch.setattr(s, "_brain", lambda: _Brain())
    material = s._brain_material()
    assert material[0] is s.saved_profile
    assert any("Licensed CPA" in m for m in material if isinstance(m, str))
    assert not any("Zandoria" in m for m in material if isinstance(m, str))   # EEO never read
    assert gate_rewrite(SOURCE, "AWS Certified engineer who built ETL pipelines in Python.",
                        PROFILE, *material) == []
