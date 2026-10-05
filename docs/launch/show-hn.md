# Show HN post

Follows https://news.ycombinator.com/showhn.html: what it is, why, what was hard, what
feedback you want. No marketing. Reply to every comment.

## Title

Show HN: SponsorJobs, an open-source local job-hunt app for international students (visa data, one-page résumé tailoring)

## URL

[[TODO: website URL, or the GitHub repo if the site is not up yet]]

## Text

I am an international student in the US. Job hunting here adds a filter most tools ignore: which employers actually sponsor. SponsorJobs is a free desktop app (Python engine + Electron shell, AGPL-3.0) that pulls jobs from public ATS feeds (Greenhouse, Lever, Ashby and others), tags every employer with its public H-1B, PERM and E-Verify record, flags "no sponsorship" postings, then tailors a LaTeX résumé to each job, drafts the cover letter, and runs interview practice. Data stays on your machine; the model runs on your own API key.

The two hard parts, technically:

1. One-page LaTeX that stays one page. The editor only rewrites text inside bullet macros, recompiles, and reads the log. Exit code 0 is not enough: it checks page count and new overfull hbox warnings against the baseline, shortens the offending bullet, and retries, rolling back to the last good source if it cannot converge.

2. Not making things up. A deterministic fact gate diffs every number in the output against the person's own material and flags figures with no source, and a per-role check discards any reword that claims a skill the role's own bullets never show.

Feedback wanted: the sponsorship data model (I aggregate USCIS, DOL and E-Verify files), and whether the anti-fabrication checks are strict enough. Repo: [[TODO: repo URL]]

(Word count: about 230. If it needs to be shorter, cut the first paragraph's feature list.)
