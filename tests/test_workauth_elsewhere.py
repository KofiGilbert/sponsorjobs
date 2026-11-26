"""(2026-11-26, the owner, Instacart) "Are you legally entitled to work in Canada?" is a work
authorization question about a country the saved profile says nothing about. When the person
saved their work authorization (for the US) and their countries of citizenship are known and do
not include Canada, the profile can tell: No (and sponsorship would be needed). Otherwise the
person is asked, as before. A job in a country the saved authorization does not cover is said
plainly before anything is filled."""
from __future__ import annotations

import pytest

import ui.app as app
from autoapply import workauth as W

US_OPT = {"countries": {"US": {"status": "", "authorized": "Yes", "sponsor_now": "No", "sponsor_future": "Yes",
                               "auth_end": ""}}, "relocate": ""}
YN = ["Yes", "No"]


def _wa(**k):
    return {**US_OPT, **k}


@pytest.mark.parametrize("q,want", [
    ("Are you legally entitled to work in Canada?", "No"),
    ("Are you legally eligible to work in Canada?", "No"),
    ("Do you have the right to work in the UK?", "No"),
    ("Will you require sponsorship to work in Canada?", "Yes"),
    ("Are you authorized to work in the United States?", "Yes"),      # the saved country, as before
])
def test_another_country_is_answered_from_the_citizenships(q, want):
    assert W.answer(q, _wa(citizenships=["Ghana"]), options=YN, field_type="select") == want


@pytest.mark.parametrize("wa", [
    _wa(),                                            # citizenship not known: asked
    _wa(citizenships=["CA"]),                          # a Canadian: never "No"
    {"countries": {}, "relocate": "", "citizenships": ["Ghana"]},   # nothing saved at all: asked
])
def test_never_guessed_when_the_profile_cannot_tell(wa):
    assert W.answer("Are you legally entitled to work in Canada?", wa, options=YN, field_type="select") is None


def test_the_eu_is_never_inferred():
    assert W.answer("Are you eligible to work in the EU?", _wa(citizenships=["Ghana"]), options=YN,
                    field_type="select") is None


def test_the_country_note_before_autofill():
    assert app._country_note("CA", US_OPT) == "This job is in Canada; your saved work authorization is for the US"
    assert app._country_note("US", US_OPT) == ""
    assert app._country_note("CA", {"countries": {}}) == ""              # nothing saved: no note
    both = {"countries": {**US_OPT["countries"], "CA": {"status": "", "authorized": "Yes", "sponsor_now": "",
                                                         "sponsor_future": "", "auth_end": ""}}}
    assert app._country_note("CA", both) == ""
