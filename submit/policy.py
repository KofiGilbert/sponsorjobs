"""Per-site submission policy (CLAUDE.md §7).

Two lanes:

  "auto"     — the site has a real, sanctioned, applicant-usable submission path that a
               human maintainer has VERIFIED and recorded in ``submit.allowlist``. Only
               these submit unattended.
  "assisted" — everything else, and BY DEFAULT: pre-fill the form, the person reviews and
               clicks submit. Unknown / unverified / employer-key-gated / automation-
               prohibited (LinkedIn, …) all land here.

The AUTO lane is sourced SOLELY from the curated, evidence-based allowlist that ships with
the product. There is deliberately no runtime path — no inference, no guessing, no user
config — that can move a site into AUTO. Conservative on purpose: a wrong auto-submit is
worse than an extra click, so anything not explicitly verified is assisted.
"""

from __future__ import annotations

from urllib.parse import urlparse

from submit.allowlist import PROHIBITED_HOSTS, auto_hosts


# Where an application is allowed to NAVIGATE while the apply agent runs (docs/apply-agent.md
# section 7): the sourcing ATSs plus the big employer portals a careers page hands off to. A
# run that lands anywhere outside the apply URL's own domain and these stops ("left-site").
# This is a navigation fence only; it says nothing about auto-submit (the allowlist does, and
# this list deliberately lives outside it).
ATS_HOSTS: tuple[str, ...] = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "recruitee.com",
    "smartrecruiters.com",
    "myworkdayjobs.com", "icims.com", "taleo.net", "successfactors.com", "jobvite.com",
    "bamboohr.com", "paylocity.com", "ultipro.com", "adp.com",
)


def _host(url: str) -> str:
    try:
        h = (urlparse(url if "//" in (url or "") else "//" + (url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def _matches(host: str, suffixes) -> bool:
    """True if host equals a suffix or is a sub-domain of it (foo.recruitee.com ~ recruitee.com)."""
    return any(host == s or host.endswith("." + s) for s in suffixes)


# Country suffixes that take a third label (acme.co.uk, not co.uk). Enough for the career
# sites we meet; a miss only narrows the fence to the full host, never widens it.
_TWO_PART_SUFFIXES = frozenset((
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp",
    "co.in", "com.br", "com.mx", "co.za", "com.sg", "com.hk", "co.kr", "com.tr",
))


def registrable_domain(host: str) -> str:
    """acme.wd5.myworkdayjobs.com -> myworkdayjobs.com, careers.acme.co.uk -> acme.co.uk."""
    labels = [p for p in (host or "").lower().split(".") if p]
    if len(labels) <= 2:
        return ".".join(labels)
    n = 3 if ".".join(labels[-2:]) in _TWO_PART_SUFFIXES else 2
    return ".".join(labels[-n:])


def run_allowed_hosts(apply_url: str) -> set[str]:
    """The hosts an apply-agent run may navigate to: the apply URL's host, its registrable
    domain, and the known ATS hosts (sub-domains included by the shell). Empty for a prohibited
    or unparseable URL, and a prohibited host is never in the set however it got there."""
    host = _host(apply_url)
    if not host or _matches(host, PROHIBITED_HOSTS):
        return set()
    hosts = {host, registrable_domain(host), *ATS_HOSTS}
    return {h for h in hosts if h and not _matches(h, PROHIBITED_HOSTS)}


def submission_policy(url: str) -> str:
    """Return the submission lane for a destination URL: 'auto' | 'assisted'.
    Default is 'assisted' (conservative — including for an empty/unparseable URL)."""
    host = _host(url)
    if not host:
        return "assisted"
    if _matches(host, PROHIBITED_HOSTS):        # prohibiting sites win outright
        return "assisted"
    if _matches(host, auto_hosts()):            # only the verified, shipped allowlist
        return "auto"
    return "assisted"


def fill_permitted(url: str) -> bool:
    """May Tailor's in-app agent autonomously FILL this site's form (the person still approves and
    submits)? True for any normal application form; FALSE for the bot-prohibited job boards
    (LinkedIn, Indeed, Glassdoor, ...) that forbid automation — there the person fills by hand.

    Note this is deliberately broader than the 'auto' SUBMIT lane: filling a form the person is
    already applying through, in a visible browser, and stopping for their approval is not evasion.
    Prohibited sites are the honest hard line (CLAUDE.md section 7). Unknown/unparseable URL -> no."""
    host = _host(url)
    return bool(host) and not _matches(host, PROHIBITED_HOSTS)


def apply_decision(start_url: str, page_url: str = "") -> dict:
    """What the in-app Apply / Fill may do on a page. The ONE place the desktop shell asks
    before it touches a site's own submit button (shell-electron/main.js, assist:apply).

    - ``fill``: may Tailor fill the form at all (False on LinkedIn and the other prohibited
      boards: the person fills those by hand).
    - ``click_submit``: may Tailor click the site's final submit. True ONLY when BOTH the
      application link the person chose and the page actually open now are on the verified
      AUTO allowlist. A redirect from an allowlisted link to another host (a careers site
      handing off to its own ATS) therefore drops to assisted. Everywhere else the shell fills,
      stops, and points at the submit button for the person to click (CLAUDE.md section 7).

    Conservative by construction: an empty or unparseable URL is never auto."""
    urls = [u for u in (start_url, page_url) if (u or "").strip()]
    if not urls:
        return {"fill": False, "click_submit": False, "lane": "assisted"}
    fill = all(fill_permitted(u) for u in urls)
    click = fill and all(submission_policy(u) == "auto" for u in urls)
    return {"fill": fill, "click_submit": click, "lane": "auto" if click else "assisted"}
