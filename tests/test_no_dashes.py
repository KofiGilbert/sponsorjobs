"""No em/en dashes in anything the person reads (standing rule, asked for three times).

"I don't like to see dashes in your writing anywhere on the app, it gives AI wrote this
vibes."

I fixed this twice and it came back twice, because I only ever swept the source I wrote.
The copy the person actually reads has TWO sources, and the second one is the loud one:

1. String literals in our own code and templates. A sweep fixes these.
2. THE MODEL. It writes most of the chat, and config/templates/*.json handed it a style
   guide that said "Talk like a sharp, warm friend who's genuinely into their story — never
   a corporate form." We taught it the dash by example. No sweep of our source can ever
   reach what the model then writes, which is why it kept coming back.

So this guards both: our strings stay clean, AND the prompt both avoids modelling a dash
and states the rule outright.
"""

from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from pathlib import Path

import pytest

DASH = re.compile(r"[—–]")
# The same dash rendered as an HTML entity is invisible to a raw-character grep but shows up
# as an em/en dash in the browser, so the web check must catch it too (it slipped through once).
HTML_DASH = re.compile(r"&(mdash|ndash|#8211|#8212|#x201[34]);", re.I)

# Files whose STRINGS reach the person, via the UI or via a prompt.
PY_FILES = ["ui/session.py", "ui/app.py", "llm/base.py", "llm/anthropic_client.py"]
WEB_FILES = ["ui/static/app.js", "ui/templates/index.html"]
WEB_FILES += [str(p) for p in sorted(Path("ui/static/locales").glob("*.json"))]   # translated copy too

# The OTHER modules whose string literals reach the person: Telegram messages (notify/),
# the review-queue lane labels + "why assisted" reasons (submit/), and the visa badges +
# sponsor labels (sourcing/). The copy the person reads has more than one home, and every
# home is in scope — an end-to-end pass found dashes here that the two lists above never
# swept. Docstrings are excluded (they're for us, like comments); everything else counts.
COPY_MODULES = [
    "notify/batch.py", "notify/service.py", "notify/control.py",
    "submit/service.py", "submit/drivers.py", "submit/policy.py", "submit/allowlist.py",
    "sourcing/sponsors.py", "sourcing/ats.py", "sourcing/watchlist.py", "sourcing/service.py",
    "interview/transcribe.py", "backend/tavus_client.py",
]


def _dashed_strings(path: Path) -> list[str]:
    """Every string literal containing a dash. Tokenize so a dash in CODE or a COMMENT
    isn't reported: comments are for us, and only strings are ever read by anyone else."""
    src = path.read_text(encoding="utf-8")
    return [t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type == tokenize.STRING and DASH.search(t.string)]


def _dashed_copy_literals(path: Path) -> list[str]:
    """Dash-bearing string literals a person could read: every str constant EXCEPT
    docstrings. Uses ast (not tokenize) on purpose: ast normalizes f-strings into their
    literal parts, so a dash inside an f-string is caught too (tokenize.STRING misses
    f-strings on Python 3.12+, which is exactly where one slipped through)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings and DASH.search(node.value)]


@pytest.mark.parametrize("rel", PY_FILES)
def test_no_dashes_in_python_copy(rel):
    found = _dashed_strings(Path(rel))
    assert not found, f"{rel}: {len(found)} strings with a dash, e.g. {found[:2]}"


@pytest.mark.parametrize("rel", WEB_FILES)
def test_no_dashes_in_web_copy(rel):
    text = Path(rel).read_text(encoding="utf-8")
    hits = DASH.findall(text) + HTML_DASH.findall(text)
    assert not hits, f"{rel}: {len(hits)} dashes (incl. HTML entities)"


@pytest.mark.parametrize("rel", COPY_MODULES)
def test_no_dashes_in_the_other_copy_modules(rel):
    """Telegram / submit-lane / sponsor copy is read by the person too, f-strings included."""
    found = _dashed_copy_literals(Path(rel))
    assert not found, f"{rel}: {len(found)} copy strings with a dash, e.g. {found[:2]}"


@pytest.mark.parametrize("name", ["shetty", "summary"])
def test_the_prompt_does_not_teach_the_model_to_use_dashes(name):
    """The one that actually mattered. A dash in the style guide is a dash in the chat."""
    m = json.loads(Path(f"config/templates/{name}.json").read_text(encoding="utf-8"))
    style = m.get("interview_style") or ""
    assert not DASH.search(style), \
        "the voice prompt contains a dash, so the model will copy it"


@pytest.mark.parametrize("name", ["shetty", "summary"])
def test_the_prompt_bans_dashes_outright(name):
    """Not modelling a dash isn't the same as forbidding one. The model writes this copy,
    so the rule has to be stated where the model can read it, not only where we can."""
    m = json.loads(Path(f"config/templates/{name}.json").read_text(encoding="utf-8"))
    style = (m.get("interview_style") or "").lower()
    assert "em dash" in style and "never" in style, \
        "the voice prompt never tells the model not to use dashes"


def test_no_dashes_anywhere_in_a_template_manifest():
    """elicit leads and labels are read out to the person almost verbatim."""
    for p in sorted(Path("config/templates").glob("*.json")):
        blob = json.dumps(json.loads(p.read_text(encoding="utf-8")), ensure_ascii=False)
        assert not DASH.search(blob), f"{p.name} contains a dash"
