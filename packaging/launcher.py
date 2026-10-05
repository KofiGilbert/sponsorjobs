"""Entry point for the packaged SponsorJobs.exe.

Separate from `python -m ui.app` because a frozen app has to solve two things the dev run
never does:

1. WHERE ITS FILES LIVE. PyInstaller unpacks bundled data next to the exe (sys._MEIPASS),
   which is NOT the current working directory, and code that reads `config/...` relatively
   would look in whatever folder the person happened to launch from. We pin it here, once.

2. WHERE THE PERSON'S DATA LIVES. It must NOT be next to the exe: that folder sits under
   Program Files (not user-writable), and an uninstall or update would take their profile,
   their CVs and their API key with it. %LOCALAPPDATA%\\SponsorJobs survives both.

Errors get a message box rather than a traceback into a console nobody can see, because
console=False means a crash would otherwise be a silently vanishing app.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# The product was "Tailor" until 2026-10; dev installs from before the rename keep their data
# under the old folder name, so the first launch of SponsorJobs adopts it (see _user_data_dir).
DATA_DIR_NAME = "SponsorJobs"
LEGACY_DATA_DIR_NAME = "Tailor"


def _bundle_dir() -> Path:
    """Where our bundled files actually are (exe-adjacent when frozen, repo when not)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _data_base() -> Path:
    """The per-user application-data root for this platform."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _migrate_legacy_data_dir(base: Path) -> bool:
    """One-time rename of the pre-rename data folder (``Tailor``) to ``SponsorJobs``.

    Only when the new folder does not exist yet and the old one is a real directory: a
    person who has already run SponsorJobs keeps what they have, and we never merge two
    profiles or follow a symlink. A rename on the same filesystem is atomic; if the OS
    refuses (locked file, cross-device), fall back to a move, and if that fails too, leave
    the old folder untouched and start fresh rather than crash before the window opens.
    Returns True when a migration happened.
    """
    old = base / LEGACY_DATA_DIR_NAME
    new = base / DATA_DIR_NAME
    if new.exists() or not old.is_dir() or old.is_symlink():
        return False
    try:
        old.rename(new)
        return True
    except OSError:
        pass
    try:
        shutil.move(str(old), str(new))
        return True
    except (OSError, shutil.Error):
        return False


def _user_data_dir(base: Path | None = None) -> Path:
    """The person's own data: their profile, CVs, and API key.

    Kept out of the install folder on purpose. Under Program Files it would not be
    writable, and uninstalling or updating the app would delete everything they own.
    """
    base = base if base is not None else _data_base()
    _migrate_legacy_data_dir(base)
    d = base / DATA_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _die(title: str, message: str) -> None:
    """Say what went wrong. With console=False a traceback goes nowhere."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        print(f"{title}: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    bundle = _bundle_dir()
    data = _user_data_dir()
    # Run from the bundle so every relative `config/...` read resolves, and point the app's
    # own env vars at the person's writable data dir before ui.app imports and reads them.
    os.chdir(bundle)
    sys.path.insert(0, str(bundle))
    os.environ.setdefault("RESUME_AGENT_DATA_DIR", str(data))
    os.environ.setdefault("RESUME_AGENT_CRED_FILE", str(data / "credentials.env"))

    try:
        from ui.app import main as run
    except Exception as exc:                       # noqa: BLE001 - last line of defence
        _die("SponsorJobs could not start", f"{exc}\n\nBundle: {bundle}")
        return
    try:
        run()
    except Exception as exc:                       # noqa: BLE001
        _die("SponsorJobs stopped unexpectedly", str(exc))


if __name__ == "__main__":
    main()
