"""Curated, evidence-based AUTO-lane allowlist (CLAUDE.md §7).

THE hard rule that makes auto-apply safe: a site is in the AUTO lane ONLY if a human
maintainer has verified it has a real, sanctioned, and **applicant-usable** submission
path — an official application-submission API (or explicit documented permission for
programmatic submission) that an applicant-run app can actually call **without the
employer's secret credential** — and recorded the evidence here.

This module is the SINGLE source of truth for auto-eligibility. There is deliberately no
runtime path that adds a host to the AUTO lane: no inference from the page, no guessing
from robots.txt, no "looks submittable", no user-editable config file. Adding a site =
a maintainer edits ``REGISTRY`` below, with a doc reference, after real verification.
Unknown / unverified / employer-key-gated / automation-prohibited → ASSISTED, always.

Each entry records an ATS's official submission endpoint and WHY it is or isn't
auto-eligible, so the allowlist doubles as an auditable evidence log.

Verified 2026-07-14 against official developer docs (see ``doc_url`` per entry). Of the
ATSs in our sourcing set, only Recruitee exposes a keyless, candidate-facing submission
API; every other one gates submission behind the employer's secret key, so they are
correctly ASSISTED.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubmissionEvidence:
    ats: str
    hosts: tuple            # hostnames (and their subdomains) this evidence covers
    endpoint: str           # the official submission endpoint
    doc_url: str            # official documentation reference
    auth_model: str         # "public_keyless" | "employer_api_key" | "employer_oauth" | ...
    applicant_usable: bool  # can an applicant-run app submit WITHOUT an employer secret?
    reason: str             # one-line justification, shown in the queue
    verified_on: str
    # Auto fires ONLY when a maintainer sets this True AND applicant_usable is True. Both
    # flags must agree — a maintainer can never enable a non-applicant-usable site.
    auto_enabled: bool = False

    @property
    def auto_eligible(self) -> bool:
        return self.auto_enabled and self.applicant_usable


# Automation-PROHIBITED hosts: their terms ban programmatic/bot submission (or logged-in
# account automation). Always assisted, and checked BEFORE the allowlist so they can never
# be promoted — LinkedIn is the headline case (CLAUDE.md §7).
PROHIBITED_HOSTS: tuple[str, ...] = (
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "dice.com",
)


# P5 re-review (2026-07-19): grew ASSISTED reach and GREEN sourcing volume, but added ZERO new
# AUTO entries. No additional ATS/portal in our set was found to expose a keyless, candidate-facing
# submission API in its official docs, so promoting any would violate §7. Everything below stays as
# verified: only Recruitee is applicant-usable; the rest remain ASSISTED with their evidence intact.
#
# The evidence log. AUTO ⟺ a `auto_enabled` + `applicant_usable` entry here.
REGISTRY: tuple[SubmissionEvidence, ...] = (
    SubmissionEvidence(
        ats="Recruitee",
        hosts=("recruitee.com",),
        endpoint="POST https://{company}.recruitee.com/api/offers/{offer_slug}/candidates",
        doc_url="https://docs.recruitee.com/reference/intro-to-careers-site-api",
        auth_model="public_keyless",
        applicant_usable=True,
        reason=("Recruitee's Careers Site API is keyless and candidate-facing. The docs "
                "state it 'does not require authorization' and 'creating a candidate via "
                "this API is like a candidate applying for a job.' Sanctioned, applicant-usable."),
        verified_on="2026-07-14",
        auto_enabled=True,      # the ONE verified auto-eligible ATS
    ),
    SubmissionEvidence(
        ats="Greenhouse",
        hosts=("greenhouse.io",),
        endpoint="POST https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{id}",
        doc_url="https://developers.greenhouse.io/job-board.html",
        auth_model="employer_api_key",
        applicant_usable=False,
        reason=("Official submit API, but it needs the employer's secret Job Board API key "
                "(Basic Auth); the docs call it 'a secret key' and require server-side "
                "proxying. An applicant-run app can't obtain it, so assisted."),
        verified_on="2026-07-14",
    ),
    SubmissionEvidence(
        ats="Lever",
        hosts=("lever.co",),
        endpoint="POST https://api.lever.co/v0/postings/{site}/{posting}?key=APIKEY",
        doc_url="https://github.com/lever/postings-api/blob/master/README.md",
        auth_model="employer_api_key",
        applicant_usable=False,
        reason=("Official apply endpoint, but the API key is generated only by a Super "
                "Admin of the employer's account, so not applicant-usable. Assisted."),
        verified_on="2026-07-14",
    ),
    SubmissionEvidence(
        ats="Ashby",
        hosts=("ashbyhq.com",),
        endpoint="POST https://api.ashbyhq.com/applicationForm.submit",
        doc_url="https://developers.ashbyhq.com/reference/applicationformsubmit",
        auth_model="employer_api_key",
        applicant_usable=False,
        reason=("Submit needs the employer's long-lived API key with candidatesWrite; the "
                "docs say it's not for browsers and must be proxied server-side. Assisted."),
        verified_on="2026-07-14",
    ),
    SubmissionEvidence(
        ats="Workable",
        hosts=("workable.com",),
        endpoint="POST https://{subdomain}.workable.com/spi/v3/jobs/{shortcode}/candidates",
        doc_url="https://workable.readme.io/reference/job-candidates-create",
        auth_model="employer_api_key",
        applicant_usable=False,
        reason=("Candidate-create needs the account's Bearer token (w_candidates) or "
                "partner-only OAuth; no keyless applicant route. Assisted."),
        verified_on="2026-07-14",
    ),
    SubmissionEvidence(
        ats="SmartRecruiters",
        hosts=("smartrecruiters.com",),
        endpoint="POST https://api.smartrecruiters.com/postings/{uuid}/candidates",
        doc_url="https://developers.smartrecruiters.com/docs/partners-post-an-application",
        auth_model="employer_api_key",
        applicant_usable=False,
        reason=("The public Posting API is read-only; submitting a candidate needs the "
                "employer's API key (X-SmartToken) or partner OAuth. Assisted."),
        verified_on="2026-07-14",
    ),
)


def auto_hosts() -> frozenset[str]:
    """Hosts that are auto-eligible: verified applicant-usable AND maintainer-enabled."""
    hs: set[str] = set()
    for e in REGISTRY:
        if e.auto_eligible:
            hs.update(e.hosts)
    return frozenset(hs)


def evidence_for(host: str) -> SubmissionEvidence | None:
    """The evidence entry whose hosts cover ``host`` (exact or sub-domain), or None."""
    host = (host or "").lower()
    for e in REGISTRY:
        for h in e.hosts:
            if host == h or host.endswith("." + h):
                return e
    return None
