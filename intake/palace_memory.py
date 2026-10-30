"""Kept for one release so old imports do not break: the memory layer is now the plain chat
diary in ``intake/diary.py`` (docs/applicant-brain.md, section 12). MemPalace and its ChromaDB
index are gone; ``PalaceMemory`` is the diary. Removed in Phase 7."""

from intake.diary import Diary as PalaceMemory, _slug, erase_all  # noqa: F401
