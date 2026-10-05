"""Unit tests for LaTeX escaping of tailored bullet text.

These lock in the fix for the class of failure a real LLM run surfaces: prose
containing %, &, $, etc. must be escaped so it can't break the compile.
"""

from __future__ import annotations

from tailoring.latex_text import (
    SanitizingLLM,
    escape_latex,
    sanitize_bullet_text,
    unescape_latex,
)


def test_special_characters_are_escaped():
    assert escape_latex("cut cost 40% and R&D $2M") == r"cut cost 40\% and R\&D \$2M"


def test_escape_is_reversible():
    plain = "a & b, 99% of $2, x_1 #tag {ok}"
    assert unescape_latex(escape_latex(plain)) == plain


def test_sanitize_is_idempotent_on_pre_escaped_text():
    # Whether the model returns raw or already-escaped, we get one correct form.
    assert sanitize_bullet_text("99% done") == sanitize_bullet_text(r"99\% done")
    assert sanitize_bullet_text(r"99\% done") == r"99\% done"


def test_rogue_command_becomes_literal():
    # A stray LaTeX command must be neutralized, not passed through.
    out = sanitize_bullet_text(r"\evilmacro wrecks things")
    assert r"\evilmacro" not in out
    assert r"\textbackslash{}" in out


class _EchoLLM:
    def generate_intake_questions(self, jd_text, saved_profile):
        return ["q"]

    def reword_bullet(self, original_text, supported_jd_terms, target_len_chars, jd_text):
        # Echo the (unescaped) original back with a % the template would choke on.
        return original_text + " improved 50%"

    def shorten_bullet(self, text, max_len_chars):
        return text[:max_len_chars]


def test_sanitizing_wrapper_feeds_clean_input_and_escapes_output():
    wrapped = SanitizingLLM(_EchoLLM())
    # Original arrives pre-escaped (as it is in the template source).
    out = wrapped.reword_bullet(r"captured 99\% of moves", [], 200, "jd")
    # The model saw clean prose; the returned value is fully escaped and safe.
    assert "99\\%" in out and "50\\%" in out
    assert "%" not in out.replace("\\%", "")  # no bare % remains
