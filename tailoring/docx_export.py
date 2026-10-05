"""ATS-friendly Microsoft Word (.docx) export of a tailored résumé.

Why this exists (in addition to the LaTeX/PDF path): résumé parsers inside applicant
tracking systems read a .docx's XML text ordering more reliably than a PDF's glyph
stream, and older Taleo / iCIMS instances still prefer Word uploads. So for the
apply-through-an-ATS path we offer a clean .docx built from the SAME tailored PROFILE the
PDF is built from — not a lossy PDF->Word conversion.

The layout is deliberately parser-safe (the things that scramble ATS parsing, avoided):
  * single column, no tables, no text boxes, no images, no headers/footers
  * contact details live in the BODY (parsers skip headers/footers)
  * standard section headings (Summary / Education / Skills / Experience / Projects ...)
  * real Word bullet paragraphs, a common font, no icon glyphs

It reuses the assembler's `normalize_profile`, so a loose real-model profile renders the
same shape here as in the PDF. `profile_to_plaintext` returns what a naive parser would
read back — the basis for a "how a parser sees your CV" sanity preview.
"""

from __future__ import annotations

import re
from pathlib import Path

from .assembler import _DEFAULT_SECTION_ORDER, _as_bullets, normalize_profile

# Contact links rendered in the body, in this order, as plain text (a parser reads the
# URL text directly — no icons, no hyperlink-only rendering that some parsers drop).
_LINK_KEYS = (("LinkedIn", "linkedin"), ("GitHub", "github"),
              ("Website", "website"), ("Blog", "blog"))

_HEADINGS = {
    "summary": "Summary", "education": "Education", "skills": "Skills",
    "projects": "Projects", "experience": "Experience",
    "extracurricular": "Extracurricular", "interests": "Additional Information",
}


def _contact_line(ident: dict) -> str:
    bits: list[str] = []
    for key in ("address", "phone", "email"):
        if ident.get(key):
            bits.append(str(ident[key]).strip())
    for _label, key in _LINK_KEYS:
        if ident.get(key):
            bits.append(str(ident[key]).strip())   # the raw URL, as readable text
    return "  |  ".join(bits)


def _roles_of(entry: dict) -> list[dict]:
    """An entry is either multi-role (`roles`) or a single flat role — mirror assembler."""
    if isinstance(entry.get("roles"), list) and entry["roles"]:
        return entry["roles"]
    return [{"title": entry.get("title", ""), "dates": entry.get("dates", ""),
             "bullets": entry.get("bullets", [])}]


def _section_order(profile: dict) -> list[str]:
    """Summary first (if present), then the default résumé order — only sections that
    have content. Kept simple and ATS-conventional rather than template-manifest-driven."""
    order = (["summary"] if str(profile.get("summary") or "").strip() else []) \
        + list(_DEFAULT_SECTION_ORDER)
    return order


