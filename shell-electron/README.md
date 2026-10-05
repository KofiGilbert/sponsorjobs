# SponsorJobs desktop shell (Electron)

The Cursor-style architecture: the app window and an in-app browser pane are both
native Chromium, so any site loads (no iframe restrictions) and the pane is
CDP-debuggable on port 9223 - the assisted-apply takeover hook (the agent fills,
the person watches and clicks submit; CLAUDE.md section 7).

Dev run (spawns the Python engine if 127.0.0.1:57000 is not already serving):

    cd shell-electron
    npm install
    npm start          # unset ELECTRON_RUN_AS_NODE if your terminal sets it

Links: any external link in the app (including clicks inside the embedded CV-preview
PDF, which route through /api/open) opens in the drawer, never carrying the app away.
Outside the shell, /api/open falls back to the system browser.

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
