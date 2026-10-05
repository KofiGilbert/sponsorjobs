"""Email / inbox for the person's OWN mailbox (CLAUDE.md §5).

Read-first, send-on-consent: scan job-related mail, surface application verification
links, and draft recruiter replies for the person to review. Never touches anyone else's
inbox and never sends without consent.
"""

from .detect import classify, sender_name, verification_link
from .service import draft_reply, scan_inbox

__all__ = ["classify", "sender_name", "verification_link", "scan_inbox", "draft_reply"]
