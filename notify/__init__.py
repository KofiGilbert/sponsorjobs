"""Approve-while-away notifications (CLAUDE.md §5).

Alert the person on Telegram when a tailored package is ready and let them approve it
with a one-line reply from their phone. Obeys only the owner's own chat; approving marks
a package applied — it never submits on its own.
"""

from .service import NotifyService, format_ready
from .telegram import TelegramBot

__all__ = ["NotifyService", "TelegramBot", "format_ready"]
