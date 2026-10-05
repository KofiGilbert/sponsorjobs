r"""LaTeX-escaping of tailored bullet text (robustness for real LLM output).

The tailoring engine splices model-written prose into ``\item`` interiors. Real
prose contains characters that are special in LaTeX — ``% & $ # _ { } ~ ^ \`` —
and an unescaped ``%`` alone silently comments out the rest of a line, breaking
the compile. The FakeLLM used in tests never produces these, so this guard is
what makes the engine safe against *real* model output.

:class:`SanitizingLLM` wraps any :class:`LLMBackend`: it feeds the model clean,
unescaped prose and escapes whatever comes back, so every value that reaches the
template is compile-safe. It also neutralizes any stray LaTeX command the model
might emit (a rogue ``\foo`` becomes literal text), which is the right behavior —
bullet interiors are content, never formatting (CLAUDE.md §8).
"""

from __future__ import annotations

# Characters that must be escaped for LaTeX, in application order.
_ESCAPES = [
    ("&", r"\&"),
    ("%", r"\%"),
    ("$", r"\$"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("{", r"\{"),
    ("}", r"\}"),
    ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
]
_BACKSLASH_SENTINEL = "\x00"


def escape_latex(text: str) -> str:
    """Escape LaTeX special characters in plain prose."""
    # Protect real backslashes first so replacements below don't double-escape.
    out = text.replace("\\", _BACKSLASH_SENTINEL)
    for ch, rep in _ESCAPES:
        out = out.replace(ch, rep)
    out = out.replace(_BACKSLASH_SENTINEL, r"\textbackslash{}")
    return out


def unescape_latex(text: str) -> str:
    """Reverse :func:`escape_latex` back to plain prose (idempotent-friendly)."""
    out = text.replace(r"\textbackslash{}", _BACKSLASH_SENTINEL)
    # Reverse the simple char escapes.
    for ch, rep in _ESCAPES[:7]:  # the single-char ones (& % $ # _ { })
        out = out.replace(rep, ch)
    out = out.replace(r"\textasciitilde{}", "~").replace(
        r"\textasciicircum{}", "^"
    )
    out = out.replace(_BACKSLASH_SENTINEL, "\\")
    return out


def sanitize_bullet_text(text: str) -> str:
    """Make model-written bullet text safe to splice into a LaTeX template.

    Idempotent: unescape any escaping the model already applied, then re-escape
    uniformly. Whether the model returns ``40%`` or ``40\\%``, the result is a
    single correct ``40\\%``.
    """
    text = text.strip()
    text = unescape_latex(text)
    text = escape_latex(text)
    return text.strip()


class SanitizingLLM:
    """Wrap an :class:`LLMBackend` so all bullet text it returns is compile-safe.

    Input prose is unescaped before the model sees it (so it isn't confused by
    ``\\%``/``\\$`` from the template); output prose is escaped before it reaches
    the template.
    """

    def __init__(self, inner) -> None:
        self.inner = inner

    def generate_intake_questions(self, jd_text, saved_profile):
        return self.inner.generate_intake_questions(jd_text, saved_profile)

    def reword_bullet(self, original_text, supported_jd_terms, target_len_chars, jd_text):
        clean = unescape_latex(original_text)
        out = self.inner.reword_bullet(
            clean, supported_jd_terms, target_len_chars, jd_text
        )
        return sanitize_bullet_text(out)

    def shorten_bullet(self, text, max_len_chars):
        out = self.inner.shorten_bullet(text, max_len_chars)
        return sanitize_bullet_text(out)
