"""H-1B cap lottery odds by prevailing-wage level, and the offer check built on them.

The rule (as of October 2026). DHS's final rule "Weighted Selection Process for Registrants
and Petitioners Seeking To File Cap-Subject H-1B Petitions" (90 FR, published 2025-12-29,
Federal Register document 2025-23853, docket USCIS-2025-0040; effective 2026-02-27) replaced
the random cap lottery with a wage-weighted one, first used for the FY2027 cap season (the
March 2026 registration). Each registration is entered into the selection pool once per
OEWS wage level: a Level I job gets 1 entry, Level II 2, Level III 3, Level IV 4. The
level is the highest OEWS level the offered wage equals or exceeds for the job's SOC code
and area; a job with several worksites or a wage range takes the lowest applicable level.
Every registration can be selected once at most, whatever its number of entries.
    https://www.federalregister.gov/documents/2025/12/29/2025-23853/weighted-selection-process-for-registrants-and-petitioners-seeking-to-file-cap-subject-h-1b

The numbers.
  * FY2026 (the last fully published season, random lottery): 343,981 eligible registrations,
    120,141 selected (USCIS, "H-1B Electronic Registration Process" page, historical table).
    https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/h-1b-electronic-registration-process
  * FY2027 (first weighted season): USCIS reported 211,600 properly submitted registrations
    (down 38.5% from FY2026) and that 17.7% of selected registrations were Level I. USCIS has
    not published the FY2027 selection count as of October 2026, so the FY2026 selection
    count stands in for it by default; pass `selections=` to override once it is published.
    (Reported from USCIS's May 2026 release by Fragomen, "USCIS Completes FY 2027 H-1B Cap
    Selection Process"; the cap was reached with no second selection in July 2026.)
  * Mix of registrations by level: the registration form only started collecting the level
    in FY2027 and USCIS has not released the split, so the default is the proxy DHS used in
    the rule's own regulatory impact analysis, cap-subject petitions FY2020 to FY2024:
    28% Level I, 55% Level II, 12% Level III, 5% Level IV.

The formula. With R registrations split by level n_l = R * mix_l and w_l = l entries each,
selection is a draw of entries without replacement until S distinct registrations are
picked. Approximating each entry as an independent draw with probability q, a registration
with w entries is selected with probability p_l = 1 - (1 - q)^w_l, and q is the value at
which the expected selections sum(n_l * p_l) equal S (solved by bisection). This is the
standard approximation (the rule's own impact analysis uses the simpler linear form
p_l = S * w_l / sum(n_k * w_k), which the same inputs reproduce within a few points when
S is small against R; the form here stays below 100% when it is not). The result is an
estimate for a typical registration at that level, not a prediction for one person.
"""

from __future__ import annotations

WEIGHTS = {1: 1, 2: 2, 3: 3, 4: 4}
# DHS regulatory impact analysis proxy (cap-subject petitions FY2020 to FY2024).
DEFAULT_MIX = {1: 0.28, 2: 0.55, 3: 0.12, 4: 0.05}
FY2027_REGISTRATIONS = 211_600          # USCIS, May 2026 (properly submitted)
FY2026_REGISTRATIONS = 343_981          # USCIS registration page (eligible)
FY2026_SELECTIONS = 120_141             # USCIS registration page
FY2027_LEVEL1_SHARE_OF_SELECTED = 0.177  # USCIS, May 2026
DEFAULT_REGISTRATIONS = FY2027_REGISTRATIONS
DEFAULT_SELECTIONS = FY2026_SELECTIONS   # FY2027's count not published as of 2026-10

RULE_URL = ("https://www.federalregister.gov/documents/2025/12/29/2025-23853/"
            "weighted-selection-process-for-registrants-and-petitioners-seeking-to-file-cap-subject-h-1b")
NUMBERS_URL = ("https://www.uscis.gov/working-in-the-united-states/temporary-workers/"
               "h-1b-specialty-occupations/h-1b-electronic-registration-process")

ODDS_NOTE = ("An estimate from public filings and USCIS's published counts, not legal advice. "
             "Your own odds depend on the job's SOC code, area and offered wage.")


def _solve_q(n_by_level: dict, selections: float) -> float:
    """The per-entry draw probability at which expected selections equal `selections`."""
    total = sum(n_by_level.values())
    if total <= 0 or selections <= 0:
        return 0.0
    if selections >= total:
        return 1.0

    def expected(q):
        return sum(n * (1 - (1 - q) ** WEIGHTS[l]) for l, n in n_by_level.items())

    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if expected(mid) < selections:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def lottery_odds(level: int, registrations: int | None = None, selections: int | None = None,
                 mix: dict | None = None) -> dict:
    """Approximate per-registration selection probability for a wage level (1..4) under the
    weighted rule. Returns {"level", "entries", "probability" (0..1), "percent", "by_level"
    (every level's probability), "registrations", "selections", "mix", "basis", "sources"}.
    Defaults: FY2027's 211,600 registrations, FY2026's 120,141 selections as the stand-in
    count, and DHS's level mix (see the module docstring). A level outside 1..4 is clamped."""
    level = max(1, min(4, int(level or 1)))
    R = int(registrations or DEFAULT_REGISTRATIONS)
    S = int(selections or DEFAULT_SELECTIONS)
    m = dict(DEFAULT_MIX if mix is None else mix)
    tot = sum(m.values()) or 1.0
    n_by = {l: R * (m.get(l, 0.0) / tot) for l in WEIGHTS}
    q = _solve_q(n_by, S)
    by_level = {l: round(1 - (1 - q) ** WEIGHTS[l], 4) for l in WEIGHTS}
    p = by_level[level]
    return {
        "level": level, "entries": WEIGHTS[level], "probability": p, "percent": round(p * 100),
        "by_level": by_level, "registrations": R, "selections": S,
        "mix": {l: round(n_by[l] / R, 3) if R else 0 for l in WEIGHTS},
        "basis": (f"Weighted selection (Level IV 4 entries, III 3, II 2, I 1) over {R:,} "
                  f"registrations and {S:,} selections, with DHS's level mix."),
        "sources": [RULE_URL, NUMBERS_URL],
        "note": ODDS_NOTE,
    }