def build_docx(profile: dict, out_path: str | Path,
               section_order: list[str] | None = None) -> str:
    """Render `profile` to a clean, ATS-friendly .docx at `out_path`. Returns the path."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.shared import Pt, RGBColor

    p = normalize_profile(profile or {})
    ident = p.get("identity", {}) if isinstance(p.get("identity"), dict) else {}

    doc = Document()
    # A common, widely-parsed sans font at a compact size (single-page friendly).
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(2)
    # Narrow margins keep it near one page without cramping the parser.
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Pt(36)      # 0.5"
        section.left_margin = section.right_margin = Pt(54)      # 0.75"

    def _heading(text: str) -> None:
        """A bold, spaced, uppercase section heading with a thin bottom rule. The rule is a
        paragraph border (NOT a table), so it stays parser-safe."""
        h = doc.add_paragraph()
        h.paragraph_format.space_before = Pt(8)
        h.paragraph_format.space_after = Pt(3)
        run = h.add_run(text.upper())
        run.bold = True
        run.font.size = Pt(11.5)
        pPr = h._p.get_or_add_pPr()
        borders = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "6")
        bottom.set(qn("w:space"), "1")
        bottom.set(qn("w:color"), "888888")
        borders.append(bottom)
        pPr.append(borders)

    def _bullets(items) -> None:
        for b in _as_bullets(items):
            doc.add_paragraph(b, style="List Bullet")

    def _row(left: str, right: str, bold_left: bool = True,
             small_caps: bool = False, italic_left: bool = False) -> None:
        """One line with `left` and (optionally) a right-aligned `right`, using a right
        tab stop — no table. Bold/italic per the résumé's visual hierarchy."""
        from docx.enum.text import WD_TAB_ALIGNMENT
        para = doc.add_paragraph()
        para.paragraph_format.space_after = Pt(1)
        r = para.add_run(left)
        r.bold = bold_left
        r.italic = italic_left
        if small_caps:
            r.font.small_caps = True
        if right:
            # Right tab stop at the usable width (~7") so the date/location aligns right.
            para.paragraph_format.tab_stops.add_tab_stop(Pt(504), WD_TAB_ALIGNMENT.RIGHT)
            para.add_run("\t")
            rr = para.add_run(right)
            rr.bold = True

    # -- header: name + contact, in the body -- #
    name = str(ident.get("name") or "").strip()
    if name:
        title = doc.add_paragraph()
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        tr = title.add_run(name)
        tr.bold = True
        tr.font.size = Pt(18)
    contact = _contact_line(ident)
    if contact:
        c = doc.add_paragraph()
        c.alignment = WD_ALIGN_PARAGRAPH.CENTER
        c.paragraph_format.space_after = Pt(6)
        c.add_run(contact).font.size = Pt(9.5)

    order = section_order or _section_order(p)
    for sec in order:
        if sec == "summary":
            text = str(p.get("summary") or "").strip()
            if not text:
                continue
            _heading(_HEADINGS["summary"])
            doc.add_paragraph(text)
        elif sec == "education":
            blocks = p.get("education") or []
            if not blocks:
                continue
            _heading(_HEADINGS["education"])
            for b in blocks:
                _row(b.get("school", ""), b.get("location", ""), small_caps=True)
                if b.get("degree") or b.get("date"):
                    _row(b.get("degree", ""), b.get("date", ""), bold_left=False)
                if b.get("courses"):
                    doc.add_paragraph(f"Courses: {b['courses']}")
        elif sec == "skills":
            skills = p.get("skills") or {}
            if not skills:
                continue
            _heading(_HEADINGS["skills"])
            for label, value in skills.items():
                para = doc.add_paragraph()
                para.add_run(f"{label}: ").bold = True
                para.add_run(str(value))
        elif sec in ("projects", "experience"):
            entries = p.get(sec) or []
            if not entries:
                continue
            _heading(_HEADINGS[sec])
            for e in entries:
                _row(e.get("org", ""), e.get("location", ""), small_caps=True)
                url = str(e.get("link") or e.get("url") or "").strip()
                if sec == "projects" and url:
                    doc.add_paragraph(url).runs[0].font.size = Pt(9)
                for r in _roles_of(e):
                    if r.get("title") or r.get("dates"):
                        _row(r.get("title", ""), r.get("dates", ""), bold_left=True,
                             italic_left=True)
                    _bullets(r.get("bullets"))
        elif sec == "extracurricular":
            items = p.get("extracurricular") or []
            if not items:
                continue
            _heading(_HEADINGS["extracurricular"])
            for it in items:
                _row(it.get("title", ""), it.get("date", ""))
                _bullets(it.get("bullets"))
        elif sec == "interests":
            interests = p.get("interests")
            if not interests:
                continue
            _heading(_HEADINGS["interests"])
            para = doc.add_paragraph()
            para.add_run("Interests: ").bold = True
            para.add_run(str(interests))

    out = str(out_path)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    return out


