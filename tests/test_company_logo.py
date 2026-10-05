"""Company-logo domain guessing (2026-08-09).

The board shows a real logo when we can guess the employer's domain, and a clean monogram
otherwise. A WRONG logo is worse than a monogram (a foreign brand on the wrong row), so the
guesser only strips known legal/entity suffixes and never invents a brand. These pin that:
legal suffixes (incl. the newer "PBC") drop, schools resolve to .edu with the academic tail
removed, and companies resolve to .com.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def dom(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A._guess_company_domain


def test_strips_legal_suffixes(dom):
    assert dom("Waymo Llc") == "waymo.com"
    assert dom("Taboola Inc") == "taboola.com"
    assert dom("Insight Enterprises, Inc.") == "insightenterprises.com"


def test_strips_pbc_public_benefit_corp(dom):
    # The suffix that used to break: "Anthropic Pbc" -> anthropicpbc.com (a dead domain).
    assert dom("Anthropic Pbc") == "anthropic.com"


def test_schools_resolve_to_edu_without_the_academic_tail(dom):
    assert dom("DePaul University") == "depaul.edu"
    assert dom("Northwestern College") == "northwestern.edu"


def test_blank_company_is_no_domain(dom):
    assert dom("") == ""
    assert dom("   ") == ""
