"""A step where some actions failed but others changed the form is progress (2026-11-23, Gainwell):
the run parked "4 steps in a row failed (ok, picked ...; ok, picked ...)" on a SuccessFactors page
where three of four country lists were set each step. Only a step where nothing worked counts."""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "shell-electron" / "agent.js").read_text(encoding="utf-8")


def test_a_step_that_changed_the_form_is_not_a_failed_step():
    loop = SRC[SRC.index("let stepFailed = false;"):SRC.index("failed = stepFailed ? failed + 1 : 0;") + 60]
    assert "stepChanged = true" in loop
    assert "r.changed !== false" in loop
    assert "if (stepFailed && stepChanged && !submitTried) stepFailed = false;" in loop
    # reading the page is never progress on its own
    for op in ("inspect", "list_options", "read_errors", "screenshot"):
        assert f'a.op !== "{op}"' in loop


def test_pressing_submit_still_counts_as_a_failed_step():
    loop = SRC[SRC.index("let stepFailed = false;"):SRC.index("failed = stepFailed ? failed + 1 : 0;")]
    assert 'r.note === "submit-guard") { submitTried = true;' in loop