def likely_level(offer: int, pw_by_level: dict) -> int:
    """The highest level whose median prevailing wage (from this employer's own filings for
    the title and place) the offer meets. 0 when no level threshold is known."""
    best = 0
    for l in (1, 2, 3, 4):
        pw = int(pw_by_level.get(l) or 0)
        if pw and offer >= pw:
            best = l
    if not best:
        # Below every known threshold: Level I if the lowest known one is Level I's, else
        # unknown (we cannot say the offer is Level I when we only know Level III's wage).
        known = [l for l in (1, 2, 3, 4) if pw_by_level.get(l)]
        best = 1 if known and known[0] == 1 else 0
    return best


def _money(x: int) -> str:
    return f"${int(x):,}"


def offer_check(offer: int, wages: dict | None, title: str = "", city: str = "",
                state: str = "", registrations: int | None = None,
                selections: int | None = None) -> dict:
    """Compare an offer with the employer's LCA wages (a SponsorDB.wages() result, or None)
    and return the likely level, lottery odds and three negotiation talking points. Pure:
    no I/O. `offer` is an annual salary in dollars."""
    offer = int(offer or 0)
    out = {"offer": offer, "has_data": bool(wages), "position": "", "likely_level": 0,
           "level_basis": "", "odds": None, "talking_points": [], "note": ODDS_NOTE}
    if offer <= 0:
        out["error"] = "Enter the annual salary in dollars."
        return out
    where = f" in {city.title()}, {state.upper()}" if city and state else (f" in {state.upper()}" if state else "")
    if not wages:
        out["talking_points"] = [
            "No public LCA wage filings matched this employer and title, so there is no "
            "employer-specific range to compare with.",
            "Ask the employer which OEWS wage level the role's prevailing wage determination "
            "uses; under the weighted lottery a Level III or IV wage gets 3 or 4 entries.",
            "Check the DOL FLC Data Center for the prevailing wage of the job's SOC code and "
            "area, and aim for the next level's wage if it is within reach.",
        ]
        return out
    lo, med, hi, n = wages["wage_min"], wages["wage_median"], wages["wage_max"], wages["n"]
    label = wages.get("title_label") or title or "this title"
    scope_note = wages.get("scope_note") or ""
    if offer < lo:
        pos = "below_range"
    elif offer < med:
        pos = "below_median"
    elif offer <= hi:
        pos = "above_median"
    else:
        pos = "above_range"
    out["position"] = pos
    pw = {l: wages.get(f"pw{l}") or 0 for l in (1, 2, 3, 4)}
    lvl = likely_level(offer, pw)
    out["likely_level"] = lvl
    if lvl:
        out["level_basis"] = (f"This employer's filings for {label}{where} carried a median "
                              f"Level {'I' * lvl if lvl < 4 else 'IV'} prevailing wage of "
                              f"{_money(pw[lvl])}; your offer meets it.")
        out["odds"] = lottery_odds(lvl, registrations, selections)
    else:
        out["level_basis"] = ("The filings do not show a prevailing wage at a level your offer "
                              "reaches, so the level is unknown.")
    pts = []
    delta = (offer - med) / med if med else 0
    if pos in ("below_range", "below_median"):
        pts.append(f"{n} filings for {label}{where} put the median at {_money(med)}; your offer is "
                   f"{abs(round(delta * 100))}% below it, and their filings go up to {_money(hi)}. "
                   f"{scope_note}".strip())
    else:
        pts.append(f"{n} filings for {label}{where} put the median at {_money(med)}; your offer is "
                   f"{'at' if abs(delta) < 0.01 else str(abs(round(delta * 100))) + '% above'} it. "
                   f"The top of their filed range is {_money(hi)}. {scope_note}".strip())
    nxt = next((l for l in (2, 3, 4) if l > lvl and pw.get(l)), 0)
    if nxt:
        odds_now = lottery_odds(lvl, registrations, selections)["percent"] if lvl else None
        odds_next = lottery_odds(nxt, registrations, selections)["percent"]
        gap = pw[nxt] - offer
        roman = {1: "I", 2: "II", 3: "III", 4: "IV"}
        pts.append(f"A wage of {_money(pw[nxt])} would reach this employer's Level {roman[nxt]} "
                   f"prevailing wage ({_money(gap)} more), which gives the registration "
                   f"{WEIGHTS[nxt]} lottery entries"
                   + (f" (about {odds_next}% odds against {odds_now}% now)." if odds_now is not None
                      else f" (about {odds_next}% odds)."))
    else:
        pts.append("Your offer already meets the highest prevailing-wage level in this "
                   "employer's filings, so the lottery weighting is on your side; negotiate "
                   "on the base, not the level.")
    top = wages.get("top_level") or 0
    if top:
        share = wages.get("top_level_share") or 0
        pts.append(f"Most of their filings here ({round(share * 100)}%) were Level "
                   f"{'I' * top if top < 4 else 'IV'}; a request to be filed at the level your "
                   f"offer supports is normal and costs the employer nothing extra.")
    else:
        pts.append("The filings do not state a prevailing-wage level; ask which level the "
                   "employer will attest to on the LCA, since it decides the lottery entries.")
    out["talking_points"] = pts[:3]
    return out
