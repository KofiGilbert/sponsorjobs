"""The desktop shell: open SponsorJobs as an APP window, not as a browser tab.

SponsorJobs is a local-first desktop app (CLAUDE.md §5) that happens to render its UI with web
tech. Launching it at 127.0.0.1:57000 in a normal tab misrepresents the product to the
person building it: you cannot judge an installed app through a URL bar, a bookmarks strip
and eleven other tabs, and every screen gets evaluated against the wrong reference.

§11 says keep the v1 shell simple and don't over-engineer it before the engine works. This
is the cheapest thing that is honestly an app window: Chrome/Edge in `--app` mode, which
has no tab strip, no URL bar, no bookmarks, its own taskbar entry and its own alt-tab
identity. No Electron, no Tauri, no 100MB bundle, no second rendering engine to keep alive.

`--user-data-dir` gives it a profile of its own, which matters for more than looks:
  * the window carries no bookmarks bar or extensions from the person's browsing;
  * their logged-in sessions are NOT in this profile, so nothing here can act as their
    signed-in LinkedIn, which §7 forbids by design rather than by good intentions;
  * their real browser and SponsorJobs can be open at once without fighting over one profile.

Deliberately NOT a headless-Chromium takeover of their screen: the in-app browsing surface
(§13, the Devin-style view) is a different feature and belongs in the app's own UI, not in
how we launch it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_WIN_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
_MAC_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


def find_chromium() -> str | None:
    """A Chrome/Edge binary that supports --app, or None.

    Chromium is the only thing here that can render a chromeless window without shipping a
    browser ourselves. Every desktop that can run this app already has one.
    """
    env = os.environ.get("TAILOR_BROWSER")
    if env and Path(env).exists():
        return env
    for name in ("chrome", "google-chrome", "chromium", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    cands = _WIN_CANDIDATES if sys.platform == "win32" else (
        _MAC_CANDIDATES if sys.platform == "darwin" else ())
    for c in cands:
        if Path(c).exists():
            return c
    return None


def open_app_window(url: str, profile_dir: str | Path, width: int = 1280,
                    height: int = 860) -> subprocess.Popen | None:
    """Open ``url`` as an app window. Returns the process, or None if we fell back.

    Falls back to the normal browser rather than failing: a person who has neither Chrome
    nor Edge should still get their app, just in a tab, and should be told why.
    """
    exe = find_chromium()
    if not exe:
        import webbrowser
        webbrowser.open(url)
        return None
    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        f"--app={url}",                     # no tab strip, no URL bar, own taskbar entry
        f"--user-data-dir={profile}",       # our own profile: see the module docstring
        f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",       # never ask to be their default browser
        "--disable-features=Translate,MediaRouter",
    ]
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        import webbrowser
        webbrowser.open(url)
        return None
