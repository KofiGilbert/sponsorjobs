"""Writing secret files so only the person's own account can read them, from the first byte.

``Path.write_text`` creates a file with the process umask (usually readable by every account on
the machine) and a later ``chmod`` leaves a window where it is not private, and never fixes a
file another tool created. Every file that holds a secret (credentials.env, the Gmail token, the
App Lock file) is written here instead:

* the new content goes to a temporary file in the same folder, created with ``os.open`` and mode
  0o600 (``O_EXCL``, so an existing file or link of that name is never reused),
* it is flushed and synced, then moved over the real name with ``os.replace`` (atomic: a reader
  sees the old file or the new one, never half of it, and a crash leaves the old file intact),
* the result is chmod 0o600 once more, for a file system that ignored the mode at creation.

Windows has no POSIX modes: there the file is written the same way and the mode is a no-op. The
files sit in the person's own data folder, which the OS keeps per user.

Follow-up (not done here): move these secrets into the OS keychain (macOS Keychain, Windows
Credential Manager) instead of files.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

PRIVATE_MODE = 0o600


def write_private(path, data: str | bytes, encoding: str = "utf-8") -> None:
    """Atomically replace ``path`` with ``data``, readable and writable by the owner only."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode(encoding) if isinstance(data, str) else bytes(data)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, PRIVATE_MODE)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    make_private(path)


def make_private(path) -> None:
    """chmod 0o600 an existing file. Best effort: Windows has no POSIX modes, and a failed chmod
    must never lose a saved value."""
    try:
        os.chmod(path, PRIVATE_MODE)
    except OSError:
        pass
