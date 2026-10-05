# Contributing to SponsorJobs

Thanks for helping. SponsorJobs exists so international students can job hunt without paying for
tools they may not be allowed to earn money to afford. Good contributions keep it honest,
private, and easy to install.

## Set up

```bash
git clone https://github.com/KofiGilbert/sponsorjobs.git sponsorjobs
cd sponsorjobs
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev,llm]"
python -m pytest
```

Tests use a deterministic fake model, so they need no API key and no network. Tests that
compile a real PDF are skipped when `pdflatex` is not installed. The README lists the LaTeX
packages you need to run them.

## Hard rules

These come from [CLAUDE.md](CLAUDE.md), which is the design source of truth. A pull request
that breaks one of them will not be merged, however useful it is otherwise.

- **Never fabricate.** Tailoring rewords the person's real material. It never invents
  employers, dates, numbers, or skills, and never moves a skill onto a role where it did
  not happen.
- **No stealth automation.** No automating a logged-in LinkedIn account, no captcha
  solving, no anti-detection tricks, no rotating proxies. Automatic submission is allowed
  only on sites listed in `submit/allowlist.py`, which a maintainer updates after verifying
  the site permits it.
- **Personal data stays local.** Profiles, résumés, and applications live on the person's
  machine. Do not add telemetry or upload personal data.
- **One page, clean compile.** The tailoring engine must never ship a PDF that is broken,
  longer than one page, or has new overfull-line warnings.

## Pull requests

1. Open an issue first for anything larger than a small fix, so we can agree on the approach.
2. Keep each pull request to one change, with tests.
3. Make sure `python -m pytest` passes.
4. Sign the contributor agreement when the bot asks. See below.

## Contributor License Agreement

Before your first pull request is merged, you will be asked to agree to the
[Contributor License Agreement](CLA.md). You keep the copyright in your work. The agreement
gives the project the right to use and relicense your contribution, which keeps options
open for funding the project's future without having to track down every past contributor.
