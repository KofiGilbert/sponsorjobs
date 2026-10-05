"""Tailor's own autonomous form-filler -- the PII-safe planning core (no browser, no browser-use).

The brain runs through our existing broker: the model sees only the form's STRUCTURE plus placeholder
KEYS, returns a fill plan, and we resolve that plan to the person's REAL values LOCALLY. These pin the
privacy guarantee (no real personal data reaches the model), the hard validation of the model's plan,
and the local substitution. Deterministic FakeLLM, so no network/model needed.
"""
from __future__ import annotations

from autoapply.fields import (FillAction, FormField, applicant_from_record,
                              resolve_plan)
from autoapply.planner import build_fill_plan, parse_plan
from llm.base import FakeLLM

FIELDS = [
    FormField("f1", "First name", "text", required=True),
    FormField("f2", "Last name", "text", required=True),
    FormField("f3", "Email", "email", required=True),
    FormField("f4", "Phone number", "tel"),
    FormField("f5", "Resume", "file"),
    FormField("f6", "Are you authorized to work in the US?", "select", options=["Yes", "No"]),
]

APPLICANT = {"first_name": "Kofi", "last_name": "Gilbert", "email": "kofi@example.com",
             "phone": "555-100-2000"}


def test_fake_planner_maps_fields_by_label():
    plan = build_fill_plan(FIELDS, FakeLLM(), role="Engineer")
    by = {a.ref: a for a in plan}
    assert by["f1"].source == "first_name" and by["f2"].source == "last_name"
    assert by["f3"].source == "email" and by["f4"].source == "phone"
    assert by["f5"].op == "upload"                      # the file input becomes an upload


def test_parse_plan_rejects_unknown_fields_and_keys():
    raw = {"actions": [
        {"ref": "f3", "op": "fill", "source": "email"},        # valid
        {"ref": "GHOST", "op": "fill", "source": "email"},     # unknown field -> dropped
        {"ref": "f4", "op": "fill", "source": "ssn"},          # bogus key -> source cleared -> dropped (no value)
        {"ref": "f6", "op": "select", "value": "Yes"},         # literal choice -> kept
    ]}
    plan = parse_plan(raw, FIELDS)
    refs = {a.ref for a in plan}
    assert refs == {"f3", "f6"}
    assert next(a for a in plan if a.ref == "f6").value == "Yes"


def test_resolve_substitutes_real_values_locally():
    plan = [FillAction("f1", "fill", source="first_name"),
            FillAction("f3", "fill", source="email"),
            FillAction("f5", "upload"),
            FillAction("f6", "select", value="Yes"),
            FillAction("f9", "fill", source="country")]        # key we don't hold -> dropped
    ops = resolve_plan(plan, APPLICANT, resume_path="/tmp/cv.pdf")
    by = {o.ref: o for o in ops}
    assert by["f1"].text == "Kofi" and by["f1"].is_sensitive is True
    assert by["f3"].text == "kofi@example.com"
    assert by["f5"].op == "upload" and by["f5"].path == "/tmp/cv.pdf"
    assert by["f6"].text == "Yes" and by["f6"].is_sensitive is False   # a literal, not personal
    assert "f9" not in by                                # no value held -> never guessed


def test_no_real_personal_data_ever_reaches_the_model():
    """The load-bearing privacy test: whatever we hand the model must NOT contain the real name,
    email, or phone -- only the field structure and placeholder KEYS."""
    seen = {}

    class Spy(FakeLLM):
        def plan_form_fill(self, fields, available_keys, role="", company="", jd=""):
            seen["fields"] = fields
            seen["keys"] = available_keys
            seen["ctx"] = f"{role} {company} {jd}"
            return super().plan_form_fill(fields, available_keys, role, company, jd)

    build_fill_plan(FIELDS, Spy(), role="Engineer", company="Acme",
                    jd="We need Python.")               # the person's data is NOT passed here at all
    blob = repr(seen).lower()
    for secret in ("kofi", "gilbert", "kofi@example.com", "555-100-2000"):
        assert secret not in blob                        # no real personal value ever left the box
    assert "first_name" in seen["keys"] and "email" in seen["keys"]   # only KEYS travel


def test_applicant_from_record_pulls_identity():
    a = applicant_from_record({"profile": {"identity": {
        "name": "Kofi Gilbert", "email": "k@x.com", "phone": "123", "linkedin": "in/kofi"}}})
    assert a["first_name"] == "Kofi" and a["last_name"] == "Gilbert"
    assert a["full_name"] == "Kofi Gilbert" and a["email"] == "k@x.com" and a["linkedin"] == "in/kofi"
    assert "country" not in a                            # absent fields are simply omitted


def test_planner_returns_empty_on_model_error():
    class Broken(FakeLLM):
        def plan_form_fill(self, *a, **k):
            raise RuntimeError("broker down")
    assert build_fill_plan(FIELDS, Broken()) == []       # no plan -> caller drops to assisted
