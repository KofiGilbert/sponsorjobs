"""Local command-line front door for the resume agent (CLAUDE.md §4, §11).

Reads a job description, runs the JD-driven intake conversation, and produces a
tailored one-page CV — saving your answers locally so you only answer once.

Usage:
    python intake_cli.py                       # paste the JD when prompted
    python intake_cli.py --jd path/to/jd.txt   # read the JD from a file
    python intake_cli.py --model claude-sonnet-5

Everything runs locally; your profile is stored in a local SQLite db and your
API key is read from config/credentials.env (git-ignored).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from intake.conversation import ConsoleIO, run_intake
from intake.profile_store import ProfileStore
from llm.anthropic_client import AnthropicLLM

TEMPLATE = ROOT / "config" / "resume_shetty.tex"


def _read_jd(io: ConsoleIO, jd_path: str | None) -> str:
    if jd_path:
        return Path(jd_path).read_text(encoding="utf-8").strip()
    io.say("Paste the job description, then a line containing only 'END':")
    lines: list[str] = []
    while True:
        line = io.ask("")
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jd", default=None, help="path to a JD text file")
    ap.add_argument("--model", default=None, help="Anthropic model id override")
    ap.add_argument("--profile", default="default", help="named saved profile")
    ap.add_argument("--db", default=str(ROOT / "data" / "resume_agent.db"))
    ap.add_argument("--workdir", default=str(ROOT / "data" / "cv_build"))
    args = ap.parse_args()

    io = ConsoleIO()
    io.say("=== Resume Agent — local intake ===\n")
    jd_text = _read_jd(io, args.jd)
    if not jd_text:
        io.say("No job description provided. Exiting.")
        return 2

    template_source = TEMPLATE.read_text(encoding="utf-8")
    Path(args.workdir).mkdir(parents=True, exist_ok=True)

    try:
        llm = AnthropicLLM(model=args.model)
    except Exception as exc:
        io.say(f"\nCould not initialize the LLM: {exc}")
        io.say("Set ANTHROPIC_API_KEY in config/credentials.env and retry.")
        return 1

    with ProfileStore(args.db) as store:
        result = run_intake(
            io, jd_text, template_source, llm, store, args.workdir,
            profile_name=args.profile,
        )

    io.say("\n" + "=" * 60)
    io.say(result.assembled.summary())
    io.say("=" * 60)
    if result.pdf_path and result.pdf_path.exists():
        pdf = result.pdf_path.resolve()
        io.say(f"\nYour CV: {pdf}")
        try:
            os.startfile(str(pdf))  # noqa: S606 - open in default viewer (Windows)
        except Exception:
            pass
    else:
        io.say("\nNo one-page CV was produced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
