"""What an ad says about sponsorship, on the real sentences found in the live feed (2026-10-08)."""
from sourcing.adstance import (NOT_OFFERED, OFFERED, RESTRICTED, UNKNOWN, ad_stance,
                               short_sentence)

# Ads that close the role to anyone without US citizenship, a green card, or a clearance. The
# phrasing comes from sourcing/quality.py's board filter (and the green-card lines from live ads).
RESTRICTED_ADS = [
    "U.S. citizenship is required for this position.",
    "Applicants must be a U.S. citizen or permanent resident.",
    "Must be a US citizen or green card holder.",
    "Candidates must be US citizens or permanent residents due to federal contract requirements.",
    "Only US citizens and green card holders will be considered.",
    "Permanent residency required.",
    "Must hold a valid green card.",
    "This position requires an active Secret clearance.",
    "Ability to obtain a security clearance is required.",
    "Requires TS/SCI with polygraph.",
]
# Green cards named POSITIVELY: sponsorship, not a bar. Must never read as restricted.
GREEN_CARD_YES = [
    "We sponsor H-1B visas and green cards for our employees.",
    "Green card sponsorship is available after one year.",
]

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
    assert is_sponsor_relevant({"source": "greenhouse", "ad_stance": "restricted"}) is False


# -- restricted: citizenship / green card / clearance ------------------------------------- #

def test_every_citizenship_or_clearance_bar_reads_as_restricted_with_its_sentence():
    for s in RESTRICTED_ADS:
        r = ad_stance(s)
        assert r["stance"] == RESTRICTED, s
        assert r["sentence"] == s.rstrip("."), s          # the ad's own words, for the chip


def test_us_abbreviation_does_not_split_the_sentence():
    """"U.S. citizenship is required" used to split into "U", "S", "citizenship is required",
    and the rule never saw the US. The quoted sentence must be the whole line."""
    r = ad_stance("Benefits are great. U.S. citizenship is required for this role. Apply now.")
    assert r == {"stance": RESTRICTED, "sentence": "U.S. citizenship is required for this role"}


def test_a_green_card_offered_is_sponsorship_not_a_bar():
    for s in GREEN_CARD_YES:
        assert ad_stance(s)["stance"] != RESTRICTED, s
    assert ad_stance(GREEN_CARD_YES[1])["stance"] == OFFERED
    # And the board filter agrees: a positive green-card line never hides the role.
    from sourcing.quality import role_excludes_international
    for s in GREEN_CARD_YES:
        assert not role_excludes_international("Some Startup", s), s


def test_restricted_outranks_no_sponsorship_which_outranks_yes():
    text = ("We do sponsor visas for many roles. This position is not eligible for visa "
            "sponsorship. Must be a U.S. citizen with an active security clearance.")
    r = ad_stance(text)
    assert r["stance"] == RESTRICTED and "U.S. citizen" in r["sentence"]
    r = ad_stance("We do sponsor visas for many roles. This position is not eligible for visa sponsorship.")
    assert r["stance"] == NOT_OFFERED


def test_the_board_filter_and_the_chip_read_the_same_sentences():
    """One source of phrasing (RESTRICTED_SRC): whatever the extension flags as restricted, the
    feed also keeps off the board, and vice versa for the citizenship / clearance branches."""
    from sourcing.quality import role_excludes_international
    for s in RESTRICTED_ADS:
        assert role_excludes_international("Some Startup", s), s


def test_short_sentence_cuts_at_a_word_and_leaves_short_ones_alone():
    assert short_sentence("U.S. citizenship is required") == "U.S. citizenship is required"
    long = ("Applicants must be legally authorized to work in the United States for any employer "
            "without requiring current or future visa sponsorship, for example employment-based "
            "visas such as H-1B or F-1 OPT")
    cut = short_sentence(long)
    assert cut.endswith("...") and len(cut) <= 144
    body = cut[:-3]
    assert long.startswith(body) and not body.endswith((" ", ","))
    assert not long[len(body)].isalnum()                       # cut at a word boundary
