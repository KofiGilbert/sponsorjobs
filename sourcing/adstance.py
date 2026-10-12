"""What a job ad SAYS about visa sponsorship, read from its own text.

Built on a survey of 1,196 real descriptions in the live feed (2026-10-08). Findings that shape
this module:

* Ads that offer sponsorship almost never name the visa. They say "Visa sponsorship is
  available", "This role is eligible for visa sponsorship", "We do sponsor visas!". So the honest
  thing to show is the ad's own stance, never an inferred visa type.
* About a THIRD of ads that mention sponsorship say they will NOT sponsor: "Fidelity will not
  provide immigration sponsorship for this position", "Do not apply for this role if you will need
  GM immigration sponsorship now or in the future", "must be authorized to work without requiring
  current or future visa sponsorship". For an international student that is the most useful fact
  on the page, and a naive keyword match counts these as POSITIVE.
* The aggregator's yes/no `sponsorship_stated` flag was wrong for most flagged ads (69% had no
  sponsorship sentence at all), so the ad is read here, by us.

The result is one of four stances plus the exact sentence it came from, so the UI can show the
source on hover. Deterministic and offline: it runs over thousands of ads in the feed build.

The fourth stance, RESTRICTED (2026-10-11), is an ad that requires US citizenship, a green card /
permanent residency, or a security clearance. Such a role is closed to a visa holder whatever the
employer's sponsorship record says, and it gets its own chip ("Citizenship or clearance required")
rather than being lumped in with "no sponsorship". The phrasing rules are the ones
sourcing/quality.py already uses to keep such roles off the board (its _EXCLUDE_RE is built from
RESTRICTED_SRC below plus the no-sponsorship phrasings), so the feed, the app and the extension
never disagree about the same ad.
"""
from __future__ import annotations

import re

OFFERED = "offered"        # the ad says sponsorship is available / the role is eligible
NOT_OFFERED = "not_offered"  # the ad says it will not sponsor / requires authorization without it
RESTRICTED = "restricted"  # the ad requires US citizenship / a green card / a security clearance
UNKNOWN = "unknown"        # the ad says nothing usable

# How much of the deciding sentence a chip shows (its tooltip and the grey line under the chips).
QUOTE_CHARS = 140

# Sentences are split on terminal punctuation and newlines (bullets). A full stop after a lone
# letter is an abbreviation ("U.S. citizenship", "e.g."), not a sentence end: splitting there
# turned "U.S. citizenship is required" into "U", "S", "citizenship is required", and the
# citizenship rule never saw the "US".
_SENT = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]\.|[^.\n!?•])+")
# Ways an ad closes the role to anyone who is not a US citizen / permanent resident / cleared.
# Shared VERBATIM with sourcing/quality.py (its _EXCLUDE_RE is this plus the no-sponsorship
# phrasings), so the board's filter and the chips read every ad the same way. Every branch needs
# an explicit bar: "we sponsor green cards" and "H-1B and green-card support offered" are
# positive sentences and must never match.
RESTRICTED_SRC = (
    r"u\.?s\.?\s*citizens?(hip)?\s*(is\s*)?(required|only|mandatory)"
    r"|must\s+be\s+(a\s+|an\s+)?(u\.?s\.?\s*)?(citizen|green\s*card\s*holder|(lawful\s+)?permanent\s+resident)"
    r"|sole\s+u\.?s\.?\s*citizen"
    r"|(citizens?|citizenship)\s*(,|/|\s+or\s+|\s+and\s+)\s*(a\s+)?"
    r"(green\s*card(\s*holders?)?|(lawful\s+)?permanent\s+residen(ts?|cy))"
    r"|(green\s*card(\s*holders?)?|(lawful\s+)?permanent\s+residen(ts?|cy))\s*(is\s+|are\s+)?"
    r"(required|only|mandatory)"
    r"|must\s+(hold|have|possess)\s+(a\s+|an\s+)?(valid\s+|current\s+)?"
    r"(green\s*card|(lawful\s+)?permanent\s+residen(t|cy))"
    r"|security\s+clearance|active\s+clearance|obtain\s+(a\s+)?(security\s+)?clearance"
    r"|ts/sci|top\s+secret|secret\s+clearance|dod\s+secret|polygraph|\bq\s+clearance\b"
)
RESTRICTED_RE = re.compile("(" + RESTRICTED_SRC + ")", re.I)
# A sentence is about immigration sponsorship when it carries the word and some context; this
# keeps out "Arcesium sponsored marketing initiatives" and "sponsor a hackathon".
_ABOUT = re.compile(r"\bsponsor\w*|\bh-?1b\b", re.I)
_CTX = re.compile(r"visa|immigra|work authori[sz]|h-?1b|\bopt\b|\bcpt\b|green card|employment "
                  r"eligib|right to work|lawful|employer of record|work permit|legally authori[sz]",
                  re.I)
