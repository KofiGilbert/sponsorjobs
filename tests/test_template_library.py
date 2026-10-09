"""The template library (2026-10-09): sixteen audience-labelled templates on one engine.

Built from career-center research: templates differ by section set, order and headings, never
decoration; names say WHO a template is for (Yale, Microsoft and NN/g all label by audience);
the featured strip leads with the fields that receive most US sponsorship (software, data,
engineering, then finance)."""
import json
from pathlib import Path

import ui.app as app
from tailoring.assembler import _SECTION_RENDERERS

TDIR = Path("config/templates")
MANIFESTS = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in TDIR.glob("*.json")}


def test_library_has_the_sixteen_audience_templates():
    assert set(MANIFESTS) == {"shetty", "software", "data", "engineering", "experienced", "summary",
                              "product", "operations", "switcher", "banking", "consulting", "mba",
                              "accounting", "phd", "nursing", "educator"}


def test_every_section_and_heading_key_is_one_the_engine_renders():
    for name, m in MANIFESTS.items():
        for sec in m["sections"]:
            assert sec in _SECTION_RENDERERS, f"{name}: unknown section {sec}"
        for sec in m.get("required_sections") or []:
            assert sec in m["sections"], f"{name}: requires {sec} but never renders it"
        for key in (m.get("headings") or {}):
            assert key in _SECTION_RENDERERS or key == "additional", f"{name}: heading for unknown {key}"
        for key in (m.get("elicit") or {}):
            assert key in m["sections"], f"{name}: asks about {key} but never renders it"


def test_names_say_who_the_template_is_for_and_carry_no_dashes():
    for name, m in MANIFESTS.items():
        for field in ("display_name", "category", "best_for", "interview_style"):
            text = m.get(field) or ""
            assert text, f"{name}: no {field}"
            assert "—" not in text and "–" not in text, f"{name}: dash in {field}"
        assert isinstance(m.get("order"), int), f"{name}: no order"


def test_the_strip_features_seven_led_by_the_sponsored_fields():
    feats = [t["name"] for t in app._templates() if t["featured"]]
    assert feats == ["shetty", "software", "data", "engineering", "experienced", "banking", "consulting"]


def test_templates_are_listed_in_manifest_order_grouped_by_audience():
    cats = [t["category"] for t in app._templates()]
    # Each category is contiguous, so the gallery's grouping follows the listing order.
    seen, last = [], None
    for c in cats:
        if c != last:
            assert c not in seen, f"category {c} split by another"
            seen.append(c); last = c
    assert seen == ["Students and New Grads", "Experienced Professionals", "Business and Finance",
                    "Research and Academia", "Healthcare and Education"]


def test_the_jd_reader_picks_the_specialised_template(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "empty.db"))
    cases = {
        "Registered Nurse (RN), BSN required, ICU, BLS and ACLS, Epic charting.": "nursing",
        "Software Engineer, backend, Go and Kubernetes, GitHub portfolio welcome.": "software",
        "Data Scientist: Python, SQL, scikit-learn, experimentation.": "data",
        "Mechanical Engineer co-op, SolidWorks and CAD, FE exam a plus.": "engineering",
        "High school teacher, secondary mathematics, lesson plan and IEP experience.": "educator",
        "Research Scientist, PhD in machine learning, publications required.": "phd",
        "Audit associate, CPA candidate preferred, Big Four.": "accounting",
        "Supply chain analyst, SAP, inventory and procurement.": "operations",
        "Product Manager owning the roadmap, UX research, Figma.": "product",
    }
    for jd, want in cases.items():
        assert app._suggest_template(jd)["name"] == want, jd


def test_every_preview_overrides_only_known_profile_keys():
    from tailoring.keywords import PROFILE_SECTIONS
    for name, m in MANIFESTS.items():
        for k in (m.get("preview") or {}):
            assert k in PROFILE_SECTIONS, f"{name}: preview overrides unknown key {k}"
