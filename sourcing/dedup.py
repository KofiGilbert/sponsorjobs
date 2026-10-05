"""Cross-feed job dedup: the same posting reached through two different doors.

A watched Greenhouse board and a remote-jobs aggregator will both hand back the same
opening, with different ids, different URL spellings, and sometimes a differently
punctuated title. ``Watchlist.upsert_jobs`` already dedups by ``source_id`` within a
feed; this catches the SAME job across feeds so the person is not shown one opening
three times and does not tailor a CV for it twice.

Two keys, both deliberately UNDER-normalized. The failure modes are asymmetric: merging
two genuinely different postings is a silent loss the person never sees, while leaving
two spellings of one posting apart is a visible duplicate they can dismiss. So the URL
key strips only known tracking parameters, never functional ones, and the role key never
splits a location on a comma ("Chicago, IL" is one place).

Keys adapted from career-ops (MIT, ``url-key.mjs`` and the dedup helpers in
``scan.mjs``), rewritten for our job dict shape. See NOTICES.md.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that carry no identity: analytics and referral tags only. A generic
# "ref" / "source" / "id" is NOT here on purpose; on some boards those select the job.
_TRACKING = re.compile(
    r"^(utm_.*|gh_src|lever-(origin|source)|fbclid|gclid|msclkid|mc_[ce]id|_hs(enc|mi)"
    r"|igshid|ref_src|src)$", re.I)


def url_key(url: str) -> str:
    """A canonical key for a posting URL, or "" when there is no usable URL.

    Lowercases the host, drops the fragment and a trailing slash, removes tracking
    parameters and sorts the rest. "" is returned for anything non-http rather than a
    lowercased stand-in: no key must never accidentally equal another no key.
    """
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", parts.path or "") or "/"
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not _TRACKING.match(k))
    return urlunsplit(("https", host, path, urlencode(query), ""))


_TRAILING_LOCATION = re.compile(
    r"\s*[\(\[（]\s*(remote|hybrid|on-?site|[a-z .,'-]+)\s*[\)\]）]\s*$", re.I)
_SEPARATORS = re.compile(r"\s*(?:;|\||·|/|\bor\b)\s*", re.I)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#. ]+", " ", text)).strip()


def role_key(company: str, title: str, location: str = "") -> str:
    """``company::title@@places`` for the same opening listed under different ids.

    Title loses a trailing bracketed location ("Engineer (Remote)"), because boards add
    those inconsistently. Locations split on ";", "|", "·", "/" and "or", never on a
    comma, and are sorted so a board reordering its city list is still one posting.
    """
    company_k, title_k = _fold(company), _fold(_TRAILING_LOCATION.sub("", str(title or "")))
    if not (company_k and title_k):
        return ""
    places = sorted({_fold(p) for p in _SEPARATORS.split(str(location or "")) if _fold(p)})
    key = f"{company_k}::{title_k}"
    return f"{key}@@{'|'.join(places)}" if places else key


class DedupIndex:
    """Keys of every posting already known, mapped to the source_id that owns them.

    A job is a duplicate when one of its keys is owned by a DIFFERENT source_id. The same
    source_id re-appearing is a refresh of a known posting, never a duplicate.
    """

    def __init__(self, existing=()) -> None:
        self._owner: dict[str, str] = {}
        for job in existing or ():
            self.add(job)

    @staticmethod
    def keys_for(job: dict) -> list[str]:
        return [k for k in (url_key(job.get("url", "")),
                            role_key(job.get("company", ""), job.get("title", ""),
                                     job.get("location", "")))
                if k]

    def add(self, job: dict) -> None:
        sid = str(job.get("source_id") or "")
        for k in self.keys_for(job):
            self._owner.setdefault(k, sid)

    def is_duplicate(self, job: dict) -> bool:
        sid = str(job.get("source_id") or "")
        return any(self._owner.get(k) not in (None, sid) for k in self.keys_for(job))

    def filter(self, jobs: list[dict]) -> tuple[list[dict], int]:
        """Keep the first sighting of each posting; returns (kept, dropped_count)."""
        kept: list[dict] = []
        dropped = 0
        for job in jobs:
            if self.is_duplicate(job):
                dropped += 1
                continue
            self.add(job)
            kept.append(job)
        return kept, dropped
