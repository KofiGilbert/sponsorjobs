"""Job sourcing — GREEN-lane only (CLAUDE.md §6).

Stage 1 pulls live open roles from official, public, no-auth ATS JSON feeds:
Greenhouse, Lever, and Ashby. These are the sanctioned sources — public boards
the companies themselves publish. There is deliberately NO scraping of gated job
sites, no scraper libraries, and no proxies. If a source can't be reached through
an official/authorized API, it is not used.
"""

from .ats import ADAPTERS, fetch_json, normalize_jobs
from .watchlist import Watchlist
from .service import refresh_watchlist, matches_criteria, SUPPORTED_ATS

__all__ = [
    "ADAPTERS", "fetch_json", "normalize_jobs",
    "Watchlist", "refresh_watchlist", "matches_criteria", "SUPPORTED_ATS",
]
