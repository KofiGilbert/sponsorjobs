"""Open a saved search on the other boards: plain search URLs for LinkedIn, Indeed, Glassdoor,
Handshake and Google Jobs, built from a strategy's keywords, location, work type and level.

We only build the address. The person's own browser opens it in a tab and they do the looking:
nothing here fetches, clicks or reads those sites (CLAUDE.md section 7: LinkedIn is browse-by-
hand, always). This lives under ui/, not sourcing/, on purpose: the sourcing package never names
these sites, and tests/test_sourcing.py keeps it that way.
"""

from __future__ import annotations

from urllib.parse import quote_plus, urlencode

# Boards in the order the buttons appear.
BOARD_LABELS = {
    "linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor",
    "handshake": "Handshake", "google": "Google Jobs",
}


def board_search_urls(q: str, loc: str = "", remote: str = "", level: str = "") -> list[dict]:
    """The same search as plain URLs on the big boards, for the browser to open in tabs.

    Each entry is {"board", "label", "url"}. Spaces become '+', quotes and commas are
    percent-encoded, so a phrase like '"data analyst" python' survives intact. Only the fields
    every board understands are mapped (keywords, location, and remote / entry-level where the
    board has a documented query flag); the finer sponsorship facets are ours alone.

    Handshake needs the student's own login, and its search page is app.joinhandshake.com; the
    address still works once they are signed in, which is their business, not ours. This function
    never contacts any of these sites."""
    q = (q or "").strip()
    loc = (loc or "").strip()
    remote = (remote or "").strip().lower()
    level = (level or "").strip().lower()
    enc = lambda params: urlencode(params, quote_via=quote_plus)   # noqa: E731

    out: list[dict] = []

    # LinkedIn: f_WT = work type (1 on-site, 2 remote, 3 hybrid); f_E=2 = entry level.
    li = {"keywords": q, "location": loc}
    wt = {"onsite": "1", "remote": "2", "hybrid": "3"}.get(remote)
    if wt:
        li["f_WT"] = wt
    if level == "entry":
        li["f_E"] = "2"
    out.append({"board": "linkedin", "label": BOARD_LABELS["linkedin"],
                "url": "https://www.linkedin.com/jobs/search/?" + enc(li)})

    # Indeed: q + l; "Remote" as the location is how Indeed expresses a remote search.
    ind = {"q": q, "l": loc or ("Remote" if remote == "remote" else "")}
    if level == "entry":
        ind["explvl"] = "entry_level"
    out.append({"board": "indeed", "label": BOARD_LABELS["indeed"],
                "url": "https://www.indeed.com/jobs?" + enc(ind)})

    # Glassdoor: sc.keyword + locKeyword (free-text location it resolves itself).
    gd = {"sc.keyword": q, "locKeyword": loc}
    if remote == "remote":
        gd["remoteWorkType"] = "1"
    out.append({"board": "glassdoor", "label": BOARD_LABELS["glassdoor"],
                "url": "https://www.glassdoor.com/Job/jobs.htm?" + enc(gd)})

    # Handshake: the student app's job search, keyword query only (location is a picker there).
    out.append({"board": "handshake", "label": BOARD_LABELS["handshake"],
                "url": "https://app.joinhandshake.com/job-search?" + enc({"query": q}),
                "note": "login"})

    # Google Jobs: a normal search with the jobs panel opened (ibp=htl;jobs). The ';' must stay
    # literal, which is why it is appended after the encoded part.
    terms = " ".join(t for t in (q, "jobs", loc, "remote" if remote == "remote" else "") if t)
    out.append({"board": "google", "label": BOARD_LABELS["google"],
                "url": "https://www.google.com/search?" + enc({"q": terms}) + "&ibp=htl;jobs"})
    return out


def strategy_open_urls(strategy: dict) -> list[dict]:
    """board_search_urls for a stored strategy row."""
    f = strategy.get("filters") or {}
    return board_search_urls(str(f.get("q") or "").strip(), f.get("loc", ""), f.get("remote", ""), f.get("level", ""))