# Ways an ad says NO. Checked before the positives: "Do not apply if you will need sponsorship" and
# "without requiring current or future visa sponsorship" both contain positive-looking words.
_NO = (
    re.compile(r"\b(not|never|unable|cannot|can ?not|can't|won't|will not|does ?not|doesn't|do ?not|"
               r"don't|no longer|isn't|is not|are not|aren't)\b[^.\n]{0,60}?\b(offer|provid|sponsor|support|"
               r"able|eligible|available|consider)", re.I),
    re.compile(r"without\s+(requiring\s+|the\s+need\s+for\s+|needing\s+)?(any\s+)?(current\s+or\s+future\s+)?"
               r"(employer[- ])?(visa\s+|immigration\s+)?sponsorship", re.I),
    re.compile(r"sponsorship\s+(is\s+)?(not|un)\s*(available|offered|provided|possible)", re.I),
    re.compile(r"\bno\s+(visa\s+|immigration\s+|employment[- ]based\s+)?sponsorship", re.I),
    re.compile(r"do\s+not\s+apply[^.\n]{0,80}sponsor", re.I),
    re.compile(r"(must|should)\s+(be|have|possess|hold)[^.\n]{0,80}(authori[sz]ation|authori[sz]ed|eligib)"
               r"[^.\n]{0,80}(not|without)[^.\n]{0,40}sponsor", re.I),
    re.compile(r"(will|would)\s+(now\s+or\s+in\s+the\s+future\s+)?(require|need)\s+(visa\s+|immigration\s+)?"
               r"sponsorship[^.\n]{0,40}(not|ineligible|unable|cannot)", re.I),
)
# Ways an ad says YES.
_YES = (
    re.compile(r"sponsorship\s+(is\s+|may\s+be\s+|will\s+be\s+)?(available|offered|provided|possible|considered)", re.I),
    re.compile(r"(eligible|open)\s+for\s+(visa\s+|immigration\s+|work\s+)?sponsorship", re.I),
    re.compile(r"\b(we|company|employer)\s+(do|does|will|can|may|are\s+able\s+to|are\s+willing\s+to|"
               r"are\s+happy\s+to)\s+(offer|provide|sponsor|support)", re.I),
    re.compile(r"(offer|offers|offering|provide|provides|providing)\s+(visa\s+|immigration\s+|h-?1b\s+|work\s+)?"
               r"(visa\s+)?sponsorship", re.I),
    re.compile(r"sponsorship\s+(for|of)\s+(work\s+)?visas?", re.I),
    re.compile(r"\bsponsor\s+(work\s+)?visas?\b", re.I),
    re.compile(r"(will|can|may)\s+sponsor\b", re.I),
    re.compile(r"h-?1b\s+(transfer|sponsorship)\s+(welcome|accepted|available|ok)", re.I),
)


def _sentences(text: str):
    for m in _SENT.finditer(text or ""):
        s = " ".join(m.group(0).split())
        if s:
            yield s


def ad_stance(text: str) -> dict:
    """{"stance": RESTRICTED|NOT_OFFERED|OFFERED|UNKNOWN, "sentence": str}.

    A bar anywhere outranks a YES anywhere: "We do sponsor visas, but not for this role" is a no
    for this role, and an ad that both welcomes applicants and then says "without requiring
    sponsorship" is a no. A citizenship / green-card / clearance requirement outranks a plain
    "no sponsorship": it is the more specific fact, and the role is closed either way. The
    sentence returned is the first one that decided the winning stance.
    """
    no_sent = yes_sent = None
    for s in _sentences(text):
        if RESTRICTED_RE.search(s):
            return {"stance": RESTRICTED, "sentence": s[:300]}
        if no_sent is not None or not (_ABOUT.search(s) and _CTX.search(s)):
            continue
        if any(p.search(s) for p in _NO):
            no_sent = s[:300]
        elif yes_sent is None and any(p.search(s) for p in _YES):
            yes_sent = s[:300]
    if no_sent:
        return {"stance": NOT_OFFERED, "sentence": no_sent}
    if yes_sent:
        return {"stance": OFFERED, "sentence": yes_sent}
    return {"stance": UNKNOWN, "sentence": ""}


def short_sentence(sentence: str, limit: int = QUOTE_CHARS) -> str:
    """The deciding sentence cut to about `limit` characters at a word boundary, for a chip's
    tooltip and the grey line under the chips. Ported in extension/adstance_core.js."""
    s = " ".join((sentence or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit + 1]
    sp = cut.rfind(" ")
    cut = cut[:sp] if sp > limit // 2 else s[:limit]
    return cut.rstrip(" ,;:") + "..."
