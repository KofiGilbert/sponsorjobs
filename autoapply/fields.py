"""Data model + PII-safe resolution for Tailor's own autonomous form filler.

The privacy crux. Playwright reads a live application form into ``FormField``s; the LLM (through our
broker) proposes a ``FillAction`` plan that maps each field to either a PLACEHOLDER KEY (for personal
data, so the model never sees the real value) or a plain literal (a non-personal choice like "Yes").
We then ``resolve_plan`` against the person's real data LOCALLY, substituting personal values only at
fill time. So the person's name, email, phone, and address travel to the browser but NEVER to the
model, our own equivalent of browser-use's sensitive_data, in ~40 lines for exactly our case.

Pure and deterministic: no browser, no network, so it is fully unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# The personal-data keys the planner may reference. Real values live only on this machine and are
# substituted at fill time; the model only ever sees the KEY, never the value.
APPLICANT_KEYS: tuple[str, ...] = (
    "first_name", "last_name", "full_name", "email", "phone",
    "address", "city", "state", "postal_code", "country",
    "linkedin", "github", "website",
)


@dataclass
class FormField:
    """One interactable field on the application form, as the executor sees it."""

    ref: str                                     # stable handle the executor targets (e.g. "f3")
    label: str                                   # the human label the model reads
    type: str                                    # text|email|tel|textarea|select|radio|checkbox|file|date
    options: list[str] = field(default_factory=list)   # for select/radio/checkbox groups
    required: bool = False

    def public(self) -> dict:
        """Exactly what the MODEL is shown: structure only, never any value."""
        d: dict = {"ref": self.ref, "label": self.label, "type": self.type, "required": self.required}
        if self.options:
            d["options"] = self.options[:40]
        return d


@dataclass
class FillAction:
    """The model's proposed action for one field. Either ``source`` (a placeholder KEY, resolved to
    real PII locally) OR ``value`` (a non-personal literal the model chose) is set, never real PII."""

    ref: str
    op: str                                      # fill|select|check|upload|skip
    source: str = ""                             # a placeholder key from APPLICANT_KEYS (PII)
    value: str = ""                              # a literal: an option label, a date, a short answer


@dataclass
class ConcreteOp:
    """A resolved action the executor runs, now carrying the REAL value (joined locally, off-model)."""

    ref: str
    op: str
    text: str = ""                               # real value to type/select (may be PII)
    path: str = ""                               # file path for an upload
    is_sensitive: bool = False                   # true when text came from personal data


def applicant_from_record(record_data: dict) -> dict:
    """Build the real personal-data map from a saved application record / profile. Stays local; only
    its KEYS (APPLICANT_KEYS) are ever exposed to the model. Missing values are simply absent."""
    identity = (record_data.get("profile") or {}).get("identity") or record_data.get("identity") or {}
    name = str(identity.get("name") or "").strip()
    first, _, last = name.partition(" ")
    out = {
        "full_name": name,
        "first_name": first.strip(),
        "last_name": last.strip(),
        "email": str(identity.get("email") or "").strip(),
        "phone": str(identity.get("phone") or "").strip(),
        "address": str(identity.get("address") or "").strip(),
        "city": str(identity.get("city") or "").strip(),
        "state": str(identity.get("state") or "").strip(),
        "postal_code": str(identity.get("postal_code") or identity.get("zip") or "").strip(),
        "country": str(identity.get("country") or "").strip(),
        "linkedin": str(identity.get("linkedin") or "").strip(),
        "github": str(identity.get("github") or "").strip(),
        "website": str(identity.get("website") or identity.get("blog") or "").strip(),
    }
    return {k: v for k, v in out.items() if v}


def resolve_plan(actions: list[FillAction], applicant: dict, *, resume_path: str = "") -> list[ConcreteOp]:
    """Join the model's PII-free plan to the person's REAL data, LOCALLY. The model never saw these
    values; they are attached here, at the last moment, right before the executor types them.

    - op ``upload`` -> the résumé path (only when we have one).
    - a ``source`` key -> the real personal value looked up from ``applicant`` (marked sensitive).
    - a ``value`` literal -> used as-is (a non-personal choice the model made).
    - empty resolutions (a key we don't hold) are dropped, never filled with a guess.
    """
    ops: list[ConcreteOp] = []
    for a in actions:
        if a.op == "skip":
            continue
        if a.op == "upload":
            if resume_path:
                ops.append(ConcreteOp(a.ref, "upload", path=resume_path))
            continue
        if a.source:                             # personal-data key -> real value, resolved locally
            val = str(applicant.get(a.source, "")).strip()
            if val:
                ops.append(ConcreteOp(a.ref, a.op or "fill", text=val, is_sensitive=True))
            continue
        if str(a.value).strip():                 # a non-personal literal the model chose
            ops.append(ConcreteOp(a.ref, a.op or "fill", text=str(a.value).strip()))
    return ops
