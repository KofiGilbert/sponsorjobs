# SponsorJobs

**A free, open-source job-hunt agent for international students in the US, and anyone else
job hunting.** SponsorJobs finds jobs, flags which employers actually sponsor visas, rewrites your
résumé for each job without breaking its one-page layout, drafts the cover letter, and helps
you practise the interview. It runs on your own computer with your own AI key.

<!-- Demo: add a 60-90 second screen recording here before launch. -->

## Why this exists

Job hunting as an international student is the hard version of job hunting. You need
employers who sponsor, a résumé in the American one-page style, and answers ready for
US-style interviews. Most tools that help charge a monthly fee to people who often cannot
legally earn money yet. SponsorJobs is free so that the students who need it most can use it.

## What it does

1. **Find jobs.** Pulls postings from official public job boards (Greenhouse, Lever, Ashby,
   Workable, SmartRecruiters, Recruitee and others). Each employer is tagged with its
   visa-sponsorship record from public U.S. government data: H-1B, green card (PERM),
   E-Verify for STEM OPT, and cap-exempt status. Postings that say "no sponsorship" are
   flagged too.
2. **Tailor your résumé.** Rewords your real experience toward the job's language, keeps it
   on one page, and shows which of the job's keywords your résumé covers and which it
   misses. It never adds a skill to a role where you did not use it.
3. **Write the rest.** Drafts a cover letter and answers to screening questions from your
   own history.
4. **Practise.** A recorded, scored first-round screen, then a simulated interviewer that
   asks questions from the actual job description.
5. **Apply.** Fills application forms for you to review and submit. It never automates
   LinkedIn and never tries to get around a site's bot protection.

## Your data

- Your profile, résumés, and applications are stored only on your computer.
- To write text, SponsorJobs sends the relevant text to the AI provider you choose (Anthropic
  or OpenAI) using **your own API key**. SponsorJobs itself stores none of it.
- The jobs list comes from a shared feed of public job postings. Your profile is never
  sent to it.

## Install

### Windows

Download `SponsorJobs-Setup.exe` from the [Releases](../../releases) page and run it. Windows
may show a SmartScreen warning because the installer is not yet code-signed. Click
**More info**, then **Run anyway**.

You also need LaTeX, which SponsorJobs uses to typeset your résumé. Install
[MiKTeX](https://miktex.org/download) and let it install missing packages on the fly.

### macOS and Linux (from source for now)

You need Python 3.11 or newer, LaTeX, and Git.

**1. Install LaTeX.** On macOS the simplest option is the full MacTeX:

```bash
brew install --cask mactex-no-gui
```

For a smaller install, use BasicTeX and add the packages the templates need:

```bash
brew install --cask basictex
sudo tlmgr update --self
sudo tlmgr install bbm bbm-macros cm-super enumitem blindtext microtype listings xcolor
```

On Debian or Ubuntu:

```bash
sudo apt install texlive-latex-extra texlive-fonts-extra cm-super
```

**2. Install and run SponsorJobs.**

```bash
git clone https://github.com/KofiGilbert/sponsorjobs.git sponsorjobs
cd sponsorjobs
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[llm]"
python run_ui.py
```

SponsorJobs opens in your browser at `http://127.0.0.1:57000`. It is only reachable from your
own machine.

**Optional desktop window.** The desktop shell adds a built-in browser pane for applying.
It needs Node.js 20 or newer:

```bash
cd shell-electron
npm install
npm start
```

### Get an AI key

SponsorJobs needs an API key from [Anthropic](https://console.anthropic.com/) (recommended) or
[OpenAI](https://platform.openai.com/). The first-run setup asks for it, and it is stored
only on your computer. You pay the provider directly for what you use.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first. It covers setup,
tests, the project's hard rules, and the contributor agreement. For how the engine works,
see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

To report a security problem, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## License

SponsorJobs is free software under the [GNU Affero General Public License v3.0](LICENSE). You can
use, study, change, and share it. If you run a modified version as a service for other
people, you must share your changes under the same license. Third-party components and
their licenses are listed in [NOTICES.md](NOTICES.md).

Built by [Kofi Gilbert](https://github.com/KofiGilbert).
