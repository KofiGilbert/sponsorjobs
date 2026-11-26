"""(2026-11-26, the owner) Where the person wants to work, remote, hybrid or in the office, is a
standing preference: asked once on a form, kept in Memory (Edit / Forget), and reused on later
forms by meaning, whatever the wording. A question about one office is not it."""
from __future__ import annotations

import pytest

from applicant import keys as K
from applicant import mapper
from applicant import page
from autoapply.answers import AnswerLibrary, normalize

STRIPE = "If this role offers the option to work from a remote location, do you plan to work remotely?*"


@pytest.mark.parametrize("q", [
    STRIPE, "Preferred work arrangement", "Do you prefer to work remotely, hybrid or on-site?",
    "Remote / Hybrid / On-site preference", "Remote or hybrid?", "Which working model would you prefer?",
    "Do you intend to work in the office?",
])
def test_every_wording_is_one_key(q):
    assert normalize(q) == "@work_location"


@pytest.mark.parametrize("q", [
    "Are you able to work on-site in Austin 5 days a week?", "Are you currently authorized to work in the US?",
    "Preferred work location", "Will you require sponsorship?", "Do you plan to work in Texas?", "Hybrid",
])
def test_other_questions_are_not_it(q):
    assert normalize(q) != "@work_location"


def test_the_brain_knows_the_key_and_the_model_mapper_can_map_new_wordings_to_it():
    assert K.get("@work_location") is not None and K.sensitivity("@work_location") == K.NORMAL
    assert "@work_location" in mapper.KEYS


def test_answered_once_it_is_reused_by_meaning_and_shows_in_memory(tmp_path):
    lib = AnswerLibrary(tmp_path / "answers.db")
    lib.save(STRIPE, "Yes, I intend to work remotely.", "you")
    hit = lib.lookup("Preferred work arrangement")
    assert hit and hit["answer"] == "Yes, I intend to work remotely."
    rows = page.answer_rows(None, lib, {})
    row = next(r for r in rows if r["id"] == "key:@work_location")
    assert row["label"] == "Where you want to work" and row["kind"] == "text"
    assert row["value"] == "Yes, I intend to work remotely."