def build_cover_docx(identity: dict, body: str, out_path: str | Path) -> str:
    """Render a cover letter to a clean .docx: a name + contact header (in the body, so a
    parser reads it) followed by the letter's paragraphs. Returns the path."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Pt(54)
        section.left_margin = section.right_margin = Pt(72)

    ident = identity if isinstance(identity, dict) else {}
    name = str(ident.get("name") or "").strip()
    if name:
        h = doc.add_paragraph()
        run = h.add_run(name)
        run.bold = True
        run.font.size = Pt(14)
    contact = _contact_line(ident)
    if contact:
        c = doc.add_paragraph()
        c.paragraph_format.space_after = Pt(10)
        c.add_run(contact).font.size = Pt(9.5)

    # Blank-line-separated blocks become paragraphs; a single newline inside a block is a
    # soft wrap, so collapse it to a space (letters flow, not ragged one-word-per-line).
    for block in re.split(r"\n\s*\n", str(body or "").strip()):
        text = re.sub(r"\s*\n\s*", " ", block).strip()
        if text:
            doc.add_paragraph(text)

    out = str(out_path)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    return out


def build_screen_report_docx(screen: dict, out_path: str | Path) -> str:
    """Render a recorded-screen PRACTICE report to a clean .docx so the person can keep their
    feedback and track progress between sessions: the overall score, the competency breakdown,
    per-answer feedback and delivery, and what to do next. Practice feedback only, never a hiring
    decision. Returns the path."""
    from docx import Document
    from docx.shared import Pt

    s = screen or {}
    r = s.get("result") or {}
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Pt(54)
        section.left_margin = section.right_margin = Pt(72)

    def head(text, size=13, after=4):
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.bold = True
        run.font.size = Pt(size)
        p.paragraph_format.space_after = Pt(after)
        return p

    role = str(s.get("role") or "the role").strip()
    company = str(s.get("company") or "").strip()
    head("Interview practice report", size=16, after=2)
    sub = doc.add_paragraph()
    sub.add_run(role + (f" at {company}" if company else "")).font.size = Pt(11)
    sub.paragraph_format.space_after = Pt(10)

    score = r.get("score", 0)
    passed = bool(r.get("passed"))
    threshold = r.get("threshold", 70)
    head(f"Score: {score} out of 100. "
         f"{'Passed the practice bar' if passed else 'Below the practice bar'} "
         f"(threshold {threshold}).")
    if r.get("why"):
        doc.add_paragraph(str(r["why"]))

    comps = r.get("competencies") or []
    if comps:
        head("By competency (weakest first)")
        for cb in comps:
            doc.add_paragraph(f"{cb.get('name', '')}: {cb.get('score', 0)} out of 100",
                              style="List Bullet")

    if r.get("improvements"):
        head("Do this next")
        for x in r["improvements"]:
            doc.add_paragraph(str(x), style="List Bullet")

    graded = [a for a in (s.get("answers") or []) if a.get("per_answer_feedback")]
    if graded:
        head("Question by question")
        for i, a in enumerate(graded, 1):
            fb = a.get("per_answer_feedback") or {}
            dm = a.get("delivery_metrics") or {}
            q = doc.add_paragraph()
            q.add_run(f"{i}. {a.get('q', '')}").bold = True
            comp = str(a.get("competency") or "").strip()
            meta = f"Score {fb.get('score', 0)} out of 100" + (f", competency: {comp}" if comp else "")
            doc.add_paragraph(meta).runs[0].font.size = Pt(9.5)
            if fb.get("feedback"):
                doc.add_paragraph(str(fb["feedback"]))
            if dm:
                doc.add_paragraph(
                    f"Delivery: {dm.get('wpm', 0)} words per minute, {dm.get('words', 0)} words, "
                    f"about {dm.get('speak_sec', 0)} seconds, {dm.get('fillers', 0)} filler words."
                ).runs[0].font.size = Pt(9.5)
            if fb.get("honesty"):
                doc.add_paragraph(f"Keep it accurate: {fb['honesty']}")

    disc = doc.add_paragraph()
    disc.paragraph_format.space_before = Pt(12)
    disc.add_run(str(r.get("assessment_disclaimer")
                     or "This is practice feedback to help you improve, not a real hiring "
                        "decision.")).italic = True

    out = str(out_path)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    return out


def build_cheatsheet_docx(prep: dict, out_path: str | Path) -> str:
    """Render a one-page GAME-DAY cheat sheet from a prep: the questions to have ready (each with
    the competency it probes and a one-line how-to), plus STAR and sponsorship reminders. Something
    to review 30 minutes before the interview. Reminders to practice from, never a script to read.
    Returns the path."""
    from docx import Document
    from docx.shared import Pt

    p = prep or {}
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Pt(54)
        section.left_margin = section.right_margin = Pt(72)

    def head(text, size=13, after=4):
        h = doc.add_paragraph()
        run = h.add_run(text)
        run.bold = True
        run.font.size = Pt(size)
        h.paragraph_format.space_after = Pt(after)

    role = str(p.get("role") or "the role").strip()
    company = str(p.get("company") or "").strip()
    head("Interview cheat sheet", size=16, after=2)
    sub = doc.add_paragraph()
    sub.add_run(role + (f" at {company}" if company else "")).font.size = Pt(11)
    sub.paragraph_format.space_after = Pt(10)

    head("Remember")
    doc.add_paragraph("STAR: Situation, Task, Action, Result. Lead with the result, name what YOU "
                      "did, and keep it under 90 seconds.")
    doc.add_paragraph("Speak with confidence. Cut hedging like 'I think' or 'maybe' and state it "
                      "directly. Say 'I' when it was your work, not 'we'.")

    head("Questions to have ready")
    for q in (p.get("questions") or []):
        if not str(q.get("q", "")).strip():
            continue
        item = doc.add_paragraph(style="List Number")
        run = item.add_run(str(q.get("q", "")))
        run.bold = True
        comp = str(q.get("competency") or "").strip()
        if comp:
            item.add_run(f"  ({comp})").italic = True
        if str(q.get("why", "")).strip():
            note = doc.add_paragraph(str(q["why"]))
            note.paragraph_format.left_indent = Pt(18)
            note.runs[0].font.size = Pt(9.5)

    head("The sponsorship question")
    doc.add_paragraph("State your work authorization plainly, address any future need honestly, then "
                      "pivot straight to the value you bring. A confident pivot beats over-explaining.")

    foot = doc.add_paragraph()
    foot.paragraph_format.space_before = Pt(12)
    foot.add_run("Reminders to practice from, not a script to read. Review, then speak in your own "
                 "words.").italic = True

    out = str(out_path)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    return out


def profile_to_plaintext(profile: dict) -> str:
    """What a naive résumé parser reads back from the profile — headings, lines, and
    bullets as plain text, in document order. Backs a 'how a parser sees your CV' preview
    so the person can sanity-check that nothing important got dropped or scrambled."""
    p = normalize_profile(profile or {})
    ident = p.get("identity", {}) if isinstance(p.get("identity"), dict) else {}
    lines: list[str] = []
    if ident.get("name"):
        lines.append(str(ident["name"]).strip())
    contact = _contact_line(ident)
    if contact:
        lines.append(contact.replace("  |  ", " | "))

    for sec in (section_order := _section_order(p)) + [s for s in _HEADINGS
                                                       if s not in _section_order(p)]:
        title = _HEADINGS.get(sec, sec.title())
        if sec == "summary" and str(p.get("summary") or "").strip():
            lines += ["", title.upper(), str(p["summary"]).strip()]
        elif sec == "skills" and (p.get("skills") or {}):
            lines += ["", title.upper()]
            for label, value in (p.get("skills") or {}).items():
                lines.append(f"{label}: {value}")
        elif sec == "education" and (p.get("education") or []):
            lines += ["", title.upper()]
            for b in p["education"]:
                lines.append(" - ".join(x for x in (b.get("school"), b.get("location")) if x))
                deg = " - ".join(x for x in (b.get("degree"), b.get("date")) if x)
                if deg:
                    lines.append(deg)
                if b.get("courses"):
                    lines.append(f"Courses: {b['courses']}")
        elif sec in ("projects", "experience") and (p.get(sec) or []):
            lines += ["", title.upper()]
            for e in p[sec]:
                lines.append(" - ".join(x for x in (e.get("org"), e.get("location")) if x))
                for r in _roles_of(e):
                    hdr = " - ".join(x for x in (r.get("title"), r.get("dates")) if x)
                    if hdr:
                        lines.append(hdr)
                    lines += [f"• {b}" for b in _as_bullets(r.get("bullets"))]
        elif sec == "extracurricular" and (p.get("extracurricular") or []):
            lines += ["", title.upper()]
            for it in p["extracurricular"]:
                lines.append(" - ".join(x for x in (it.get("title"), it.get("date")) if x))
                lines += [f"• {b}" for b in _as_bullets(it.get("bullets"))]
        elif sec == "interests" and p.get("interests"):
            lines += ["", title.upper(), f"Interests: {p['interests']}"]
    return "\n".join(lines).strip() + "\n"
