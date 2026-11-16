# Show HN post

Follows https://news.ycombinator.com/showhn.html: what it is, why, what was hard, what
feedback you want. No marketing. Reply to every comment.

## Title

Show HN: SponsorJobs, an open-source local job-hunt app for international students (visa data, one-page resume tailoring)

## URL

https://sponsorjobs.ai

## Text

I am an international student in the US. Job hunting here adds a filter most tools ignore: which employers actually sponsor. SponsorJobs is a free desktop app (Python engine plus an Electron shell, AGPL-3.0) that pulls jobs from public ATS feeds (Greenhouse, Lever, Ashby, Workday and others), tags every employer with its public H-1B, PERM and E-Verify record, flags "no sponsorship" postings, then gives each job a workspace where it tailors a one-page LaTeX resume to it, drafts the cover letter, and runs interview practice.

Everything you write stays on your machine. The AI is included through an account rather than an API key you paste, and the service behind it counts usage without seeing your documents. The community edition runs from source with your own key if you would rather have no account at all.

The three hard parts, technically:

1. One-page LaTeX that stays one page. The editor only rewrites text inside bullet macros, recompiles, and reads the log. Exit code 0 is not enough: it checks page count and new overfull hbox warnings against the baseline, shortens the offending bullet, and retries, rolling back to the last good source if it cannot converge. A newer check reads the finished PDF's text layer the way an applicant tracking system does, because a resume can compile perfectly and still arrive as unparseable noise.

2. Not making things up. A deterministic fact gate diffs every number in the output against the person's own material and flags figures with no source, and a per-role check discards any reword that claims a skill the role's own bullets never show.

3. Applying without lying about what we are. The agent fills application forms, and it submits only on sites whose own documented rules allow an applicant-run program to do so. That allowlist is maintained by hand with the evidence recorded next to each entry, and it is currently one ATS. Everywhere else the agent fills the form and stops at the submit button. No stealth headers, no captcha solving, no automating a logged-in LinkedIn account. The honest version of this product is slower than the dishonest one.

Feedback wanted: the sponsorship data model (I aggregate USCIS, DOL and E-Verify files), whether the anti-fabrication checks are strict enough, and whether anyone has found an ATS with a sanctioned, applicant-usable submission API, because I have looked hard and mostly found employer-key-gated ones.

Repo: https://github.com/KofiGilbert/sponsorjobs

(Word count: about 350. If it needs to be shorter, cut point 3 down to its first and last sentences.)
