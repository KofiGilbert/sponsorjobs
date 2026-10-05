"""Live demo runner: tailor the config template to a real JD via the real LLM.

Usage:
    python run_live.py path/to/jd.txt [--model claude-opus-4-8]

Builds a PROFILE from the résumé content already in config/resume_shetty.tex
(the real material to tailor), runs the full tailoring engine against the JD
using the bring-your-own-key Anthropic backend, prints the coverage report and
a before/after of every changed bullet, and opens the resulting PDF.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from llm.anthropic_client import AnthropicLLM
from tailoring.latex_template import LatexTemplate
from tailoring.tailor import tailor_resume

TEMPLATE = ROOT / "config" / "resume_shetty.tex"


def latex_to_text(src: str) -> str:
    """Crudely strip LaTeX so the résumé's real words are searchable as PROFILE."""
    lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("%")]
    text = "\n".join(lines)
    # \href{url}{label} -> label
    text = re.sub(r"\\href\{[^}]*\}\{([^}]*)\}", r"\1", text)
    # \textbf{x}, \textsc{x}, \large x -> inner text
    text = re.sub(r"\\(?:textbf|textsc|emph|underline)\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+\*?", " ", text)   # drop remaining commands
    text = text.replace("{", " ").replace("}", " ")
    return re.sub(r"[ \t]+", " ", text)


def build_profile_from_template(template_source: str) -> dict:
    """Assemble a PROFILE dict from the résumé's own content."""
    tmpl = LatexTemplate(template_source)
    bullets = [b.text for b in tmpl.bullets]
    return {
        "resume_bullets": bullets,
        "resume_text": latex_to_text(template_source),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jd_file", help="path to a text file containing the job description")
    ap.add_argument("--model", default=None, help="override the Anthropic model id")
    ap.add_argument("--workdir", default=str(ROOT / "data" / "live_build"))
    args = ap.parse_args()

    jd_text = Path(args.jd_file).read_text(encoding="utf-8").strip()
    if not jd_text:
        print("ERROR: job description file is empty.", file=sys.stderr)
        return 2

    template_source = TEMPLATE.read_text(encoding="utf-8")
    profile = build_profile_from_template(template_source)

    print(f"Model: {args.model or os.environ.get('RESUME_AGENT_MODEL', 'claude-opus-4-8')}")
    print(f"JD length: {len(jd_text)} chars\n")
    print("Tailoring with the real Anthropic model — this makes one call per "
          "bullet, please wait...\n")

    llm = AnthropicLLM(model=args.model)
    tmpl = LatexTemplate(template_source)
    result = tailor_resume(template_source, profile, jd_text, llm, args.workdir)

    print("=" * 70)
    print(result.summary())
    print("=" * 70)

    if result.edits:
        print("\n--- Before / after (changed bullets) ---")
        for bid, new_text in result.edits.items():
            original = tmpl.get(bid).text
            print(f"\n[{bid}]  ({tmpl.get(bid).section})")
            print(f"  before: {original}")
            print(f"  after : {new_text}")

    if result.pdf_path and result.pdf_path.exists():
        pdf = result.pdf_path.resolve()
        print(f"\nPDF written: {pdf}")
        try:
            os.startfile(str(pdf))  # opens in the default viewer on Windows
            print("(opened in your default PDF viewer)")
        except Exception as exc:  # pragma: no cover
            print(f"(could not auto-open: {exc})")
    else:
        print("\nNo PDF was produced (rolled back).")

    # Persist the tailored .tex next to the PDF for inspection.
    tex_out = Path(args.workdir) / "resume_tailored.tex"
    tex_out.write_text(result.tex_source, encoding="utf-8")
    print(f"Tailored .tex: {tex_out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
