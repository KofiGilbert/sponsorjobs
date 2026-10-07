"""Rewrites must not reach a length target with hollow phrases (tailoring/filler.py)."""
from tailoring.filler import filler_added, is_padded

ORIG = ("Established and monitored operational workflows and system processes that scaled to "
        "support 5,000+ daily orders.")


def test_the_padded_sentence_from_the_first_real_run_is_rejected():
    padded = (ORIG[:-1] + ", demonstrating the analytical rigor, requirements discipline, and "
              "stakeholder communication essential to complex platform implementations and "
              "successful delivery outcomes.")
    assert is_padded(ORIG, padded)
    assert "demonstrating" in filler_added(ORIG, padded)


def test_a_concrete_rewrite_passes():
    assert not is_padded(ORIG, "Built and monitored the operational workflows and system "
                               "processes behind 5,000+ daily orders.")


def test_the_persons_own_wording_is_never_counted_as_filler():
    assert not is_padded("Ensured compliance across teams.",
                         "Ensured compliance across four regional teams.")


def test_one_connective_in_a_rewrite_that_did_not_grow_is_allowed():
    assert not is_padded("Cut the monthly close from ten days to four by automating the reports.",
                         "Automated the reports, ensuring a four-day monthly close, down from ten.")
