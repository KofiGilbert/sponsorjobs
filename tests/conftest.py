"""Shared fixtures for the Phase-1 acceptance tests.

Everything here is offline: a sample TEMPLATE (the repo's config file), a saved
PROFILE, sample JDs, and the deterministic FakeLLM. No job board, browser, or
network is involved (CLAUDE.md §12).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

# Make the project packages importable when pytest runs from the repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm.base import FakeLLM  # noqa: E402
from tailoring.compiler import find_pdflatex  # noqa: E402

TEMPLATE_PATH = ROOT / "config" / "resume_shetty.tex"

# The offline suite keeps MemPalace's durable verbatim layer but skips semantic
# indexing/recall by default, so it stays fast and needs no embedding model.
# Tests that exercise real recall set RESUME_AGENT_PALACE_INDEX=1 themselves.
os.environ.setdefault("RESUME_AGENT_PALACE_INDEX", "0")


def _palace_available() -> bool:
    try:
        import mempalace  # noqa: F401
        return True
    except Exception:
        return False


# Tests that exercise MemPalace semantic recall are skipped (not failed) where the
# package/embedder isn't available, mirroring `requires_latex`.
requires_palace = pytest.mark.skipif(
    not _palace_available(),
    reason="mempalace not installed (semantic recall layer unavailable)",
)


def _ocr_available() -> bool:
    try:
        from intake.cv_import import ocr_available
        return ocr_available()
    except Exception:
        return False


# OCR tests are skipped (not failed) where RapidOCR isn't importable.
requires_ocr = pytest.mark.skipif(
    not _ocr_available(),
    reason="rapidocr-onnxruntime not available (OCR layer)",
)


def _pdflatex_available() -> bool:
    try:
        find_pdflatex()
        return True
    except FileNotFoundError:
        return False


# Tests that actually compile LaTeX are skipped (not failed) if no engine exists,
# so the pure-logic acceptance tests still run anywhere.
requires_latex = pytest.mark.skipif(
    not _pdflatex_available(),
    reason="no pdflatex/MiKTeX toolchain available",
)


@pytest.fixture
def template_source() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def workdir(tmp_path) -> Path:
    d = tmp_path / "build"
    d.mkdir()
    return d


@pytest.fixture
def sample_profile() -> dict:
    """A saved PROFILE. Content mirrors the kind of material a person supplies;
    it deliberately supports some JD terms (Python, SQL, risk management,
    machine learning) and not others (Kubernetes, Kafka)."""
    return {
        "name": "Jonathan Dobson",
        "contact": {
            "email": "jonathandobson@example.com",
            "phone": "(312) 555-0187",
            "location": "Chicago, IL",
        },
        "experience": [
            {
                "company": "Torbeck Capital",
                "role": "Quantitative Risk Associate",
                "dates": "Feb 2023 - Present",
                "highlights": [
                    "Built portfolio risk and stress-testing models in Python.",
                    "Backtested model performance on historical market moves.",
                    "Led validation of model changes and risk-framework rollout.",
                ],
            },
            {
                "company": "Zenith",
                "role": "Senior Software Developer",
                "dates": "July 2018 - July 2021",
                "highlights": [
                    "Built an NLP engine using machine learning in Python.",
                    "Built REST API integrations and cloud infrastructure on AWS.",
                    "Used SQL and data pipelines for reporting.",
                ],
            },
        ],
        "skills": [
            "Python", "C++", "SQL", "AWS", "risk management",
            "machine learning", "statistical modeling", "REST API",
        ],
    }


@pytest.fixture
def jd_quant() -> str:
    """A quant-risk JD whose terms are mostly supported by the profile."""
    return (
        "Quantitative Risk Analyst. We are looking for a candidate strong in "
        "Python and SQL to build risk management models. Experience with "
        "machine learning, statistical modeling, and backtesting is required. "
        "Familiarity with AWS and REST API design is a plus. You will work on "
        "market risk and portfolio theory for derivatives."
    )


@pytest.fixture
def jd_swe() -> str:
    """A distinct software-engineering JD, some terms unsupported by the profile
    (Kubernetes, Kafka) so the coverage report has real gaps."""
    return (
        "Senior Software Engineer. Build scalable REST API services in Python. "
        "Must have experience with AWS, Kubernetes, and Kafka. Strong SQL and "
        "cloud infrastructure skills required. Machine learning exposure a plus."
    )


@pytest.fixture(autouse=True)
def _no_first_open_crawl(monkeypatch):
    """The Jobs route starts a real background crawl of 1,100 boards when the local store is
    empty (ui.app._kick_local_crawl). Under test that would hit the network from every test
    that touches /api/jobs. An env switch is used rather than a monkeypatched function because
    several fixtures reload ui.app, which would discard a patched attribute. The bundled H-1B
    seed loads SYNCHRONOUSLY here (JOBS_SYNC_SEED=1): the app merges it on a background thread,
    but tests that open a fresh sponsor DB and read its counts straight away need it in place;
    the one test of the background path unsets this itself (tests/test_h1b_data.py).
    NOTIFY_OFFICIAL=0 keeps the notification hub off the broker (the official Telegram bot);
    tests that exercise it inject fakes and set it back themselves."""
    monkeypatch.setenv("JOBS_FIRST_CRAWL", "0")
    monkeypatch.setenv("NOTIFY_OFFICIAL", "0")
    monkeypatch.setenv("JOBS_SYNC_SEED", "1")
