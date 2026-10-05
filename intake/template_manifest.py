"""Per-template interview config (CLAUDE.md §4a, §13).

A template is a fixed set of slots. Each template ships a manifest — its slot
schema plus a bank of warm, leading questions and interview-style guidance the LLM
draws on to converse naturally (never a script). This is PRODUCT config that lives
with the template; the person's own answers live in MemPalace, not here.

One template today ('shetty'); the loader takes a name so future templates each
ship their own manifest and the picker just selects one.
"""

from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent.parent / "config" / "templates"
_DEFAULT = "shetty"


def load_manifest(name: str | None = None) -> dict:
    """Load a template manifest by name; returns {} if none is found (callers fall
    back to sensible defaults so a missing manifest never breaks the interview)."""
    p = _DIR / f"{(name or _DEFAULT)}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def elicit_leads(manifest: dict, key: str) -> list[str]:
    """Example leading questions for a section (e.g. 'extracurricular')."""
    return list(((manifest.get("elicit") or {}).get(key) or {}).get("leads") or [])


def elicit_goal(manifest: dict, key: str) -> str:
    """What the section is for — guides the LLM on what to draw out."""
    return str(((manifest.get("elicit") or {}).get(key) or {}).get("goal") or "").strip()


def interview_style(manifest: dict) -> str:
    return str(manifest.get("interview_style") or "").strip()


# The section render order for a template's body (assembler draws sections from here so a
# template's shape — e.g. summary-first, no extracurricular — is data, not hardcoded).
_DEFAULT_SECTIONS = ("education", "skills", "projects", "experience", "extracurricular", "interests")
# Sections a CV of this template MUST have before it's built — the person provides them (or
# explicitly declines), else we can't build to the template's standard. Legacy manifests
# without the key fall back to the two page-filling sections that were required before.
_DEFAULT_REQUIRED = ("extracurricular", "interests")


def sections(manifest: dict) -> list[str]:
    """Ordered body sections the selected template renders."""
    secs = manifest.get("sections")
    return list(secs) if isinstance(secs, list) and secs else list(_DEFAULT_SECTIONS)


def required_sections(manifest: dict) -> list[str]:
    """Sections the template REQUIRES the person to provide (or decline) before building."""
    req = manifest.get("required_sections")
    if isinstance(req, list):
        return list(req)                       # may be [] — a template with no required extras
    return list(_DEFAULT_REQUIRED) if manifest else []


_DEFAULT_IDENTITY = ("name", "address", "phone", "email", "linkedin", "github", "blog")


def identity_fields(manifest: dict) -> list[str]:
    """The contact/identity slots this template shows — so intake only asks for the links a
    template actually has (e.g. the summary template has no blog slot, so it never asks)."""
    fields = manifest.get("identity_fields")
    return list(fields) if isinstance(fields, list) and fields else list(_DEFAULT_IDENTITY)
