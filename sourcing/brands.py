"""The name a job card shows, kept apart from the name the sponsor badge matches on.

The watchlist's ``company`` is the employer's LEGAL filing name ("Payward Inc", "Agilebits Usa
Inc") on purpose: that is the name the government's H-1B and PERM records use, so the sponsor
overlay (sourcing/sponsors.py) matches it. People know the brand ("Kraken", "1Password").
So a watched board may carry a ``display_name`` (seed/watchlist_seed.csv), and every other
legal name is tidied for display by ``brand_name``: corporate suffixes dropped, small words in
lower case. Sponsor matching, dedup and every stored row keep the legal name.
"""

from __future__ import annotations

import re

# Trailing words that are part of a legal filing name, not of the brand.
_SUFFIXES = frozenset("""
inc incorporated llc l.l.c corp corporation co company ltd limited lp llp plc pbc pc gmbh
""".split())
# Words written in lower case inside a name ("University of Chicago", "Texas A&M at Austin").
_SMALL = frozenset("of at and for in on the to via".split())


def brand_name(company: str) -> str:
    """A legal filing name tidied for display: "Datadog Inc" -> "Datadog", "The University Of
    Chicago" -> "The University of Chicago", "Red Hat, Inc." -> "Red Hat". A name that is all
    suffix ("Company") is left as it is."""
    name = re.sub(r"\s+", " ", str(company or "")).strip()
    if not name:
        return ""
    words = name.replace(",", " ").split()
    while len(words) > 1 and words[-1].lower().strip(".") in _SUFFIXES:
        words.pop()
    out = [w if i == 0 or w.lower() not in _SMALL else w.lower() for i, w in enumerate(words)]
    return " ".join(out).strip(" .,") or name


def display_name(company: str, explicit: str = "") -> str:
    """The brand to show: the seed's explicit ``display_name`` when there is one, else the
    tidied legal name."""
    explicit = str(explicit or "").strip()
    return explicit or brand_name(company)
