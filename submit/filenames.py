"""What a resume or cover letter is CALLED when it leaves the app.

Tailored files are stored under internal names (``cv-<record id>.pdf``,
``cover-<record id>.pdf``). Those names mean nothing to an employer, and an upload named
``cv-123.pdf`` looks careless. So every path that hands a file to someone else -- an API
submit, the in-app form upload, a Download button -- names it from the person's own profile
name instead: ``Kofi Gilbert Resume.pdf`` / ``Kofi Gilbert Cover Letter.pdf``. The stored
files keep their internal names; only the outgoing name changes.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

_LABELS = {"resume": "Resume", "cover": "Cover Letter"}
# Path separators, Windows-reserved characters, and control characters never belong in a
# filename someone else's system will save.
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_MAX_NAME = 80


def safe_person_name(name) -> str:
    """The person's name made safe to use in a filename ('' when there is none)."""
    s = _BAD.sub(" ", str(name or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:_MAX_NAME].rstrip(" .")


def export_filename(name, kind: str = "resume", ext: str = "pdf") -> str:
    """``"<Name> Resume.pdf"`` / ``"<Name> Cover Letter.pdf"``; ``"Resume.pdf"`` (or
    ``"Cover Letter.pdf"``) when there is no usable name."""
    label = _LABELS.get(kind, _LABELS["resume"])
    who = safe_person_name(name)
    ext = str(ext or "pdf").lstrip(".")
    return f"{who} {label}.{ext}" if who else f"{label}.{ext}"


def upload_copy(src, dest_dir, filename: str) -> Path:
    """A copy of ``src`` named ``filename`` inside ``dest_dir`` (one file per folder), for
    uploads that take the file's own name (a browser file input via CDP uses the path's
    basename). Refreshed when the source is newer; any older copy under another name
    (the person renamed themselves) is removed so the folder never holds two."""
    src, dest_dir = Path(src), Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    for old in dest_dir.iterdir():
        if old.is_file() and old.name != filename:
            try:
                old.unlink()
            except OSError:
                pass
    if not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime \
            or dest.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dest)
    return dest
