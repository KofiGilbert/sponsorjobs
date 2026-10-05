"""§10: surgical-edit confinement and aggressive rewording.

Covers:
  * "Diff of original vs tailored .tex changes ONLY targeted bullet interiors."
  * "Content sections (Experience, Projects, Skills) are aggressively reworded
     toward the JD."
"""

from __future__ import annotations

import re

from tailoring.latex_template import LatexTemplate
from tailoring.surgical_editor import apply_edits, verify_confined
from tailoring.keywords import term_present


def test_edits_are_confined_to_bullet_interiors(template_source):
    """Only the interiors of targeted bullets may change; all else byte-equal."""
    tmpl = LatexTemplate(template_source)
    targets = tmpl.tailorable_bullets()[:3]
    edits = {b.id: f"Reworded interior for {b.id}." for b in targets}

    edited = apply_edits(tmpl, edits)
    result = verify_confined(tmpl, edited, edits)
    assert result.ok, result.violations

    # Preamble (everything before \begin{document}) must be byte-identical.
    marker = "\\begin{document}"
    assert template_source.split(marker)[0] == edited.split(marker)[0]

    # Every section heading survives unchanged.
    headings = re.findall(r"\\textbf\{\\textsc\{\\large[^}]*\}\}", template_source)
    for h in headings:
        assert h in edited

    # The new interior text is present; a non-targeted bullet is untouched.
    assert "Reworded interior for" in edited
    untouched = tmpl.tailorable_bullets()[5]
    assert untouched.text in edited


def test_confinement_holds_under_an_independent_oracle(template_source):
    """`verify_confined` derives 'outside' from the parser's OWN interior offsets, so its
    pass is partly tautological. Re-check with an INDEPENDENT oracle: mask every ``\\item``
    interior via a standalone regex in both the original and the edited .tex, then assert the
    masked skeletons (every non-interior byte — preamble, headings, ``\\end{itemize}``, spacing
    macros) are byte-identical. A boundary-widening parser regression that let an edit touch a
    formatting macro would change a skeleton byte and fail here even while verify_confined passed.
    """
    tmpl = LatexTemplate(template_source)
    targets = tmpl.tailorable_bullets()[:4]
    edits = {b.id: f"Independent-oracle rewrite number {i}." for i, b in enumerate(targets)}
    edited = apply_edits(tmpl, edits)

    interior = re.compile(
        r"(\\item(?![A-Za-z]))(.*?)(?=\\item(?![A-Za-z])|\\end\{itemize\})", re.DOTALL)

    def skeleton(src: str) -> str:
        # Replace each item interior with a fixed sentinel, leaving all else untouched.
        return interior.sub(lambda m: m.group(1) + "\x00", src)

    assert skeleton(template_source) == skeleton(edited)   # only interiors changed
    assert "Independent-oracle rewrite number 0." in edited  # ...and they really did change


def test_tampering_outside_interiors_is_detected(template_source):
    """A change to the preamble is flagged as an out-of-scope violation."""
    tmpl = LatexTemplate(template_source)
    b = tmpl.tailorable_bullets()[0]
    edits = {b.id: "A safe interior rewrite."}
    edited = apply_edits(tmpl, edits)

    # Now tamper with the geometry margin — strictly outside any bullet.
    tampered = edited.replace("right=0.7cm", "right=1.3cm")
    assert tampered != edited

    result = verify_confined(tmpl, tampered, edits)
    assert not result.ok
    assert result.violations


def test_unknown_bullet_id_rejected(template_source):
    tmpl = LatexTemplate(template_source)
    try:
        tmpl.render({"does-not-exist-1": "x"})
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for unknown bullet id")


def test_experience_and_projects_are_reworded_toward_jd(
    template_source, sample_profile, jd_quant, fake_llm, workdir
):
    """Experience/Projects bullets are reworded and carry JD vocabulary."""
    from tailoring.tailor import tailor_resume

    res = tailor_resume(
        template_source, sample_profile, jd_quant, fake_llm, workdir
    )
    tmpl = LatexTemplate(template_source)

    exp_projects = [
        b for b in tmpl.tailorable_bullets()
        if b.section.lower() in {"experience", "projects"}
    ]
    edited_ids = set(res.edits)
    # A healthy majority of experience/projects bullets were actually reworded.
    assert sum(b.id in edited_ids for b in exp_projects) >= len(exp_projects) // 2

    # At least one JD term the profile supports was woven into the edits.
    joined = "\n".join(res.edits.values())
    assert term_present("Python", joined) or term_present("risk management", joined)

    # Each rewrite differs from the original bullet text.
    for b in exp_projects:
        if b.id in res.edits:
            assert res.edits[b.id].strip() != b.text
