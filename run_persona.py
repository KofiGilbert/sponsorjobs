"""Assemble a full CV from a real PROFILE into the template's shape, vs a JD.

Usage:
    python run_persona.py data/sample_profile.json data/jd_ai_architect.txt \
        [--model claude-sonnet-5]

Pours the person's own content into the template's slots (reusing its exact
preamble/fonts/colours/links/structure), tailors bullet language to the JD, fits
to one page, and writes both PDFs: the ORIGINAL template (the empty shape / clay)
and the FILLED CV.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from llm.anthropic_client import AnthropicLLM
from tailoring.assembler import assemble_cv
from tailoring.compiler import compile_tex

TEMPLATE = ROOT / "config" / "resume_shetty.tex"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile_json")
    ap.add_argument("jd_file")
    ap.add_argument("--model", default=None)
    ap.add_argument("--workdir", default=str(ROOT / "data" / "cv_build"))
    ap.add_argument("--no-tailor", action="store_true",
                    help="fill slots verbatim without LLM rewording")
    args = ap.parse_args()

    profile = json.loads(Path(args.profile_json).read_text(encoding="utf-8"))
    jd_text = Path(args.jd_file).read_text(encoding="utf-8").strip()
    template_source = TEMPLATE.read_text(encoding="utf-8")
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    # BEFORE: render the original template (the empty shape / placeholder clay).
    before = compile_tex(template_source, workdir, jobname="before_template")
    before_pdf = workdir / "before_template.pdf"
    print(f"BEFORE (template shape): pages={before.pages}, pdf={before_pdf.resolve()}")

    model = args.model or os.environ.get("RESUME_AGENT_MODEL", "claude-opus-4-8")
    print(f"\nModel: {model}")
    person = profile.get("identity", {}).get("name", "?")
    print(f"Assembling CV for: {person}")
    if not args.no_tailor:
        print("Tailoring bullet language to the JD (one LLM call per bullet)...\n")

    llm = None if args.no_tailor else AnthropicLLM(model=args.model)
    # A no-op LLM for the --no-tailor path.
    if llm is None:
        class _Verbatim:
            def generate_intake_questions(self, *a): return []
            def reword_bullet(self, original_text, *a, **k): return original_text
            def shorten_bullet(self, text, max_len_chars): return text[:max_len_chars]
        llm = _Verbatim()

    result = assemble_cv(
        template_source, profile, jd_text, llm, workdir,
        tailor=not args.no_tailor,
    )

    print("=" * 70)
    print(result.summary())
    print("=" * 70)

    after_tex = workdir / "after_cv.tex"
    after_tex.write_text(result.tex_source, encoding="utf-8")
    if result.pdf_path and result.pdf_path.exists():
        after_pdf = result.pdf_path.resolve()
        print(f"\nAFTER (filled CV): {after_pdf}")
        try:
            os.startfile(str(after_pdf))
        except Exception as exc:  # pragma: no cover
            print(f"(could not auto-open: {exc})")
    else:
        print("\nNo CV PDF produced.")
    print(f"Filled CV .tex: {after_tex.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
