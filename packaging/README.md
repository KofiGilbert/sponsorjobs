# Packaging SponsorJobs as an installed app

Turns the repo into `SponsorJobsSetup.exe`: a normal Windows install with a Start-menu entry, a
desktop icon, and no terminal window.

## Build it

```
pip install pyinstaller
python packaging/bundle_sponsor_db.py      # vet + stage the visa-data snapshot (55MB)
pyinstaller packaging/tailor.spec          # -> dist/SponsorJobs/SponsorJobs.exe
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" packaging\tailor.iss
                                           # -> dist/installer/SponsorJobsSetup.exe
```

Roughly: app folder 254MB, installer 81MB, build about a minute.

## Build it on macOS

Build on the Mac architecture you are targeting. PyInstaller only produces binaries for the
machine it runs on, so an Apple Silicon Mac makes an `arm64` build.

```
pip install pyinstaller
python packaging/bundle_sponsor_db.py
pyinstaller packaging/tailor.spec --noconfirm        # -> dist/SponsorJobs/SponsorJobs
cd shell-electron
npm install
CSC_IDENTITY_AUTO_DISCOVERY=false npm run dist:mac -- \
    -c.electronDist=node_modules/electron/dist        # -> release/SponsorJobs-<version>-arm64.dmg
```

`electronDist` reuses the Electron that `npm install` already downloaded instead of
fetching a second 100MB copy.

The build is **not signed or notarized**. macOS will refuse to open it on first launch. The
person opens **System Settings > Privacy & Security** and clicks **Open Anyway**, once.
Signing needs an Apple Developer account ($99 a year); add it before a wide launch.

LaTeX is a prerequisite on macOS too. `tailoring/compiler.py` looks in MacTeX's
`/Library/TeX/texbin` and Homebrew's bin directories itself, because an app opened from
the Finder does not inherit the shell's PATH.

## The decisions, and why

**The CV templates are untouched.** We render with the same pdflatex, at the same settings,
so an installed CV is byte-for-byte the CV the dev run makes. Tectonic (a 20MB self-
contained LaTeX) was measured as a way to shrink the download, and it would have forced
`microtype` font expansion off. That was rejected: the download size is not worth any
change to the output.

**LaTeX is a prerequisite, not a bundle.** It is a ~600MB toolchain with its own installer;
PyInstaller is the wrong tool for relocating it. `tailor.iss` checks for MiKTeX/TeX Live
*before* installing and says so plainly, because discovering a missing prerequisite while
trying to build your first CV is the worst possible moment.

**One folder, not one file.** A `--onefile` exe unpacks ~60MB to a temp dir on every
launch: a visible pause each time, and the toolchain lands somewhere different on each run.
The installer hides the folder anyway.

**`console=False`.** No black terminal behind the window. That is the whole difference
between "I run a Python command" and "I installed an app". It also means a crash would
vanish silently, so `launcher.py` shows a message box instead.

**Per-user install (`PrivilegesRequired=lowest`).** No admin prompt. A UAC dialog on first
run is where a nervous first-time user quits.

**The person's data is NOT in the install folder.** Profile, CVs and API key live in
`%LOCALAPPDATA%\SponsorJobs` (see `launcher.py`). Under Program Files that folder would not be
writable, and uninstalling or updating would delete everything they own. The uninstaller
deliberately leaves it alone.

**No UPX.** UPX-packed executables are a classic antivirus false positive, and an AV
warning on a CV app that holds your API key is not a trade worth making.

## The visa data ships inside the installer

A fresh install used to have ZERO sponsor data: H-1B arrived only via the Update button and
PERM/E-Verify only by manual file import, so a buyer's first LinkedIn session showed "No
sponsor record" on every job. The founder's dev machine had 582,145 employers; the packaged
app's own data dir had 0.

`bundle_sponsor_db.py` builds a vetted snapshot (public U.S. government data, public
domain, redistributable) from the committed seed `seed/sponsors_seed.csv.gz` plus the
E-Verify bundle in `config/`, so it works on a fresh checkout with no `data/` directory. A
local `data/sponsors.db` is used instead only when it holds MORE H-1B employers than the seed
(the first version copied the dev DB blindly, and that DB had zero H-1B rows, so the installer
shipped without the headline badge). The spec bundles it, and first run copies it into the
person's data dir. How the H-1B data is kept fresh now that USCIS publishes no yearly file:
`docs/h1b-data.md`. An EMPTY existing database is replaced (the packaged app had already created one);
a populated one is never touched, so the person's own newer imports always win. The Update
button stays for freshness.

At scale this also moves the download load onto OUR installer instead of a million fresh
installs each pulling files from a government website.

## The job list is not bundled

The installer ships no job list. A fresh install downloads the live feed (`JOBS_FEED_URL`) on first
open and shows a "connecting" state meanwhile; only when the feed is absent, or has failed for 30
minutes with nothing cached, does it run a small, polite first-open crawl. Details: `docs/feed.md`,
"What a fresh install sees".

## The browser extension

The installer ships `extension/` beside the app, and the Start-menu group has a shortcut to
the folder. On LinkedIn and Indeed, which forbid bots (§7), the extension is the ONLY way
we help, so an app installed without it is silent on the two sites people actually search.

**Chrome cannot be made to install an unpacked extension from an installer.** That is
Chrome's own security rule, not a gap here. So today the person clicks "Load unpacked"
once, and the app tells them how: `/api/extension/status` reports whether the extension has
called us, and the Jobs page shows the three steps and the exact folder path until it has.
The app never asks "did you install it?", because that is a question the software should
answer itself.

**The real fix is a Chrome Web Store listing at launch.** $5 one-time, a review of a few
days, then one-click install and auto-updates. It must be FREE: Google shut down Chrome Web
Store payments on 1 Feb 2021, so paid extensions no longer exist. Every competitor's
extension is free for the same reason (Teal, Huntr, Simplify, Careerflow, FrogHire), and
the paywall lives in the app. FrogHire has ~40,000 users on a free listing: the store is
distribution, not a checkout.

## Not done yet

- **Code signing.** Unsigned, SmartScreen will warn on first run. Needs a real certificate.
- **macOS / Linux.** The spec is cross-platform; `tailor.iss` is Windows-only. macOS needs
  a `.app` bundle + notarisation.
- **Updates.** No updater. Today a new version means running the new installer.
