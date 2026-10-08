"""What an ad says about sponsorship, on the real sentences found in the live feed (2026-10-08)."""
from sourcing.adstance import NOT_OFFERED, OFFERED, UNKNOWN, ad_stance

NO = [
    "GM does not provide immigration-related sponsorship for this role.",
    "Fidelity will not provide immigration sponsorship for this position.",
    "This position is not eligible for visa sponsorship.",
    "Allstate generally does not sponsor individuals for employment-based visas for this position.",
    "• This position is not available for visa sponsorship",
    "Applicants for employment in the US must have work authorization that does not now or in the "
    "future require sponsorship of a visa for employment authorization in the United States.",
    "Applicants must be legally authorized to work in the United States for any employer without "
    "requiring current or future visa sponsorship (for example, employment-based visas such as "
    "H-1B, F-1/OPT, or similar).",
    "Do not apply for this role if you will need GM immigration sponsorship now or in the future.",
    "We are unable to sponsor visas for this role.",
    "Visa sponsorship is not available for this position.",
    "No visa sponsorship.",
]
YES = [
    "Visa sponsorship is available.",
    "This role is eligible for visa sponsorship.",
    "Visa sponsorship: We do sponsor visas!",
    "Please note, this opportunity may offer work visa sponsorship",
    "We offer visa sponsorship for qualified candidates.",
    "We are happy to sponsor work visas for the right candidate.",
    "H-1B transfer welcome.",
]
NEITHER = [
    "Participate in conferences and Arcesium sponsored marketing initiatives.",
    "You will sponsor the quarterly hackathon and mentor interns.",
    "Translate complex technical situations into decision frameworks: options, tradeoffs.",
    "We're on a mission to connect a billion people with optimism and civility.",
    "",
]


def test_every_real_no_sentence_reads_as_not_offered():
    for s in NO:
        assert ad_stance(s)["stance"] == NOT_OFFERED, s


def test_every_real_yes_sentence_reads_as_offered():
    for s in YES:
        assert ad_stance(s)["stance"] == OFFERED, s


def test_unrelated_uses_of_sponsor_and_option_words_are_unknown():
    for s in NEITHER:
        assert ad_stance(s)["stance"] == UNKNOWN, s


def test_a_no_for_this_role_outranks_a_general_yes():
    text = ("Visa sponsorship: We do sponsor visas! However, this position is not eligible for "
            "visa sponsorship. Apply today.")
    r = ad_stance(text)
    assert r["stance"] == NOT_OFFERED and "not eligible" in r["sentence"]


def test_the_deciding_sentence_is_returned_for_the_hover():
    r = ad_stance("Great team. This role is eligible for visa sponsorship. Hybrid in Austin.")
    assert r["stance"] == OFFERED and r["sentence"] == "This role is eligible for visa sponsorship"


def test_an_ad_that_refuses_sponsorship_is_not_sponsor_relevant():
    from sourcing.quality import is_sponsor_relevant
    row = {"source": "freehire", "sponsorship_stated": True, "ad_stance": "not_offered"}
    assert is_sponsor_relevant(row) is False          # the aggregator flag was wrong; the ad rules
    assert is_sponsor_relevant({"source": "freehire", "ad_stance": "offered"}) is True
    assert is_sponsor_relevant({"source": "greenhouse", "ad_stance": "not_offered"}) is False
