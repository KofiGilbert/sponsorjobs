"""Tailor's own lightweight autonomous form-filler (no browser-use, no stealth).

A narrow, privacy-first browser agent for the ONE job it needs: fill a job-application form on an
allowlisted, bot-permitted site, pause for the person to approve, then submit. It runs through our
EXISTING broker (a plain text prompt -> a JSON fill plan, no tool-calling or vision needed), drives a
VISIBLE browser via vanilla Playwright (or the person's own Chrome), and NEVER sees the person's real
personal data -- only placeholder keys, resolved locally at fill time. It obeys submit/policy +
submit/allowlist (only runs where automation is permitted) and drops to assisted apply on any captcha
or auth wall, never evading. See CLAUDE.md section 7.
"""

from autoapply.fields import (APPLICANT_KEYS, ConcreteOp, FillAction, FormField,
                              applicant_from_record, resolve_plan)
from autoapply.planner import build_fill_plan, parse_plan

__all__ = ["APPLICANT_KEYS", "FormField", "FillAction", "ConcreteOp",
           "applicant_from_record", "resolve_plan", "build_fill_plan", "parse_plan"]
