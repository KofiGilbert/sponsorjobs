# SponsorJobs desktop shell (Electron)

The Cursor-style architecture: the app window and an in-app browser pane are both
native Chromium, so any site loads (no iframe restrictions). In a dev run the shell is
CDP-debuggable on port 9223 (or TAILOR_CDP_PORT) for the e2e scripts; a packaged build
opens no debugging port unless TAILOR_CDP_PORT is set. The apply agent itself drives the
pane through Electron's own debugger API and needs no port.

Session token: each launch the shell makes a random token, passes it to the engine it
spawns (TAILOR_SESSION_TOKEN) and sets it as an HttpOnly, SameSite=Strict cookie on the
app window. With the token set the engine answers only requests that carry it (the
cookie, or the X-Tailor-Session header the shell's own code sends). A script that starts
the engine itself and then the shell can set TAILOR_SESSION_TOKEN for both.

Dev run (spawns the Python engine if 127.0.0.1:57000 is not already serving):

    cd shell-electron
    npm install
    npm start          # unset ELECTRON_RUN_AS_NODE if your terminal sets it

Links: any external link in the app (including clicks inside the embedded CV-preview
PDF, which route through /api/open) opens in the drawer, never carrying the app away.
Outside the shell, a POST to /api/open opens the link in the system browser; a plain
GET only shows the link.

Packaging (electron-builder + PyInstaller sidecar) is the next step; this directory
is the dev shell.

## Building the installer

    # 1. Build the Python engine sidecar (repo root):
    python -m PyInstaller packaging/tailor.spec --noconfirm     # -> dist/SponsorJobs/

    # 2. Build the installer (this directory):
    npm run dist -- -c.directories.output="%LOCALAPPDATA%\Temp\sponsorjobs-release"

Notes:
- OUTPUT MUST BE OUTSIDE ONEDRIVE: building the 600MB tree inside a synced folder
  makes app-builder fail with ERR_ELECTRON_BUILDER_CANNOT_EXECUTE and locks files
  mid-build. Any local (non-synced) output path works.
- The build is unsigned (win.signAndEditExecutable=false): SmartScreen will warn on
  first run until code signing is added at launch (see CLAUDE.md section 13).
- LaTeX remains an install-time prerequisite (the engine finds system pdflatex);
  bundling Tectonic needs compiler support first and is a follow-up.
- Installed user data lives in %LOCALAPPDATA%\SponsorJobs, separate from any dev profile.
- Dev terminals that set ELECTRON_RUN_AS_NODE must unset it or the exe runs as
  plain node and exits instantly.

## Before every release

Run the real-window checks with the app closed (two copies cannot run at once):

```
npm run test:e2e-pane     # the in-app browser lands exactly on its slot at 67%, 76%, 100%, 125%, 150% zoom
```

The pane geometry is also pinned in CI by `tests/test_pane_geometry.py`. Any change to
`layoutPane`, `panegeom.js`, `preload.js` or the panel layout must pass both.
