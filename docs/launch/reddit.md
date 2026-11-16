# Reddit post

Target: r/cscareerquestions (check the day's rules; tools posts may need the weekly thread)
or an international-students subreddit such as r/f1visa or r/immigration. Read each sub's
self-promotion rule first. Post as a person asking for feedback, disclose that you built
it, do not post the same text in several subs on the same day.

## Title

I built a free, open-source tool that tags job postings with the employer's H-1B / PERM / E-Verify record. Would this have helped you?

## Body

International student here. The thing that wasted most of my time was applying to companies that turned out not to sponsor. So I built a desktop app that pulls postings from public job boards and marks each employer with its sponsorship record from the public government data: USCIS H-1B, DOL PERM, and E-Verify. It also flags postings that say "no sponsorship" outright.

It does other things too. Each job gets its own workspace, where it rewrites your resume toward that posting's language using only material you gave it and keeps it to one page, drafts the cover letter, and runs interview practice. It will fill an application form for you, and on the small number of sites whose own rules permit programmatic submission it can send it; everywhere else it fills the form and stops, and you click submit yourself. I would rather it be useless on a site than break that site's terms.

The sponsor tagging is the part I want to ask about:

- Which fields would you actually look at? Approval counts, denial rate, the year, the job title?
- Is "cap-exempt" worth surfacing separately?
- What would make you distrust the badge?

On the boring but fair questions: it is AGPL-3.0 and runs on your own computer. Your resumes, profile and applications stay there. The AI is included through a SponsorJobs account so there is no API key to set up, and the only thing the service sees is your usage count, not your documents. Free gives you every job and 3 tailored applications a month; a pass is $29 once for 30 days if you are in a heavy stretch, and nothing auto-renews. If you want no limits and no account, the community edition runs from source with your own AI key.

Source and downloads: https://sponsorjobs.ai and https://github.com/KofiGilbert/sponsorjobs

Disclosure: I made this. Not selling anything here, and I am more interested in whether the sponsorship data is trustworthy than in downloads.
