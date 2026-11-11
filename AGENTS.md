# AGENTS.md: rules for coding agents working in this repo

This file is for automated coding agents (OpenHands and any others) that the owner, Kofi Gilbert,
brings in to help. Claude Code leads the project: it assigns work, reviews every agent pull request,
and is the only agent that merges. Read `CLAUDE.md` first; every rule in it applies to you.

## How work reaches you

- Work only on GitHub issues labelled `openhands`. Each issue states the scope, the files you may
  change, and the checks that must pass. Do not start work that has no issue.
- If an assignment comes from the owner directly, Claude Code will turn it into an issue; until then,
  ask the owner to confirm the scope with Claude Code.
- One issue, one branch, one pull request.

## Branches and pull requests

- Branch names start with `oh/` (for example `oh/ats-text-check`).
- Never push to `main`. Never merge, approve, or close a pull request, yours or anyone else's.
- Open pull requests as **drafts** with the `openhands` label. Mark ready for review when the
  checks below pass. Claude Code reviews, asks for changes in PR comments, and merges.
- Rebase on `main` before asking for review. Keep pull requests small and on one topic.
- Commit messages end with a `Co-Authored-By:` line naming your agent.

## Do not touch (Claude Code is actively changing these)

Unless your issue names them explicitly, do not edit:

- `tailoring/assembler.py` (the plan writer, fill, staircase and polish code)
- `tailoring/linefit.py`, `tailoring/inferred_support.py`, `tailoring/fact_gate.py`
- `ui/session.py`
- `agent/` (the chat agent and narrator)
- `llm/anthropic_client.py` and `llm/base.py` (prompts and the test double)
- `worker/` (the live broker; deploys are done by Claude Code only)

The list changes; the issue you are given is the source of truth for what you may edit.

## Every pull request must include

1. What changed and why, in plain language.
2. The tests you ran and their result. Run targeted tests (`pytest tests/<file>.py`), not the whole
   suite at once (CI runs it in 8 shards).
3. For anything a user sees: before and after screenshots.
4. Anything you could not finish, or are unsure about.

## Hard rules (from CLAUDE.md, repeated because they matter)

- No RED-lane code: no logged-in LinkedIn automation, no captcha solving, no stealth or
  bot-detection evasion, no proxies to dodge blocks. If a task seems to need any of these, stop and
  say so in the issue.
- Never commit personal data, credentials, `config/credentials*.env`, or anything under `data/`.
- Never use real AI calls or paid services in tests; use the test double (`RESUME_AGENT_DEV=1
  RESUME_AGENT_FAKE_LLM=1`). Ask in the issue before any real model run.
- User-facing text: write "resume" (never "CV" or "résumé"), and no em or en dashes.
- Resumes stay one page, with no new overfull boxes; a failing check rolls back, never ships.
