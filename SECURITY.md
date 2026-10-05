# Security policy

SponsorJobs holds sensitive things on the user's machine: their work history, their AI API key,
and optionally access to their own mailbox. Please report security problems privately.

## Reporting a vulnerability

Use GitHub's private reporting: open the repository's **Security** tab and choose
**Report a vulnerability**. Please do not open a public issue.

Include what you found, how to reproduce it, and what an attacker could do with it. You
should get a reply within a week.

## In scope

- Leaking the user's API key, profile, résumés, or mail outside their machine.
- The local server at `127.0.0.1:57000` being reachable or exploitable from another
  machine or from a website the user visits.
- Anything that makes SponsorJobs submit an application, send email, or act on a site without
  the user's consent.

## Out of scope

- Problems that require an attacker who already controls the user's computer.
- Bugs in third-party job boards or AI providers.
