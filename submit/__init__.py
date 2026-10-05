"""Submission: decide, per site, whether an application may be submitted autonomously
or must be handed to the person to click, and route it accordingly (CLAUDE.md §7).

Conservative by design — a wrong auto-submit is worse than an extra click, so the default
is always ASSISTED. AUTO is sourced solely from the curated, evidence-based allowlist in
``submit.allowlist`` (human-maintained; no runtime inference). See ``submit.policy`` for
the lanes, ``submit.drivers`` for the per-site submission drivers, and ``submit.service``
for the routing.
"""

from submit.allowlist import REGISTRY, auto_hosts, evidence_for
from submit.drivers import driver_for
from submit.policy import submission_policy
from submit.service import classify_record, submit_record

__all__ = ["submission_policy", "classify_record", "submit_record",
           "auto_hosts", "evidence_for", "driver_for", "REGISTRY"]
