"""Real bring-your-own-key Anthropic backend (CLAUDE.md §5).

Implements the same :class:`LLMBackend` methods as :class:`FakeLLM`, so the
tailoring engine and intake are identical in test and production. The key is
read from a git-ignored ``config/credentials.env`` (``ANTHROPIC_API_KEY``) and is
never written to the repo.

CLAUDE.md §5 asks for low-temperature, faithful rephrasing. On current Claude
models (Opus 4.8 / Sonnet 5) the ``temperature`` parameter has been removed and
is rejected with a 400, faithfulness is steered by the prompt and a low
``effort`` setting instead (this is mechanical rephrasing, not open-ended
reasoning), which is also cheaper and faster.

This module is import-safe without the ``anthropic`` package or a key installed;
it only fails when you actually construct the client. The Phase-1 acceptance
tests use :class:`FakeLLM` and never touch this path.
"""

from __future__ import annotations

import json
import re
import os
from pathlib import Path

# Cost-efficient default for tailoring: Sonnet 4.6 is high quality at ~40% less than Opus
# ($3/$15 vs $5/$25 per Mtok). Overridable via RESUME_AGENT_MODEL (bump to claude-opus-4-8
# for a premium tier, or drop to claude-haiku-4-5 for bulk/simple tasks at ~5x less).
DEFAULT_MODEL = "claude-sonnet-5-5"

# Models that accept `output_config.effort`. Written as the families that DO support it
# so an unrecognised future model degrades to "send no effort" -- a working request --
# rather than to a 400 that kills a whole auto-apply batch.
_EFFORT_CAPABLE_PREFIXES = (
    "claude-fable-", "claude-mythos-",
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-opus-4-5",
    "claude-sonnet-5", "claude-sonnet-4-6",
)


def supports_effort(model: str) -> bool:
    return str(model or "").startswith(_EFFORT_CAPABLE_PREFIXES)


def _cred_file() -> Path:
    """The single git-ignored credentials file, SAME resolution the web app uses when it
    SAVES the key (RESUME_AGENT_CRED_FILE override, else <repo>/config/credentials.env).
    Anchored at the repo root, not the CWD, so a key saved through the UI is actually the
    key the real model reads back, regardless of where the process was launched."""
    root = Path(__file__).resolve().parents[1]
    return Path(os.environ.get("RESUME_AGENT_CRED_FILE") or (root / "config" / "credentials.env"))


def _load_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    env_file = _cred_file()
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "ANTHROPIC_API_KEY":
                return v.strip().strip('"').strip("'")
    return None


_FILE_BLOCK = re.compile(r"^=== FILE: (.+?) ===[ \t]*\n(.*?)^=== END FILE ===[ \t]*$", re.S | re.M)


def parse_file_blocks(raw: str) -> list[dict]:
    """Files written between ``=== FILE: path ===`` and ``=== END FILE ===`` markers."""
    out = []
    for m in _FILE_BLOCK.finditer(raw or ""):
        content = m.group(2)
        out.append({"path": m.group(1).strip(), "content": content if content.endswith("\n") else content + "\n"})
    return out


class AnthropicLLM:
    """Production LLM backend. Constructed lazily so imports never require a key."""

    def __init__(
        self,
        model: str | None = None,
        effort: str = "low",
        api_key: str | None = None,
    ) -> None:
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise ImportError(
                "The 'anthropic' package is required for AnthropicLLM. "
                "Install it with: pip install anthropic"
            ) from exc
        from anthropic import Anthropic

        key = api_key or _load_key()
        if not key:
            raise RuntimeError(
                "No ANTHROPIC_API_KEY found. Set it in the environment or in "
                "config/credentials.env (git-ignored)."
            )
        self.model = model or os.environ.get("RESUME_AGENT_MODEL", DEFAULT_MODEL)
        # low effort: mechanical rephrasing, cheaper/faster, no deep reasoning.
        self.effort = effort
        self._client = Anthropic(api_key=key)

    # -- helpers --------------------------------------------------------- #
    def _complete(
        self, system: str, user: str, max_tokens: int = 1024, effort: str | None = None
    ) -> str:
        create = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        # `effort` is not universal: the smaller/older models reject it outright with
        # "This model does not support the effort parameter", a 400 that takes down the
        # whole call. It only tunes reasoning depth, so a model that cannot take it should
        # simply run without it rather than fail. Same guard as the broker-side provider.
        if supports_effort(self.model):
            create["output_config"] = {"effort": effort or self.effort}
        try:
            msg = self._client.messages.create(**create)
        except Exception as exc:
            if "output_config" in create and "effort" in str(exc).lower():
                create.pop("output_config", None)
                msg = self._client.messages.create(**create)
            else:
                raise
        # Token counts for callers that log usage (an AI-built project logs its build's total).
        u = getattr(msg, "usage", None)
        self.last_usage = ({"input": int(getattr(u, "input_tokens", 0) or 0),
                            "output": int(getattr(u, "output_tokens", 0) or 0)} if u else None)
        return "".join(
            block.text for block in msg.content if getattr(block, "type", "") == "text"
        ).strip()

    # -- LLMBackend ------------------------------------------------------ #
    def generate_intake_questions(
        self, jd_text: str, saved_profile: dict | None
    ) -> list[str]:
        system = (
            "You help a job applicant. Read the job description and ask ONLY the "
            "questions relevant to THIS role: which of the person's experiences "
            "and projects to feature, and the identity/date fields the JD can't "
            "supply. Do not ask a fixed generic questionnaire. Return a JSON "
            "array of question strings."
        )
        saved = json.dumps(saved_profile) if saved_profile else "(none saved yet)"
        user = f"JOB DESCRIPTION:\n{jd_text}\n\nSAVED PROFILE:\n{saved}"
        raw = self._complete(system, user)
        try:
            # Slice to the outermost [...] so a code-fenced or prose-wrapped array still parses
            # (same tolerance the other array parsers use). A bare json.loads(raw) fails on a
            # ```json ... ``` reply and used to leak the fence line in as a "question".
            data = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
            if isinstance(data, list):
                return [str(q).strip() for q in data if str(q).strip()]
        except (json.JSONDecodeError, ValueError):
            pass
        # Fall back to line-splitting if the model returned no usable array. Drop any stray
        # code-fence lines so they never surface as a question.
        return [ln.strip("-• ").strip() for ln in raw.splitlines()
                if ln.strip() and not ln.strip().startswith("```")]

    def reword_bullet(
        self,
        original_text: str,
        supported_jd_terms: list[str],
        target_len_chars: int,
        jd_text: str,
    ) -> str:
        system = (
            "You rewrite ONE resume bullet for a specific job. Keep it concrete, plain and "
            "recruiter-grade: what was done, with what, for whom, with what measurable result "
            "when the original gives one.\n"
            "Rules:\n"
            f"- ONE complete sentence of at most {target_len_chars} characters. Shorter is "
            "fine. Never pad to reach a length: a short true sentence beats a long vague one.\n"
            "- NO filler. Do not add clauses like 'demonstrating analytical rigor', 'essential "
            "to successful delivery outcomes', 'ensuring stakeholder alignment', 'leveraging', "
            "'robust', 'comprehensive', 'seamless'. If a phrase cannot be checked by a "
            "reference call, leave it out.\n"
            "- Keep every fact, number and name from the original exactly; you may reorder "
            "and sharpen, not embellish.\n"
            "- HARD LIMIT on invention: never fabricate VERIFIABLE facts, do not add "
            "specific numbers, percentages, dollar amounts, dates, team sizes, employer "
            "or product/system names the original didn't state. Keep every real metric "
            "verbatim. Qualitative professional context is fine; fake precision is not.\n"
            "- HARD LIMIT on domain: never introduce a skill, tool, technology, domain, "
            "or field of work the ORIGINAL bullet does not already demonstrate. Reword "
            "WITHIN the bullet's real domain only. A banking, operations, sales, or "
            "research bullet must NOT be reframed as AI, ML, LLM, or software-engineering "
            "work it did not involve, even when the target job is about those, that is "
            "fabrication and it defeats the résumé. Mirror the job's vocabulary ONLY where "
            "the original's own content genuinely supports it.\n"
            "- Open with a specific, varied action verb. Do NOT reuse the same "
            "opening verb across bullets (avoid starting everything with "
            "'Architected'/'Led'/'Built').\n"
            "- Mirror the job's language where it fits naturally: weave in one or two "
            "relevant terms from the list, only where the bullet genuinely supports "
            "them. NO keyword walls, no slash-stacked acronym piles, no term stuffing.\n"
            "Return ONLY the rewritten bullet text, no quotes, no markup, no lead-in."
        )
        terms = ", ".join(supported_jd_terms) if supported_jd_terms else "(none)"
        user = (
            f"ORIGINAL BULLET:\n{original_text}\n\n"
            f"RELEVANT JOB TERMS (use at most 1-2, only if they fit):\n{terms}\n\n"
            f"TARGET JOB (context, do not copy verbatim):\n{jd_text[:1500]}"
        )
        out = self._complete(system, user, max_tokens=400, effort="medium")
        return out or original_text

    def shorten_bullet(self, text: str, max_len_chars: int) -> str:
        system = (
            "Shorten this résumé bullet so it fits a tighter space, while keeping "
            "its key achievement and any metrics.\n"
            f"- The result MUST be ONE complete, grammatical sentence within "
            f"{max_len_chars} characters. Never cut off mid-sentence or trail off "
            "with a dangling word or preposition.\n"
            "- If you must drop detail, drop whole clauses cleanly, not word "
            "fragments.\n"
            "Return ONLY the shortened bullet text, no quotes or markup."
        )
        out = self._complete(system, text, max_tokens=300)
        return out or text

    def keyword_action(self, term: str, jd_text: str, profile: dict, entries: list[dict],
                       allow_new: bool) -> dict:
        """One missing JD term: is it genuinely in the person's material, and if so, one
        honest edit that puts it on the page (tailoring/keyword_action.py validates all of
        this again deterministically, so the model's answer is a proposal, never the law)."""
        system = (
            "You help a person put ONE job-ad keyword on their resume, honestly.\n"
            "You are given the keyword, the job ad, the person's own profile, and the roles "
            "or projects on their one-page resume with their bullets (each bullet has an id).\n"
            "First decide: does the person's OWN material genuinely show this keyword, in the "
            "SAME role or project as a bullet you would change? Evidence must be a VERBATIM "
            "quote from their profile (it is checked word for word). A keyword that is only in "
            "a skills list, or only in a different role, is NOT support for a bullet.\n"
            "If it is supported, draft ONE of:\n"
            "- \"rewrite\": reword the single most relevant existing bullet of that entry so it "
            "carries the keyword naturally, in the job ad's own wording. Keep the SAME length, "
            "within about ten percent of the original, and keep every real fact. This is "
            "re-emphasis, not expansion.\n"
            + ("- \"new\": a new one-line bullet (at most 160 characters) under that entry, "
               "ONLY when no existing bullet of the entry can carry it. Set target to the id "
               "of the last bullet of that entry.\n" if allow_new else
               "The page has no room for a new line, so you may only rewrite.\n")
            + "Hard rules: never add a number, percentage, amount, date, team size, employer or "
            "product the profile does not state. Never add any other skill or tool the entry "
            "does not show. No filler phrases (demonstrating, leveraging, robust, seamless). "
            "No em dashes or en dashes anywhere. Plain prose, no markup.\n"
            "Return ONLY JSON: {\"supported\": true|false, \"reason\": \"one plain line\", "
            "\"evidence\": {\"label\": \"role or project\", \"quote\": \"verbatim text\", "
            "\"section\": \"experience|projects|extracurricular\"}, "
            "\"kind\": \"rewrite\"|\"new\", \"target\": \"bullet id\", \"draft\": \"the bullet\"}. "
            "When not supported, return {\"supported\": false, \"reason\": \"...\"} only."
        )
        shown = []
        for e in entries or []:
            lines = [f"ENTRY: {e.get('label', '')} ({e.get('section', '')})",
                     f"  context: {str(e.get('context') or '')[:1200]}"]
            for b in e.get("bullets") or []:
                lines.append(f"  [{b.get('id')}] {b.get('text')}")
            shown.append("\n".join(lines))
        compact = {k: profile.get(k) for k in ("summary", "experience", "projects", "skills",
                                                "extracurricular", "certifications")
                   if profile.get(k)}
        user = (f"KEYWORD: {term}\n\n"
                f"JOB AD (context):\n{(jd_text or '')[:2500]}\n\n"
                f"THE PERSON'S PROFILE:\n{json.dumps(compact, ensure_ascii=False)[:9000]}\n\n"
                f"BULLETS ON THE PAGE:\n" + "\n\n".join(shown))
        raw = self._complete(system, user, max_tokens=700, effort="medium")
        data = self._parse_json_object(raw) or {}
        if not isinstance(data, dict):
            return {"supported": False, "reason": "The model gave no usable answer."}
        return data

    def extract_jd_skills(self, jd_text: str) -> list[str]:
        """Model-read skill list for the coverage report; falls back to the curated vocabulary
        when the model is unavailable or answers badly, so the report never goes blank."""
        import json as _json

        from tailoring.keywords import skill_terms
        system = (
            "Read this job ad and list ONLY the concrete, checkable things it asks a candidate "
            "to have, in the ad's own wording: hard skills, tools, software, programming "
            "languages, methods and frameworks, certifications and licences, degrees or fields "
            "of study when named as requirements.\n"
            "Do NOT list: job titles or role words (analyst, architect, manager), soft skills "
            "(communication, leadership), duties or verbs (manages, mentors, engages), company "
            "or product names, locations, benefits, or generic words.\n"
            "Order by importance to the ad (required before nice-to-have). At most 25 items. "
            "Each item 1 to 4 words. Return ONLY a JSON array of strings, nothing else."
        )
        fallback = skill_terms(jd_text)
        try:
            out = self._complete(system, jd_text[:12000], max_tokens=600)
            start, end = out.find("["), out.rfind("]")
            items = _json.loads(out[start:end + 1]) if start != -1 and end > start else []
        except Exception:                                 # noqa: BLE001 - model or parse trouble
            return fallback
        seen: set[str] = set()
        skills: list[str] = []
        flat: list[str] = []
        for it in items:
            # "Tableau or Power BI" is two skills; checked as one phrase it matched neither,
            # and the report listed it as missing from a profile that had both.
            flat.extend(re.split(r"\s+(?:or|and|/)\s+|\s*/\s*", str(it)))
        for it in flat:
            t = str(it).strip().strip(".,;:")
            if not (1 <= len(t.split()) <= 4) or len(t) > 40:
                continue
            if t.lower() in seen:
                continue
            seen.add(t.lower())
            skills.append(t)
        return skills[:25] or fallback

    def expand_bullets(
        self, role_title: str, bullets: list[str], jd_text: str
    ) -> list[str]:
        system = (
            "You turn a person's raw notes for ONE résumé role into strong, complete, "
            "page-width bullet points. People type terse fragments ('Built SQL "
            "reporting pipelines') or cram several distinct accomplishments into one "
            "comma-list ('built SQL pipelines, Tableau dashboards, and automated "
            "reports'), both read as weak, stubby bullets.\n"
            "Produce ONE complete sentence PER DISTINCT accomplishment in the input:\n"
            "- SPLIT a merged comma/and list into separate bullets, one per real "
            "accomplishment.\n"
            "- Write each as a plain, concrete sentence of 90 to 170 characters: what was "
            "done, with what tools or methods, for whom or at what scale, and the result "
            "WHEN the notes state one. Start with a specific, varied action verb.\n"
            "- Say ONLY what the notes support. Do not add purposes, outcomes, "
            "responsibilities or qualities that 'plausibly follow'; do not pad with words "
            "like robust, comprehensive, seamless, ensuring, enabling, leveraging, "
            "demonstrating, or 'cross-functional alignment'. A shorter true sentence beats "
            "a longer vague one.\n"
            "- KEEP any bullet that is already a full, distinct sentence; only tighten it.\n"
            "- Use the target job's vocabulary only where the notes genuinely support it.\n"
            "- Keep every real metric the input states verbatim.\n"
            "Return ONLY a JSON array of bullet strings, most impressive first."
        )
        user = (
            f"ROLE TITLE: {role_title}\n\n"
            f"RAW BULLETS / NOTES:\n" + "\n".join(f"- {b}" for b in bullets) + "\n\n"
            f"TARGET JOB (context, do not copy verbatim):\n{jd_text[:1500]}"
        )
        out = self._complete(system, user, max_tokens=800, effort="medium")
        if out and "[" in out and "]" in out:
            try:
                data = json.loads(out[out.find("["): out.rfind("]") + 1])
                cleaned = [str(b).strip() for b in data if str(b).strip()]
                if cleaned:
                    return cleaned
            except (json.JSONDecodeError, TypeError):
                pass
        return [str(b).strip() for b in bullets if str(b).strip()]

    def ask_missing(self, missing: list[str], answered: list[str], role: str = "",
                    declined: list[str] | None = None) -> str:
        system = (
            "You are a sharp, warm resume coach mid-conversation, sound like a helpful "
            "friend, not a form. You still need a few details to build the person's resume. "
            "Write ONE short, natural chat message (1-2 sentences) that:\n"
            "- briefly and ACCURATELY acknowledges their last response by NAMING what "
            "they gave, not with filler. CRITICAL: the response may include things they "
            "DON'T have (see DECLINED). NEVER praise a declined item as if they provided "
            "it, do not say 'those links will round out your profile' when they said "
            "they have no links. Acknowledge a decline neutrally ('no problem, we'll skip "
            "those') or just move on.\n"
            "- then asks for AT MOST TWO of the still-missing items from MISSING, "
            "grouping naturally where it reads better;\n"
            "- sounds like a person, not a form: no bullet lists, no 'I need the "
            "following'. BANNED robotic openers: 'Perfect , ', 'Got it', 'Great!', "
            "'Awesome', 'absolutely', 'no worries', 'for sure', vary your phrasing.\n"
            "Only ask for items in MISSING. Return ONLY the message text."
        )
        user = (f"ROLE they're applying to: {role or '(unknown)'}\n\n"
                f"JUST PROVIDED (acknowledge briefly, don't restate): {answered or '(nothing)'}\n\n"
                f"JUST DECLINED / don't have (acknowledge neutrally, NEVER praise): "
                f"{declined or '(none)'}\n\n"
                f"MISSING (ask for at most TWO of these): {missing}")
        try:
            out = self._complete(system, user, max_tokens=200, effort="low")
        except Exception:
            out = ""
        return out or ""

    def ask_enrich(self, topics: list[str], answered: list[str], role: str = "",
                   guidance: dict | None = None, person: dict | None = None) -> str:
        style = (guidance or {}).get("style") or (
            "Talk like a warm, curious friend catching up, never a form. Open "
            "questions, brief affirmations, reflect back what they say. Go broad "
            "first, then narrow. Never invent.")
        system = (
            "You are a sharp, warm resume coach drawing out a person's REAL material to fill "
            "their resume to a full, template-matching page. Sound like a friend who's into "
            "their story, not a corporate form. INTERVIEW STYLE:\n" + style +
            "\nVOICE: 1-2 short sentences; ONE ask (or at most two closely-related "
            "topics); acknowledge what they just shared by NAMING it, not with filler. "
            "BANNED openers (robotic): 'Perfect , ', 'Got it', 'Great!', 'Awesome', "
            "'absolutely', 'no worries', 'for sure', 'to be honest', a bare 'I "
            "understand', vary your phrasing.\n"
            "Write ONE short, natural chat message (1-2 sentences) that:\n"
            "- if ANSWERED is non-empty, warmly and briefly acknowledges what they "
            "just added (don't restate it verbatim);\n"
            "- then invites AT MOST TWO of the TOPICS. You MUST ask about the given "
            "TOPICS specifically, do NOT introduce a different field (e.g. never ask "
            "for a phone number here) and do NOT say the résumé is finished or that "
            "you 'have everything', there is more to draw out. For a topic that "
            "carries a GOAL and example LEADS, ask INDIRECTLY in your own words in "
            "that spirit (e.g. draw out extracurriculars/interests by asking what they "
            "do outside work or could talk about for hours), never say 'list your "
            "extracurriculars'; use the leads as inspiration, do NOT recite them.\n"
            "- makes clear it's optional: they can skip any topic or say \"that's "
            "everything\" to finalize, and you will never invent anything.\n"
            "No bullet lists, no walls of questions. Return ONLY the message text."
        )
        gtopics = (guidance or {}).get("topics") or {}
        detail = "\n".join(
            f"- {k}: goal = {v.get('goal','')}; example leads = "
            f"{' | '.join(v.get('leads') or [])}" for k, v in gtopics.items())
        # WHO we are asking. Without this the interviewer knew only the target role, so it
        # could only ask generic questions ("tell me an accomplishment"), and a generic
        # question unlocks nothing: the page sat at half full while the person had plenty
        # to say. This is the alternative to a bullet GENERATOR, which would write the
        # claim itself and invite a tired applicant to accept it. A question cannot
        # fabricate; it just has to be worth answering.
        who = ""
        if person:
            lines = []
            for r in (person.get("roles") or []):
                said = "; ".join(r.get("already_said") or []) or "(nothing yet)"
                lines.append(f"- {r.get('title','?')} at {r.get('org','?')} "
                             f"({r.get('dates') or 'dates unknown'}). Already on the resume: {said}")
            edu = ", ".join(f"{d.get('degree','')} at {d.get('school','')}".strip()
                            for d in (person.get("education") or []))
            who = ("\n\nWHO YOU ARE TALKING TO"
                   + (f" (first name: {person['name']})" if person.get("name") else "")
                   + ":\n" + ("\n".join(lines) or "- (no roles on file yet)")
                   + (f"\nEducation: {edu}" if edu else ""))
        user = (f"ROLE they're applying to: {role or '(unknown)'}\n\n"
                f"ANSWERED (just added, acknowledge briefly): {answered or '(nothing yet)'}\n\n"
                f"TOPICS to draw out (pick at most TWO): {topics}\n\n"
                f"TOPIC GUIDANCE (phrase indirectly, don't recite):\n{detail or '(none)'}"
                f"{who}\n\n"
                "GROUND THE QUESTION IN THEIR OWN HISTORY. Name the actual employer, role "
                "or degree above so it is a question only THEY could be asked, never a "
                "generic 'tell me about an accomplishment' (that asks them to search their "
                "whole life and they answer with nothing). Ask about something NOT already "
                "on the resume above. Never suggest what the answer might be, never propose a "
                "metric or an achievement for them to confirm: you are jogging their memory, "
                "not writing their resume. Their words only.")
        try:
            return self._complete(system, user, max_tokens=240, effort="low") or ""
        except Exception:
            return ""

    def absorb_enrichment(self, jd_text: str, essentials: dict, message: str) -> dict:
        system = (
            "The person is adding REAL detail to their résumé. From their message, "
            "extract only what they actually state and return it as JSON to merge:\n"
            "- bullets_by_org: object mapping a company name they already listed to a "
            "list of 1-3 accomplishment bullets (complete sentences, keep their "
            "metrics, invent nothing);\n"
            "- projects: list of real projects {org, title, location, dates, bullets};\n"
            "- skills: list of skill strings they claim;\n"
            "- certifications: list of certification strings;\n"
            "- extracurricular: list of {title, date, bullets};\n"
            "- interests: a one-line string.\n"
            "Omit any field they didn't address. Never fabricate. The known company "
            "names are: " + json.dumps([j.get("org", "") for j in essentials.get("experience", [])]) +
            ". Return ONLY the JSON object."
        )
        try:
            raw = self._complete(system, f"MESSAGE:\n{message}", max_tokens=1200, effort="medium")
            data = self._parse_json_object(raw) or {}
        except Exception:
            data = {}
        e = essentials
        bybo = data.get("bullets_by_org") or {}
        if isinstance(bybo, dict):
            for j in e.get("experience", []) or []:
                org = str(j.get("org") or "").strip()
                hit = next((v for k, v in bybo.items() if k.strip().lower() == org.lower()), None)
                if isinstance(hit, list) and hit:
                    existing = [str(b).strip() for b in (j.get("bullets") or []) if str(b).strip()]
                    for b in hit:
                        b = str(b).strip()
                        if b and b not in existing:
                            existing.append(b)
                    j["bullets"] = existing[:4]
        if isinstance(data.get("projects"), list):
            projs = e.setdefault("projects", [])
            for p in data["projects"]:
                if isinstance(p, dict) and (p.get("org") or p.get("title")):
                    p.setdefault("location", "Personal Project")
                    p.setdefault("bullets", [])
                    p["suggested"] = False
                    projs.append(p)
        if isinstance(data.get("skills"), list) and data["skills"]:
            e.setdefault("skills_input", [])
            for s in data["skills"]:
                s = str(s).strip()
                if s and s not in e["skills_input"]:
                    e["skills_input"].append(s)
        if isinstance(data.get("certifications"), list) and data["certifications"]:
            e.setdefault("certifications", [])
            for c in data["certifications"]:
                c = str(c).strip()
                if c and c not in e["certifications"]:
                    e["certifications"].append(c)
        if isinstance(data.get("extracurricular"), list) and data["extracurricular"]:
            e.setdefault("extracurricular", [])
            e["extracurricular"].extend(x for x in data["extracurricular"] if isinstance(x, dict))
        if str(data.get("interests") or "").strip() and not str(e.get("interests") or "").strip():
            e["interests"] = str(data["interests"]).strip()
        return e

    def select_courses(self, jd_text: str, transcript_text: str) -> list[str]:
        system = (
            "You are given the raw text of a person's academic transcript. Extract the "
            "REAL course titles they actually took, then return ONLY the ones most "
            "relevant to the target job. Do not invent courses; only select and clean "
            "up titles that appear in the transcript (drop course codes, grades, and "
            "credit numbers). Return at most 8, most-relevant first, as a JSON array "
            "of course-title strings and nothing else."
        )
        user = f"TARGET JOB:\n{jd_text[:1500]}\n\nTRANSCRIPT TEXT:\n{transcript_text[:6000]}"
        raw = self._complete(system, user, max_tokens=500, effort="medium")
        try:
            data = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
            if isinstance(data, list):
                return [str(c).strip() for c in data if str(c).strip()][:8]
        except (json.JSONDecodeError, ValueError):
            pass
        return []

    def suggest_projects(self, jd_text: str) -> list[dict]:
        system = (
            "Propose 1-2 concrete, buildable projects that would qualify a candidate "
            "for the target job (e.g. 'build a small credit-risk model on public data "
            "and put it on GitHub'). These are SUGGESTIONS the person has not built "
            "yet. Return ONLY a JSON array of objects: "
            "{\"org\": short project name, \"location\": \"Personal Project\", "
            "\"dates\": a plausible recent year, \"how\": one sentence on how to build "
            "it, \"bullets\": [one résumé-style bullet]}. Keep them realistic and small."
        )
        raw = self._complete(system, f"TARGET JOB:\n{jd_text[:1500]}", max_tokens=600, effort="medium")
        try:
            data = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
        except (json.JSONDecodeError, ValueError):
            return []
        out = []
        for p in data if isinstance(data, list) else []:
            if isinstance(p, dict) and (p.get("org") or p.get("title")):
                p["suggested"] = True                     # single source of truth
                p.setdefault("location", "Personal Project")
                p.setdefault("bullets", [])
                out.append(p)
        return out[:2]

    def _complete_history(self, system, history, max_tokens=1500, effort=None):
        """Complete with a multi-turn message history (role user/assistant)."""
        messages = []
        for h in history:
            role = "assistant" if h.get("role") == "agent" else "user"
            content = h.get("content", "")
            if content:
                messages.append({"role": role, "content": content})
        if not messages or messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "(start)"})
        msg = self._client.messages.create(
            model=self.model, max_tokens=max_tokens,
            output_config={"effort": effort or self.effort},
            system=system, messages=messages,
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()

    def extract_intake(self, jd_text: str, known: dict, history: list[dict]) -> dict:
        system = (
            "You are a sharp, warm resume coach talking with a job applicant, sound like a "
            "helpful friend who's genuinely into their story, never a corporate form.\n"
            "VOICE (this governs the 'reply' field):\n"
            "- Keep the reply to 1-3 short sentences, and ask about ONE thing at a time, "
            "never dump a list of fields.\n"
            "- Acknowledge what they just said by NAMING it specifically ('a SPAN risk "
            "model at CME, that's strong'), not with empty filler. Use their FIRST name "
            "once you know it, but don't overuse it.\n"
            "- When you ask for something, add a half-line on WHY it helps ('so I can put "
            "your real numbers on the page instead of filler').\n"
            "- BANNED openers/filler (they read as a robot or a script): 'Perfect , ', "
            "'Got it', 'Great!', 'Awesome', 'absolutely', 'no worries', 'for sure', 'to be "
            "honest', 'as I mentioned', and a bare 'I understand'. Vary your phrasing; "
            "never reuse the same opener two turns running.\n\n"
            "Read the target job below. You are "
            "collecting their real background: identity (name, email, phone, "
            "address, LinkedIn/GitHub/blog links), education (school, degree, dates), "
            "and work history (company, title, employment dates, and the REAL bullet "
            "points / accomplishments listed under each role).\n"
            "People answer naturally and messily, several degrees in one message, "
            "a link on the wrong line, casual phrasing. Understand it, EXTRACT the "
            "fields, and MERGE them into what you already know. Ask a short, warm "
            "follow-up ONLY for what's genuinely missing or ambiguous, never "
            "interrogate field by field, and accept multiple items at once. If a URL "
            "appears, treat it as a profile link (linkedin/github/blog). Put the "
            "person's FULL mailing address (street, city, state, ZIP) in "
            "identity.address, the template header shows the whole address, not just "
            "a city. Keep every company, "
            "employment date, school, degree, AND their existing bullet points EXACTLY "
            "as written, copy real bullets verbatim, never paraphrase or invent them.\n"
            "Set ready=true once you have their name, at least one degree, and at "
            "least one job, or if they say to just build it.\n\n"
            f"TARGET JOB:\n{jd_text[:2500]}\n\n"
            f"ALREADY KNOWN (merge new info into this):\n{json.dumps(known)}\n\n"
            "Return ONLY a JSON object: {\"essentials\": {\"identity\": {name, email, "
            "phone, address, linkedin, github, blog}, \"education\": [{school, degree, "
            "date, location}], \"experience\": [{org, title, dates, location, bullets: "
            "[verbatim real bullet strings, or [] if the source listed none]}]}, "
            "\"reply\": \"your short chat reply\", \"ready\": true|false}. essentials "
            "must be the FULL merged set; preserve bullets already present in ALREADY KNOWN."
        )
        # Run extraction over the conversation as a TRANSCRIPT (a pure extraction
        # task), not as a chat to continue, otherwise a leading assistant greeting in
        # the history makes the model reply conversationally and return nothing.
        transcript = "\n".join(
            f"{'Applicant' if h.get('role') != 'agent' else 'Assistant'}: {h.get('content', '')}"
            for h in (history or []) if str(h.get("content") or "").strip())
        raw = self._complete(system, "CONVERSATION SO FAR:\n" + transcript +
                             "\n\nExtract everything the applicant has stated and return the JSON now.",
                             max_tokens=1500, effort="medium")
        data = self._parse_json_object(raw)
        if not isinstance(data, dict) or "essentials" not in data:
            # Never dead-end on a parse hiccup: if we already have enough to build,
            # proceed; otherwise ask for more without an error tone.
            have_enough = bool(known.get("experience")
                               and (known.get("identity") or {}).get("name"))
            reply = ("Let me build from what I already have."
                     if have_enough else
                     "Tell me a bit more about your background, your name, schools, and roles.")
            return {"essentials": known, "reply": reply, "ready": have_enough}
        ess = data.get("essentials") or known
        for k in ("identity", "education", "experience"):
            ess.setdefault(k, known.get(k, {} if k == "identity" else []))
        return {"essentials": ess, "reply": str(data.get("reply", "")),
                "ready": bool(data.get("ready"))}

    def converse(self, jd_text: str, history: list[dict], recalled: list[dict],
                 facts: list[dict]) -> str:
        """A free-form conversational reply, so the person can chat like they would with a
        collaborator that remembers everything about them (P1, Task 1). Grounded in their full
        history, the recalled memories, and the distilled facts; never invents facts about them."""
        system = (
            "You are the applicant's career copilot in an ongoing chat. Reply naturally to what "
            "they just said, like a sharp, warm collaborator who genuinely remembers them. Use "
            "their history, the recalled memories, and the known facts as context, and note "
            "briefly what you'll remember when they share something new. NEVER invent facts about "
            "them. If they ask you to build or tailor a resume, say you're on it. Be concise and "
            "genuine, no filler, no clichés."
        )
        from tailoring.conform import strip_ai_dashes
        convo = "\n".join(f"[{t.get('role')}] {t.get('content')}" for t in (history or [])[-12:])
        mem = "\n".join(f"- {r.get('text', '')}" for r in (recalled or [])[:6])
        fx = "\n".join(f"- ({f.get('type')}) {f.get('key')}: {f.get('value')}"
                       for f in (facts or [])[:20])
        user = (f"TARGET JOB (context):\n{(jd_text or '')[:1200]}\n\n"
                f"RECALLED MEMORIES:\n{mem or '(none)'}\n\n"
                f"KNOWN FACTS:\n{fx or '(none)'}\n\n"
                f"RECENT CONVERSATION:\n{convo}")
        return strip_ai_dashes(self._complete(system, user, max_tokens=500, effort="medium").strip())

    def distill_memory(self, history: list[dict], known_keys: list[str]) -> dict:
        """Distill NEW durable structured memory from the chat into a facts store (P1, Task 2):
        facts (skills/projects/achievements/dates), preferences (targeting fintech, remote), and
        constraints (do not mention X, needs sponsorship). Only what the person actually said."""
        system = (
            "You extract DURABLE structured memory from a career chat, to store as rows. Distill "
            "only NEW items the person ACTUALLY stated (never infer or invent):\n"
            "- type 'fact': a concrete skill, project, achievement, role, date, or credential. A form answer "
            "they state (desired salary, earliest start date, notice period, years of experience) is a "
            "'fact' with key desired_salary, start_date, notice_period or years_experience.\n"
            "- type 'preference': something they want (targeting fintech, prefers remote).\n"
            "- type 'constraint': something to ENFORCE on the resume (do not mention the 2021 gap; "
            "needs visa sponsorship).\n"
            "Separately, list in 'rules' each CHOICE they state about which of their own roles or projects "
            "to show for a KIND of job ('for banks, show Stanbic, not Viva'; 'lead with BlackOrigin for "
            "startups'; 'never show both 2020 jobs together'), only from their LAST message. Each rule: "
            "'action' (show, hide, lead, prefer, apart), 'items' (the names exactly as they said them; for "
            "prefer, the one to show first), 'scope' (their words for the kind of job, '' for every job).\n"
            "Each item needs a short STABLE snake_case 'key' so the same item updates in place "
            "(e.g. 'target_industry', 'skill_python', 'avoid_2021_gap'), a concise 'value', and a "
            "'confidence' 0-1. Skip anything already in KNOWN KEYS unless the value changed. If "
            "there is nothing new, return empty lists. Return ONLY JSON: {\"facts\":[{\"type\":"
            "\"...\",\"key\":\"...\",\"value\":\"...\",\"confidence\":0.9}], \"rules\":[{\"action\":"
            "\"prefer\",\"items\":[\"...\",\"...\"],\"scope\":\"...\"}]}."
        )
        convo = "\n".join(f"[{t.get('role')}] {t.get('content')}" for t in (history or [])[-16:])
        user = (f"KNOWN KEYS (already stored, skip unless changed):\n{', '.join(known_keys) or '(none)'}"
                f"\n\nCONVERSATION:\n{convo}")
        raw = self._complete(system, user, max_tokens=900, effort="medium")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes
        out = []
        for f in (data.get("facts") or []):
            if not isinstance(f, dict):
                continue
            key = str(f.get("key", "")).strip().lower()
            ftype = str(f.get("type", "")).strip().lower()
            if not key or ftype not in ("fact", "preference", "constraint"):
                continue
            try:
                conf = max(0.0, min(1.0, float(f.get("confidence", 0.8))))
            except (TypeError, ValueError):
                conf = 0.8
            out.append({"type": ftype, "key": key,
                        "value": strip_ai_dashes(str(f.get("value", "")).strip()), "confidence": conf})
        # Choices the person stated: names and scope words only. Code resolves them to their own
        # items and saves nothing until they tap Save (applicant/rules.py).
        rules = []
        for r in (data.get("rules") or [])[:4]:
            if not isinstance(r, dict) or str(r.get("action", "")).lower() not in ("show", "hide", "lead", "prefer", "apart"):
                continue
            rules.append({"action": str(r.get("action")).lower(),
                          "items": [str(x)[:80] for x in (r.get("items") or [])][:2],
                          "scope": str(r.get("scope") or "")[:80]})
        return {"facts": out, "rules": rules}

    def select_for_cv(self, jd_text: str, profile: dict, recalled: list[dict],
                      preferences: list[dict]) -> dict:
        """Decide what belongs on a one-page CV for THIS job (P1, Task 3): select the person's
        experiences/projects/skills that earn a place and say briefly why, honoring every stated
        preference/constraint exactly (an active 'do not mention X' constraint must be EXCLUDED)."""
        system = (
            "You decide what belongs on a ONE-PAGE résumé for a SPECIFIC job. Given the person's "
            "own material (experiences, projects, skills), recalled memories, and their stated "
            "PREFERENCES/CONSTRAINTS, SELECT the items that earn a place for THIS job and briefly "
            "say why; list what you would leave off and why. HONOR every constraint exactly: if a "
            "constraint says not to mention something, it must appear in 'excluded', never "
            "'selected'. Respect preferences (e.g. foreground fintech-relevant work). Never invent "
            "material. Return ONLY JSON: {\"selected\":[{\"item\":\"...\",\"why\":\"...\"}], "
            "\"excluded\":[{\"item\":\"...\",\"why\":\"...\"}], \"summary\":\"one honest line\"}."
        )
        prefs = "\n".join(f"- ({p.get('type')}) {p.get('value') or p.get('key')}"
                          for p in (preferences or []))
        mem = "\n".join(f"- {r.get('text', '')}" for r in (recalled or [])[:8])
        user = (f"TARGET JOB:\n{(jd_text or '')[:2000]}\n\n"
                f"PREFERENCES / CONSTRAINTS (honor exactly):\n{prefs or '(none)'}\n\n"
                f"RECALLED MEMORIES:\n{mem or '(none)'}\n\n"
                f"PERSON'S MATERIAL (JSON):\n{json.dumps(profile)[:5000]}")
        raw = self._complete(system, user, max_tokens=1200, effort="medium")
        data = self._parse_json_object(raw)
        from tailoring.conform import strip_ai_dashes
        if not isinstance(data, dict):
            return {"selected": [], "excluded": [], "summary": ""}

        def _clean(items):
            out = []
            for it in (items or []):
                if isinstance(it, dict) and str(it.get("item", "")).strip():
                    out.append({"item": strip_ai_dashes(str(it["item"]).strip()),
                                "why": strip_ai_dashes(str(it.get("why", "")).strip())})
            return out
        return {"selected": _clean(data.get("selected")), "excluded": _clean(data.get("excluded")),
                "summary": strip_ai_dashes(str(data.get("summary", "")).strip())}

    @staticmethod
    def _parse_json_object(raw: str):
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            nl = text.find("\n")
            if nl != -1:
                text = text[nl + 1:]
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None

    def draft_profile(self, jd_text: str, essentials: dict) -> dict:
        system = (
            "You help a job applicant build a strong, truthful résumé tailored to "
            "a target job. You are given the target job and the ESSENTIALS the "
            "person supplied (identity, education, and an experience skeleton of "
            "companies/titles/dates). Draft the rest so it reads like real, strong "
            "prose matched to the job.\n"
            "STRICT RULES:\n"
            "- Keep every company name, employment date, LOCATION (city, country), "
            "school, and degree EXACTLY as given. Never invent, alter, or DROP them, "
            "each experience entry must keep its location.\n"
            "- You MAY suggest a JD-aligned variant of a job TITLE. If you change a "
            "title, set title_suggested=true and put the person's original in "
            "title_original. Otherwise keep the title and set title_suggested=false.\n"
            "- If an experience entry already carries 'bullets' (the person's REAL "
            "accomplishments), use them as the basis and set drafted_bullets=false. "
            "Keep every stated fact and metric verbatim. If a real bullet is already a "
            "full, substantial sentence, keep it and only refine wording. If it is a "
            "TERSE FRAGMENT (a few words, e.g. 'Built SQL reporting pipelines'), EXPAND "
            "it into one complete, page-width sentence describing the SAME "
            "accomplishment, what was built/done, the tools and methods, the scope, "
            "and the qualitative business purpose or outcome that plausibly follows, "
            "so no bullet renders as a stubby half-line. Only when a role has NO bullets "
            "at all, draft 2-3 for it and set drafted_bullets=true.\n"
            "- Every bullet (real-expanded or drafted) is a complete sentence with "
            "varied opening verbs, at most 1-2 job terms each, NO keyword walls, "
            "nothing the role wouldn't plausibly support.\n"
            "- Skills: a 'skills' object of 4 to 5 labeled lines (like the template's four "
            "dense categories) grouped naturally from the person's own material (e.g. "
            "'Languages & Tools', 'Data & Analytics', 'Domain Knowledge', 'Methods & "
            "Frameworks', 'Certifications'). Prefer 4; use a 5th ONLY if the person has "
            "enough genuine skills to fill it, never more than 5 (too many crowds the "
            "page). Include ONLY skills, tools, and methods the person's OWN material "
            "(their stated skills, uploaded resume, experience, education, projects, and "
            "certifications) actually shows. PACK each line with as many of the person's "
            "GENUINE, relevant skills as naturally fit so it runs close to the right margin "
            "like a densely filled template line, full lines, not two skinny ones. Put the "
            "ones THIS JOB asks for FIRST and use the JOB's own wording where the person "
            "genuinely has them (ATS alignment). NEVER invent a tool or skill the material "
            "doesn't clearly support, and never repeat the same skill across lines, fill "
            "with the breadth of REAL skills, never padding or filler. Unmet JD skills are "
            "reported to the person separately; they are never invented onto the resume. "
            "- For each degree add a 'courses' string of 5-8 relevant courses ONLY if "
            "the person already gave courses or a transcript; otherwise leave it empty. "
            "1-2 'projects' (each with org, title, dates, and one bullet); 1-2 "
            "'extracurricular' entries (each with title, date, one bullet); and a "
            "one-line 'interests' string. Size every bullet to fill complete lines: "
            "either one FULL line (~145 chars, filling to the right margin) or two FULL "
            "lines (~290 chars), a complete, specific thought, never an awkward "
            "half-line fragment or a line-and-a-half that leaves the last line half empty.\n"
            "- Summary: a 'summary' string, a 2-3 sentence professional summary that could "
            "lead the resume (seniority/years, strongest domains, headline skills, and focus for "
            "THIS job). SYNTHESIZE it strictly from the person's REAL experience, skills, and "
            "education, never invent a title, employer, metric, or years they don't have. "
            "First person implied (no 'I'); confident but truthful.\n"
            "- Do not fabricate metrics; keep claims generic if you lack numbers.\n"
            "Return ONLY a JSON object with keys: identity, summary, education, skills, "
            "projects, experience, extracurricular, interests. education[] items are "
            "{school, degree, date, location, courses}. experience[].roles is a list "
            "of {title, dates, bullets, title_suggested, title_original}. projects[] "
            "and extracurricular[] items include their bullets."
        )
        user = (
            f"TARGET JOB:\n{jd_text[:3000]}\n\n"
            f"ESSENTIALS (JSON):\n{json.dumps(essentials)}"
        )
        raw = self._complete(system, user, max_tokens=3000, effort="medium")
        return self._parse_profile_json(raw, essentials)

    def extract_profile(self, cv_text: str) -> dict:
        """Parse a résumé/CV (raw text) into a structured PROFILE, faithfully, no JD,
        no tailoring, no invention. Powers the profile-first 'drop your résumé' flow."""
        system = (
            "You are given the raw text of a person's resume. Extract it into a "
            "structured profile, copying every REAL fact VERBATIM. Do NOT invent, "
            "embellish, tailor, or add anything the résumé does not state.\n"
            "Return ONLY a JSON object with these keys:\n"
            "- identity: {name, email, phone, address, linkedin, github, blog}, empty "
            "string for anything the résumé doesn't give. Put the FULL mailing address "
            "(street, city, state, ZIP) in address if present.\n"
            "- summary: the résumé's own summary/objective if it has one; otherwise a "
            "1-2 sentence neutral summary synthesized ONLY from the real content, never "
            "an invented title, employer, metric, or years.\n"
            "- education: [{school, degree, date, location, courses}], courses only if "
            "the résumé lists them, else \"\".\n"
            "- skills: an object of labeled lines grouping the skills the résumé lists "
            "(e.g. {\"Languages\": \"Python, SQL\", \"Cloud\": \"AWS, Docker\"}); {} if none.\n"
            "- projects: [{title, dates, bullets:[verbatim]}] if the résumé lists projects, else [].\n"
            "- experience: [{org, location, roles:[{title, dates, bullets:[verbatim real "
            "bullets]}]}], keep every company, title, employment date, location, and "
            "bullet EXACTLY as written; copy bullets verbatim, never paraphrase or invent.\n"
            "- extracurricular: [{title, date, bullets:[]}] if present, else [].\n"
            "- interests: a one-line string if the résumé lists interests, else \"\".\n"
            "Return ONLY the JSON object."
        )
        raw = self._complete(system, f"RÉSUMÉ TEXT:\n{cv_text[:12000]}", max_tokens=3000, effort="medium")
        data = self._parse_json_object(raw)
        return data if isinstance(data, dict) else {}

    def draft_cover_letter(self, jd_text: str, profile: dict, role: str = "",
                           company: str = "", tone: str = "professional") -> str:
        system = (
            "You write a concise, genuine, WELL-CRAFTED cover letter for a job applicant, tailored "
            "to a target job and grounded ONLY in the person's PROFILE.\n"
            "STRICT RULES:\n"
            "- Use ONLY facts the profile supports. NEVER invent employers, job titles, dates, "
            "metrics, numbers, or accomplishments the profile does not state. If you lack a specific "
            "fact, stay qualitative rather than making one up.\n"
            "CRAFT (what makes it strong, not generic):\n"
            "- OPEN WITH A REAL HOOK, never a template line. BANNED openers: 'I am writing to "
            "apply', 'I am excited to apply', 'I am writing to express my interest', 'Please accept "
            "this'. Instead open on the single most relevant real thing about THIS applicant for "
            "THIS role (a concrete strength, a genuine motivation, a specific result).\n"
            "- 'Why this company' must lean on CONCRETE signals from the JD itself (its stated "
            "mission, product, customers, or values). If the JD gives nothing specific, focus "
            "honestly on the ROLE, do NOT invent facts about the company or write hollow flattery.\n"
            "- The middle paragraph leads with the applicant's STRONGEST JD-relevant real "
            "accomplishment, quantified if the profile gives a number; mirror the JD's own wording "
            "for skills the person genuinely has (ATS alignment).\n"
            "- 3-4 short paragraphs, ~250-320 words. A brief, warm, specific close (not 'thank you "
            "for your consideration' boilerplate).\n"
            f"- Tone: {tone}. First person, no clichés or filler, no 'I am the perfect candidate' "
            "overreach, no empty superlatives.\n"
            "- Address the hiring team unless the JD names a person; sign off with the person's name "
            "from the profile identity.\n"
            "Return ONLY the letter text, no preamble, no explanation, no markdown fences."
        )
        who = f"ROLE: {role}\nCOMPANY: {company}\n" if (role or company) else ""
        user = (f"{who}TARGET JOB:\n{jd_text[:3000]}\n\n"
                f"APPLICANT PROFILE (JSON):\n{json.dumps(profile)[:6000]}")
        return self._complete(system, user, max_tokens=900, effort="medium")

    def draft_referral_message(self, role: str, company: str, profile: dict,
                               jd_text: str = "", recipient_name: str = "",
                               relationship: str = "") -> str:
        """Draft a short, warm outreach message the applicant can send to a real person at
        the target company to ask for a referral. Grounded ONLY in the applicant's own
        profile: we never invent a shared school, employer, or mutual contact. The person
        finds the recipient themselves and sends it themselves (privacy-preserving, no
        scraping, no auto-send)."""
        system = (
            "You write a brief, genuine referral-request message a job applicant will send "
            "to someone who works at the company they want to join (LinkedIn message or "
            "email).\n"
            "STRICT RULES:\n"
            "- Ground ONLY in the applicant's PROFILE. NEVER invent a shared school, former "
            "employer, mutual connection, or any tie to the recipient that the inputs do not "
            "state. If no genuine tie is given, write a respectful cold message instead.\n"
            "- 90 to 130 words. Warm, concise, and respectful of a stranger's time. Open by "
            "naming the specific role; give TWO real, relevant reasons from the profile the "
            "applicant is a strong fit (mirror the JD wording for skills they genuinely "
            "have); make ONE clear, low-pressure ask (would they be open to referring the "
            "applicant, or sharing a quick thought on the team); thank them.\n"
            "- First person, plain language, no flattery, no 'I am the perfect candidate' "
            "overreach, no presumptuous 'I know you will love my profile'. It is fine for the "
            "recipient to say no.\n"
            "- Address the recipient by first name if one is given, otherwise 'Hi there'. "
            "Sign off with the applicant's name from the profile identity.\n"
            "Return ONLY the message text, no subject line, no preamble, no markdown fences."
        )
        tie = f"HOW THE APPLICANT KNOWS THEM (use only if genuine): {relationship}\n" if relationship else ""
        to = f"RECIPIENT FIRST NAME: {recipient_name}\n" if recipient_name else ""
        jd = f"TARGET JOB:\n{jd_text[:1500]}\n\n" if jd_text else ""
        user = (f"ROLE: {role or '(unknown)'}\nCOMPANY: {company or '(unknown)'}\n{to}{tie}\n"
                f"{jd}APPLICANT PROFILE (JSON):\n{json.dumps(profile)[:6000]}")
        return self._complete(system, user, max_tokens=380, effort="medium")

    def answer_screening_questions(self, jd_text: str, profile: dict,
                                   questions: list[str]) -> list[str]:
        if not questions:
            return []
        system = (
            "You answer a job application's free-text screening questions for the applicant, "
            "grounded ONLY in their PROFILE and the target job.\n"
            "STRICT RULES:\n"
            "- One first-person answer per question, 2-5 sentences, specific and genuine.\n"
            "- Use ONLY facts the profile supports. NEVER invent employers, metrics, dates, or "
            "experience. If the profile lacks what a question asks for, answer briefly and "
            "honestly (e.g. transferable experience) rather than fabricating.\n"
            "- Mirror the JD's wording for skills the person genuinely has.\n"
            'Return ONLY a JSON object: {"answers": ["...", ...]} with exactly one answer per '
            "question, in the SAME ORDER as the questions given."
        )
        qlist = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
        user = (f"TARGET JOB:\n{jd_text[:2500]}\n\n"
                f"APPLICANT PROFILE (JSON):\n{json.dumps(profile)[:6000]}\n\n"
                f"QUESTIONS:\n{qlist}")
        raw = self._complete(system, user, max_tokens=1200, effort="medium")
        data = self._parse_json_object(raw)
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, list):
            answers = []
        answers = [str(a).strip() for a in answers]
        if len(answers) < len(questions):        # never drop a question
            answers += [""] * (len(questions) - len(answers))
        return answers[:len(questions)]

    def draft_email_reply(self, subject: str, body: str, sender: str, profile: dict) -> str:
        system = (
            "You draft a concise, professional reply to a recruiter's email on the applicant's "
            "behalf, grounded ONLY in the person's PROFILE.\n"
            "STRICT RULES:\n"
            "- Answer what the message actually asks, interest, availability, or a question, "
            "in 3-6 sentences.\n"
            "- Use ONLY facts the profile supports. NEVER invent employers, dates, metrics, or a "
            "specific availability the profile doesn't state; keep scheduling flexible and "
            "qualitative rather than committing to a real date or time.\n"
            "- Warm, first person, no clichés or filler. Greet the sender by name if the From "
            "header gives one; sign off with the person's name from the profile identity.\n"
            "Return ONLY the reply body as plain text, no subject line, no preamble, no markdown."
        )
        user = (f"FROM: {sender}\nSUBJECT: {subject}\n\nTHEIR MESSAGE:\n{(body or '')[:2500]}\n\n"
                f"APPLICANT PROFILE (JSON):\n{json.dumps(profile)[:5000]}")
        return self._complete(system, user, max_tokens=600, effort="medium")

    def draft_linkedin_profile(self, profile: dict, target_role: str,
                               current_headline: str = "", current_about: str = "") -> dict:
        """A LinkedIn headline and About aimed at the person's target role, grounded ONLY in
        the saved profile. The person pastes them into LinkedIn themselves."""
        system = (
            "You write a LinkedIn HEADLINE and ABOUT section for a job applicant, aimed at the "
            "TARGET ROLE, so recruiters searching for that role find and pick them.\n"
            "STRICT RULES:\n"
            "- Ground ONLY in the APPLICANT PROFILE. NEVER invent an employer, a number, a "
            "metric, a degree, a certification, or a skill the profile does not state. If the "
            "profile has no numbers, write without numbers.\n"
            "- HEADLINE: under 200 characters. Target role first, then two or three searchable "
            "skills in the wording a job description would use, then one outcome or strength. "
            "Separate parts with ' | '. Not a job title alone, and never 'seeking opportunities'.\n"
            "- ABOUT: 900 to 1,600 characters, first person, plain language, no em dashes or "
            "en dashes, no bullet symbols. Paragraph 1: who they are and the roles they target. "
            "Paragraph 2: what they have actually done, naming three to five skills in the "
            "target role's vocabulary. Paragraph 3: one proof point from the profile. Last line: "
            "a short call to action (open to the target roles; how to reach them, without "
            "inventing contact details).\n"
            "- Keep anything true and useful from the CURRENT headline/About; drop filler.\n"
            "Return ONLY a JSON object: {\"headline\": \"...\", \"about\": \"...\"}."
        )
        cur = ""
        if current_headline:
            cur += f"CURRENT HEADLINE: {current_headline[:300]}\n"
        if current_about:
            cur += f"CURRENT ABOUT:\n{current_about[:2600]}\n"
        user = (f"TARGET ROLE: {target_role or '(the most recent role in the profile)'}\n{cur}\n"
                f"APPLICANT PROFILE (JSON):\n{json.dumps(profile, ensure_ascii=False)[:7000]}")
        raw = self._complete(system, user, max_tokens=1200, effort="medium")
        data = self._parse_json_object(raw) or {}
        if not isinstance(data, dict):
            return {"headline": "", "about": ""}
        return {"headline": str(data.get("headline") or ""), "about": str(data.get("about") or "")}

    def classify_application_mail(self, subject: str, body: str, sender: str, company: str) -> dict:
        """Stage triage for ONE message from a TRACKED employer the rules could not place
        (inbox/suggestions.py). Cheap and strict: "other" unless the text plainly supports a kind."""
        system = (
            "You read one email a job applicant received from an employer they applied to, and say "
            "what it is. Answer with ONLY JSON: {\"kind\": ..., \"phrase\": ..., \"reason\": ...}.\n"
            "kind is exactly one of: interview_invite (an interview, a call, a screen, scheduling, "
            "next steps), rejection (not moving forward), offer (a job offer or offer letter), "
            "assessment (a coding test, take-home or online assessment), application_received "
            "(a confirmation that the application arrived), recruiter_outreach (a recruiter "
            "reaching out about a role), other.\n"
            "phrase: the exact words from the message that decided it (under 12 words). "
            "reason: one short plain sentence, no dashes. Say other whenever the text does not "
            "plainly support a kind; never guess."
        )
        user = (f"EMPLOYER: {company}\nFROM: {sender}\nSUBJECT: {subject}\n\nMESSAGE:\n"
                f"{(body or '')[:3000]}")
        raw = self._complete(system, user, max_tokens=200, effort="low")
        data = self._parse_json_object(raw) or {}
        return {"kind": str(data.get("kind") or "other"), "phrase": str(data.get("phrase") or ""),
                "reason": str(data.get("reason") or "")}

    def draft_stage_email(self, kind: str, job: dict, profile: dict, contact: dict,
                          stage: str) -> dict:
        """One short per-stage email for a tracked job (inbox/drafts.py). The person edits and
        sends it from their own mail; nothing is sent here."""
        from inbox.drafts import DRAFT_LABELS
        purpose = DRAFT_LABELS.get(kind, kind)
        system = (
            "You draft one short, plain email for a job applicant to send themselves, about ONE "
            "job they are tracking. Write it for this purpose: " + purpose + ".\n"
            "STRICT RULES:\n"
            "- Use ONLY facts in the APPLICANT PROFILE, the JOB and the CONTACT given. Never invent "
            "dates, times, salary figures, names, employers, metrics, or anything the inputs do "
            "not state. Keep availability flexible and qualitative.\n"
            "- 3 to 6 sentences, warm, first person, no filler, no cliches. Plain text, no "
            "markdown, no bullet points, no em or en dashes.\n"
            "- Greet the contact by first name when one is given, else a neutral greeting. Sign "
            "off with the applicant's name from the profile.\n"
            "- For a negotiation, ask to discuss, do not state a number unless the profile "
            "gives a desired salary. For a decline, be gracious and brief.\n"
            "Return ONLY JSON: {\"subject\": \"...\", \"body\": \"...\"}."
        )
        user = (f"JOB (JSON):\n{json.dumps({k: (job or {}).get(k) for k in ('company', 'title', 'location', 'stage', 'applied_at', 'notes')})}\n"
                f"STAGE: {stage}\n"
                f"CONTACT (JSON):\n{json.dumps(contact or {})}\n\n"
                f"APPLICANT PROFILE (JSON):\n{json.dumps(profile)[:5000]}")
        raw = self._complete(system, user, max_tokens=700, effort="medium")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes
        return {"subject": strip_ai_dashes(str(data.get("subject") or "")),
                "body": strip_ai_dashes(str(data.get("body") or raw or ""))}

    # -- Project builder: defend-your-work test (the honesty gate) --------- #
    def generate_project_test(self, project: dict) -> dict:
        """A short DEFENSE test that reveals whether the person GENUINELY understands a
        project they want on their CV (can they defend it in an interview), grounded in THIS
        project. This is the integrity gate: you can't add a project you can't defend."""
        system = (
            "You are a technical interviewer. A candidate wants to put a project on their resume. "
            "Write a short DEFENSE test that reveals whether they GENUINELY understand what "
            "they built and could defend it in an interview, not whether they memorized facts. "
            "Ground EVERY question in THIS specific project. Cover these angles, one each:\n"
            "1. a design-decision probe ('why did you choose X over an alternative');\n"
            "2. a walk-through ('explain how the core part actually works');\n"
            "3. a trade-off ('what did you give up, what breaks at scale');\n"
            "4. an extension ('how would you add a specific new feature');\n"
            "5. a debugging/complexity probe ('what went wrong or was harder than expected, "
            "and how did you handle it').\n"
            "Each question must be answerable by someone who truly built and understood it, and "
            "expose someone who can't. Return ONLY JSON {\"questions\": [\"...\", ...]} with 5 "
            "plain-text questions, no numbering."
        )
        user = (f"PROJECT\ntitle: {project.get('title','')}\n"
                f"what it does / how it was built: {project.get('summary','')}\n"
                f"tech: {project.get('tech','')}")
        raw = self._complete(system, user, max_tokens=700, effort="medium")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes
        qs = [strip_ai_dashes(str(q).strip()) for q in (data.get("questions") or []) if str(q).strip()]
        return {"questions": qs[:6] or [
            "Why did you choose this approach over an alternative, and what were the trade-offs?",
            "Walk me through how the core part of it actually works.",
            "What did you give up with this design, and what would break as it scaled up?",
            "How would you add a significant new feature to it?",
            "What went wrong or was harder than you expected, and how did you handle it?",
        ]}

    def grade_project_defense(self, project: dict, qa: list[dict]) -> dict:
        """Judge whether the answers show GENUINE understanding, so the person only claims a
        project they can actually defend. Honest, not polish-graded."""
        system = (
            "You are a fair but rigorous technical interviewer grading whether a candidate can "
            "DEFEND a project they want to put on their resume. Judge GENUINE UNDERSTANDING, not "
            "writing polish: do the answers show they made the decisions, understand how it "
            "works, and grasp the trade-offs? An answer that is vague, hand-wavy, self-"
            "contradicting, or that anyone who did NOT build it could give must be marked down. "
            "Be honest, the point is to protect the candidate from claiming what they can't "
            "defend in a real interview.\n"
            "For EACH question, verdict = 'solid' (clearly understands), 'shaky' (partial or "
            "vague), or 'missing' (does not demonstrate understanding), plus a one-line note.\n"
            "PASS only if they can genuinely defend the project overall: NO 'missing' answers "
            "and at most ONE 'shaky'. Otherwise fail and list the specific gaps to close.\n"
            "Return ONLY JSON: {\"pass\": true|false, \"per_question\": [{\"verdict\": "
            "\"solid|shaky|missing\", \"note\": \"...\"}], \"summary\": \"one honest sentence\", "
            "\"gaps\": [\"specific thing to understand better\", ...]}."
        )
        qa_text = "\n\n".join(f"Q: {p.get('q','')}\nA: {p.get('a','')}" for p in (qa or []))
        user = (f"PROJECT\ntitle: {project.get('title','')}\nsummary: {project.get('summary','')}\n"
                f"tech: {project.get('tech','')}\n\nDEFENSE (their answers):\n{qa_text}")
        raw = self._complete(system, user, max_tokens=900, effort="medium")
        data = self._parse_json_object(raw)
        if not isinstance(data, dict):
            return {"pass": False, "per_question": [], "gaps": [],
                    "summary": "Couldn't grade the defense this time, try again."}
        data["pass"] = bool(data.get("pass"))
        data.setdefault("per_question", [])
        data.setdefault("gaps", [])
        data.setdefault("summary", "")
        # Strip AI-tell em/en dashes from every user-facing string (per the no-dashes rule).
        from tailoring.conform import strip_ai_dashes
        data["summary"] = strip_ai_dashes(data["summary"])
        data["gaps"] = [strip_ai_dashes(str(g)) for g in data["gaps"]]
        for pq in data["per_question"]:
            if isinstance(pq, dict) and pq.get("note"):
                pq["note"] = strip_ai_dashes(str(pq["note"]))
        return data

    def suggest_buildable_projects(self, profile: dict, target: str = "") -> dict:
        """Suggest concrete, buildable projects that would CLOSE the gap between the person's
        real background and a target role/JD, so they don't invent a project from scratch. Each
        idea extends what they already know (so it stays genuinely defensible at the gate) toward
        a specific skill the target wants. The output feeds straight into a draft project."""
        system = (
            "You are a pragmatic engineering mentor helping someone pick a portfolio project to "
            "BUILD so they can honestly put a wanted skill on their resume. Suggest 3 concrete, "
            "buildable projects (a real few-days-to-weeks scope, not a toy and not a months-long "
            "epic). RULES:\n"
            "- Ground each idea in what the person ALREADY knows from their PROFILE and stretch it "
            "toward the TARGET, so they can genuinely build AND later defend it in an interview.\n"
            "- Each project must demonstrate ONE specific skill the target wants (name it).\n"
            "- Be concrete about WHAT they'd build and the core tech, not a vague theme.\n"
            "- No fabrication: suggest what they could realistically build, don't imply they've "
            "already done it.\n"
            "For each: title (short, resume-worthy), skill (the one wanted skill it proves), "
            "summary (2-3 sentences on what to build and how), tech (comma-separated), why (one "
            "sentence on how it strengthens their fit for the target).\n"
            "Return ONLY JSON: {\"suggestions\": [{\"title\":\"...\",\"skill\":\"...\","
            "\"summary\":\"...\",\"tech\":\"...\",\"why\":\"...\"}, ...]} with exactly 3 items."
        )
        tgt = (target or "").strip()
        user = ((f"TARGET ROLE / JOB:\n{tgt[:2500]}\n\n" if tgt else
                 "TARGET: no specific role given; suggest projects that strengthen this person's "
                 "profile for the kind of roles their background points to.\n\n")
                + f"APPLICANT PROFILE (JSON):\n{json.dumps(profile)[:6000]}")
        raw = self._complete(system, user, max_tokens=1100, effort="medium")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes
        out = []
        for s in (data.get("suggestions") or [])[:3]:
            if not isinstance(s, dict) or not str(s.get("title", "")).strip():
                continue
            out.append({k: strip_ai_dashes(str(s.get(k, "")).strip())
                        for k in ("title", "skill", "summary", "tech", "why")})
        return {"suggestions": out}

    def suggest_github_ideas(self, profile: dict, role: str = "", jd_terms: list | None = None,
                             missing: list | None = None, count: int = 6, query: str = "",
                             exclude: list | None = None) -> dict:
        """GitHub portfolio ideas aimed at a target role: small, finishable projects a recruiter
        would pin, each proving 3 to 5 skills the role asks for, favouring the ones the profile
        lacks. The model only drafts the ideas; the app adds real example repos from GitHub's
        own search (never a model-written URL) and works out which skills are gaps."""
        system = (
            "You are a senior engineer helping an international student pick GitHub portfolio "
            f"projects for a target role. Suggest {count} distinct, ORIGINAL project ideas a "
            "recruiter would want to see pinned. RULES:\n"
            "- Lawful and respectful of other sites: never suggest a project that scrapes, crawls or "
            "automates a website whose terms forbid it (LinkedIn, Indeed, Glassdoor, Handshake, job "
            "boards, social networks, ticketing or shopping sites) or that evades bot checks. Use "
            "official public APIs, open datasets (Kaggle, data.gov, city open-data portals) or the "
            "project's own sample data instead.\n"
            "- Finishable: small and focused. Not a tutorial clone, not "
            "a months-long epic. Each must end with something runnable and demoable.\n"
            "- Each proves 3 to 5 concrete skills the target role asks for. Prefer the MISSING "
            "skills listed (the role wants them, the profile does not show them yet), stretching "
            "from what the person already knows so they can genuinely build and defend it.\n"
            "- Vary the ideas: different domains.\n"
            "- Use the role's own skill wording.\n"
            + ("- TOPIC: every idea must be about the topic the person searched for (given below), "
               "still aimed at the target role and grounded in their profile.\n" if query else "")
            + ("- Do NOT repeat or lightly reword any idea in the ALREADY SHOWN list; each must be "
               "a genuinely different project.\n" if exclude else "")
            + "- No em or en dashes. Plain words.\n"
            "For each: title (2 to 4 words), pitch (ONE line, under 90 characters, what it does), "
            "skills (array of 3 to 5 skill names), tech (comma-separated stack), "
            "repo (a kebab-case repo name), "
            "search (3 to 5 plain keywords to find similar public repos on GitHub).\n"
            "Return ONLY JSON: {\"ideas\": [{\"title\":\"...\",\"pitch\":\"...\","
            "\"skills\":[\"...\"],\"tech\":\"...\",\"repo\":\"...\","
            "\"search\":\"...\"}]}"
        )
        user = (f"TARGET ROLE: {(role or '').strip() or 'not given; infer from the profile'}\n"
                f"SKILLS THE ROLE ASKS FOR: {', '.join(map(str, jd_terms or [])) or 'not known'}\n"
                f"MISSING FROM THE PROFILE: {', '.join(map(str, missing or [])) or 'none known'}\n"
                + (f"TOPIC THE PERSON SEARCHED FOR: {query.strip()[:80]}\n" if query else "")
                + (f"ALREADY SHOWN (do not repeat): {'; '.join(map(str, (exclude or [])[:60]))}\n" if exclude else "")
                + "\n"
                + f"APPLICANT PROFILE (JSON):\n{json.dumps(profile)[:5000]}")
        raw = self._complete(system, user, max_tokens=1600, effort="medium")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes
        out = []
        for s in (data.get("ideas") or [])[:count]:
            if not isinstance(s, dict) or not str(s.get("title", "")).strip():
                continue
            skills = s.get("skills") if isinstance(s.get("skills"), list) else str(s.get("skills") or "").split(",")
            out.append({
                "title": strip_ai_dashes(str(s.get("title", "")).strip()),
                "pitch": strip_ai_dashes(str(s.get("pitch", "")).strip()),
                "skills": [strip_ai_dashes(str(k).strip()) for k in skills if str(k).strip()][:5],
                "tech": strip_ai_dashes(str(s.get("tech", "")).strip()),
                "repo": str(s.get("repo", "")).strip(),
                "search": str(s.get("search", "")).strip(),
            })
        return {"ideas": out}

    def generate_build_plan(self, project: dict) -> dict:
        """An ordered, buildable plan for a project the person is about to build THEMSELVES. Each
        milestone both tells them what to build AND teaches the concept behind it, so that by the
        time they reach the defend gate their understanding is real, not borrowed. This is the
        'guided build and teach' step: structure plus learning, not a solution to copy."""
        system = (
            "You are a hands-on engineering mentor laying out how someone should BUILD a portfolio "
            "project themselves, step by step, learning as they go. Produce 4-6 ordered milestones "
            "that take them from nothing to a working, defensible project. For EACH milestone:\n"
            "- title: a short imperative name for the step.\n"
            "- build: concretely what they DO in this step, grounded in THIS project and its tech.\n"
            "- learn: the concept(s) this step teaches and WHY it works this way, so they truly "
            "understand it (this is the teaching, keep it real and specific, not generic advice).\n"
            "- check: how they'll know the step actually works before moving on.\n"
            "They must build it themselves so they can defend it later, so guide and teach, do NOT "
            "hand over a finished copy-paste solution. Order milestones so each builds on the last.\n"
            "Return ONLY JSON: {\"milestones\": [{\"title\":\"...\",\"build\":\"...\","
            "\"learn\":\"...\",\"check\":\"...\"}, ...]}."
        )
        user = (f"PROJECT\ntitle: {project.get('title','')}\n"
                f"what it does \\ how they'll build it: {project.get('summary','')}\n"
                f"tech: {project.get('tech','')}")
        # A 4-6 milestone plan with four substantive fields each is long; give it room so the
        # JSON isn't truncated mid-object (a short cap silently yields an unparseable response).
        raw = self._complete(system, user, max_tokens=2600, effort="medium")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes
        out = []
        for m in (data.get("milestones") or [])[:6]:
            if not isinstance(m, dict) or not str(m.get("title", "")).strip():
                continue
            out.append({k: strip_ai_dashes(str(m.get(k, "")).strip())
                        for k in ("title", "build", "learn", "check")})
        return {"milestones": out}

    # -- AI-built projects: plan the files, write them in batches, teach them, defend them -- #
    # The person's personal data is never sent here: only the idea (title, pitch, tech, skills).
    @staticmethod
    def _idea_text(project: dict) -> str:
        return (f"title: {project.get('title', '')}\n"
                f"pitch: {project.get('summary', '')}\n"
                f"tech: {project.get('tech', '')}\n"
                f"skills it should prove: {', '.join(project.get('skills') or [])}")

    def plan_project_files(self, project: dict, max_files: int = 16, max_bytes: int = 60000) -> dict:
        """The file tree for a SMALL, working, runnable portfolio project. Paths and a one-line
        purpose each; the code is written later, in batches. Returns {"files": [{path, purpose}],
        "run": the command that runs it, "test": the command that runs the tests}."""
        system = (
            "You are a senior engineer planning a SMALL but genuinely working portfolio project a "
            "student will study and then defend in an interview. Plan the file tree only.\n"
            f"- At most {max_files} files, total code under about {max_bytes // 1000} KB. Small and "
            "finishable, not a framework.\n"
            "- Use the idea's own stack. Real, runnable code with only common, well-known "
            "dependencies. No paid APIs, no API keys, no network calls needed to run the tests "
            "(use local sample data or fakes).\n"
            "- MUST include: README.md, .gitignore, a dependency file for the stack (e.g. "
            "requirements.txt or package.json), and 2 to 4 focused unit tests in a test file.\n"
            "- Relative POSIX paths only, no leading slash, no '..', at most 4 folders deep.\n"
            "Return ONLY JSON: {\"files\": [{\"path\": \"...\", \"purpose\": \"one line\"}], "
            "\"run\": \"command to run it\", \"test\": \"command to run the tests\"}."
        )
        raw = self._complete(system, "PROJECT IDEA\n" + self._idea_text(project),
                             max_tokens=2000, effort="medium")
        data = self._parse_json_object(raw) or {}
        files = []
        for f in data.get("files") or []:
            if isinstance(f, dict) and str(f.get("path") or "").strip():
                files.append({"path": str(f["path"]).strip(), "purpose": str(f.get("purpose") or "").strip()[:200]})
        return {"files": files[:max_files], "run": str(data.get("run") or "").strip()[:200],
                "test": str(data.get("test") or "").strip()[:200]}

    def write_project_files(self, project: dict, plan: dict, paths: list, written: list,
                            fix: dict | None = None) -> dict:
        """Write the content of ``paths`` (a batch of the plan), consistent with the files already
        ``written`` (their paths and first lines are passed for context). ``fix`` maps a path to
        the validation problem its last version had. Files come back between plain markers, not
        JSON, so code needs no escaping. Returns {"files": [{path, content}]}."""
        tree = "\n".join(f"- {f['path']}: {f.get('purpose', '')}" for f in plan.get("files") or [])
        ctx = "\n\n".join(f"### {w['path']}\n{w['content'][:1500]}" for w in (written or [])[-8:])
        fixes = "".join(f"\n- {p}: {why}" for p, why in (fix or {}).items())
        system = (
            "You write the files of a SMALL, working portfolio project, exactly as planned. Real, "
            "correct, idiomatic code a student can read and defend. Rules:\n"
            "- Never include API keys, tokens, passwords, or personal data. Read any config from "
            "environment variables with a safe default.\n"
            "- No absolute file paths. No network calls at import time; network code, if any, only "
            "runs inside functions, and tests never need the network.\n"
            "- Short, useful comments that explain WHY, not what.\n"
            "- README.md (when asked) has these sections in order: Problem, Approach, Tech stack, "
            "How to run, Screenshots (a placeholder line), What I learned (a prompt for the "
            "student to fill in). Do not put anyone's name in it.\n"
            "Output each requested file as:\n=== FILE: <path> ===\n<content>\n=== END FILE ===\n"
            "Output nothing else."
        )
        user = (f"PROJECT IDEA\n{self._idea_text(project)}\n\nRUN: {plan.get('run', '')}\n"
                f"TEST: {plan.get('test', '')}\n\nFILE TREE\n{tree}\n\n"
                + (f"FILES ALREADY WRITTEN (for consistency)\n{ctx}\n\n" if ctx else "")
                + (f"FIX THESE PROBLEMS FROM THE LAST VERSION:{fixes}\n\n" if fixes else "")
                + "WRITE THESE FILES NOW:\n" + "\n".join(f"- {p}" for p in paths))
        raw = self._complete(system, user, max_tokens=8000, effort="medium")
        return {"files": parse_file_blocks(raw)}

    def generate_learn_steps(self, project: dict, files: list) -> dict:
        """A 5 to 8 step walkthrough of the ACTUAL files: each step points at a file and a line
        range (the app cuts the real excerpt itself), explains what it does and why it was built
        that way, and asks one quick check question."""
        listing = "\n\n".join(
            f"### {f['path']}\n" + "\n".join(f"{i + 1:>3}| {ln}" for i, ln in enumerate(f["content"].splitlines()[:220]))
            for f in files if f.get("path") != ".gitignore")[:45000]
        system = (
            "You are a patient senior engineer teaching a student the project below, which an AI "
            "pair-programmer wrote for them. They must understand it well enough to defend it in "
            "an interview. Write a 5 to 8 step walkthrough in the order a reader should learn it "
            "(entry point, core logic, data, tests). For EACH step:\n"
            "- title: short.\n- path: one file from the listing.\n- start, end: the line range "
            "(numbers in the listing) of the excerpt to show, a whole function or section, at most 80 lines.\n"
            "- focus: [first, last], the few lines inside that range the step is really about, at most "
            "15 lines.\n"
            "- explain: what this code does, plain English, 60 to 110 words. Name lines as \"line 12\" or "
            "\"lines 12 to 15\" using the listing's numbers.\n"
            "- why: 1 to 3 sentences on why it was built this way and the alternative.\n"
            "- check: one quick MULTIPLE-CHOICE question with instant feedback, always this shape: "
            "{\"kind\": \"choice\", \"q\": \"...\", \"options\": [EXACTLY 4 short options, one right and "
            "three plausible wrong ones], \"answer\": index of the right option, \"explain\": \"why that is "
            "right\", \"whys\": [one short line per option, in the same order, saying why THAT option is "
            "right or wrong]}. Never ask for typed answers.\nPlain text, no em or en dashes. Return ONLY JSON "
            "{\"steps\": [...]}."
        )
        raw = self._complete(system, f"PROJECT\n{self._idea_text(project)}\n\nFILES\n{listing}",
                             max_tokens=5000, effort="medium")
        data = self._parse_json_object(raw) or {}
        return {"steps": [s for s in (data.get("steps") or []) if isinstance(s, dict)][:8]}

    def choice_checks_for(self, items: list) -> dict:
        """Turn typed one-line Learn it checks (walkthroughs cached before checks were always multiple
        choice) into multiple-choice ones. ``items`` is [{"i", "q", "accept", "explain", "code"}];
        returns {"checks": [{"i", "q", "options", "answer", "explain", "whys"}]}. One small call."""
        system = (
            "Rewrite each quick-check question below as multiple choice for a student learning this "
            "code. For each item return {\"i\": same i, \"q\": the question, \"options\": EXACTLY 4 short "
            "options (one right, matching the accepted answer, three plausible wrong ones), \"answer\": "
            "index of the right option, \"explain\": why it is right, \"whys\": [one short line per "
            "option saying why THAT option is right or wrong]}. Plain text, no em or en dashes. Return "
            "ONLY JSON {\"checks\": [...]}."
        )
        raw = self._complete(system, json.dumps(items)[:12000], max_tokens=2500, effort="low")
        data = self._parse_json_object(raw) or {}
        return {"checks": [c for c in (data.get("checks") or []) if isinstance(c, dict)]}

    def generate_project_quiz(self, project: dict, files: list, steps: list) -> dict:
        """Defend it: a 20-question multiple-choice quiz on THIS project, from its files and the
        Learn it walkthrough, about 15 single-answer and 5 "Select all that apply" questions
        (``type`` "single" with ``answer``, or "multi" with ``answers``). One call, made once per
        build. Code questions name a file and a line range; the app cuts the real excerpt itself,
        so no code here is model-written."""
        listing = "\n\n".join(
            f"### {f['path']}\n" + "\n".join(f"{i + 1:>3}| {ln}" for i, ln in enumerate(f["content"].splitlines()[:200]))
            for f in files if f.get("path") != ".gitignore")[:40000]
        walk = "\n".join(f"- {s.get('title', '')} ({s.get('path', '')}): {s.get('explain', '')} Why: {s.get('why', '')}"
                         for s in steps or [])[:6000]
        system = (
            "You write the final quiz a student takes to prove they understand the project below, "
            "which an AI pair-programmer built for them and they have just studied. Write EXACTLY 20 "
            "multiple-choice questions, each grounded in specific files, functions or decisions of "
            "THIS project (never generic programming trivia). Cover these topics, in any order: "
            "parts (what each main part does) x4, why (why it was built that way) x3, tradeoffs x3, "
            "testing (how it is tested, what is not) x3, scale (what breaks at scale) x2, extend (how "
            "to add a feature) x2, code (reading a short excerpt) x3.\n"
            "Make about 15 single-answer questions and about 5 \"Select all that apply\" questions "
            "where two or three options are genuinely right, spread across the topics.\n"
            "For EACH question: topic (one of parts, why, tradeoffs, testing, scale, extend, code); "
            "type (\"single\" or \"multi\"); q (one sentence); options (EXACTLY 4 short, distinct, "
            "plausible options: wrong ones must be believable mistakes, not jokes, and similar in "
            "length to the right ones). A single question gives answer (the index 0 to 3 of the one "
            "right option). A multi question gives answers (the list of indices of EVERY right option, "
            "two or three of them; it is graded right only when exactly that set is picked) and never "
            "uses an \"of the above\" option. A single question may use \"All of the above\" or "
            "\"None of the above\" as its last option, but only a few times in the quiz and only "
            "where it is genuinely right or genuinely plausible, never as a giveaway. Every question "
            "gives explain (one plain sentence on why the right option or options are right).\n"
            "A code question also gives path (a file from the listing) and start, end (line numbers "
            "from the listing, at most 12 lines) and asks about that excerpt.\n"
            "Plain text, no em or en dashes. Return ONLY JSON {\"questions\": [...]}."
        )
        raw = self._complete(system, f"PROJECT\n{self._idea_text(project)}\n\nWALKTHROUGH\n{walk}"
                                     f"\n\nFILES\n{listing}", max_tokens=7000, effort="medium")
        data = self._parse_json_object(raw) or {}
        return {"questions": [q for q in (data.get("questions") or []) if isinstance(q, dict)][:24]}

    def upskill_plan(self, role: str, gaps: list[str], profile: dict,
                     required: list[str] | None = None) -> dict:
        """For the JD skills the person's profile doesn't yet cover, the FASTEST honest way to gain
        a DEMONSTRABLE, resume-worthy version of each, plus the kind of resource to use. Grounded in
        what they already have; never invents a certification.

        Smarter than a flat list: the MUST-HAVES (the ``required`` subset -- what a screener gates on)
        are labelled and pulled to the front, each step carries an honest EFFORT estimate, and a
        LEVERAGE note calls out when a gap is a short hop from something they already know. Returns
        {"plan": [{"skill","priority","effort","how","resource","leverage"}, ...]}, must-haves first.
        """
        gaps = [str(g).strip() for g in (gaps or []) if str(g).strip()][:8]
        if not gaps:
            return {"plan": []}
        from llm.base import _profile_skill_phrases
        from tailoring.conform import strip_ai_dashes
        req = {str(x).strip().lower() for x in (required or [])}
        have = ", ".join(_profile_skill_phrases(profile)[:20]) or "(not provided)"
        must = [g for g in gaps if g.lower() in req]
        system = (
            "You are a practical career coach for international students job-hunting in the US. For "
            "EACH missing skill, give the fastest HONEST way to gain a DEMONSTRABLE, resume-worthy "
            "version of it. Build on what they already have when you can. Be specific and honest: no "
            "fluff, and NEVER invent a certification or a fake claim.\n"
            "For each skill return:\n"
            "- skill: the skill name, echoed back.\n"
            "- effort: an honest time estimate to a demonstrable version, ONE short phrase like "
            "'quick win', 'a weekend', 'a week or two', or 'a few weeks'.\n"
            "- how: ONE imperative sentence, a concrete action that produces PROOF (a small project, "
            "a specific practice, a named cert only if it genuinely exists).\n"
            "- resource: the kind of resource to use (a course type, a named free platform, a project idea).\n"
            "- leverage: if this skill is a SHORT HOP from something they already have, one short "
            "phrase naming that bridge (e.g. 'you know Python, so pandas is close'); else \"\".\n"
            "Return ONLY JSON: {\"plan\":[{\"skill\":\"...\",\"effort\":\"...\",\"how\":\"...\","
            "\"resource\":\"...\",\"leverage\":\"...\"}, ...]}, one entry per missing skill."
        )
        user = (f"TARGET ROLE: {role or 'the role'}\n"
                f"THEY ALREADY HAVE: {have}\n"
                f"MUST-HAVE gaps (a screener gates on these, cover first): "
                f"{', '.join(must) or '(none flagged)'}\n"
                f"ALL MISSING SKILLS (close these): {', '.join(gaps)}")
        raw = self._complete(system, user, max_tokens=1600, effort="medium")
        data = self._parse_json_object(raw) or {}
        out = []
        for p in (data.get("plan") or [])[:8]:
            if not isinstance(p, dict) or not str(p.get("skill", "")).strip():
                continue
            skill = strip_ai_dashes(str(p.get("skill", "")).strip())
            out.append({
                "skill": skill,
                # Priority is set SERVER-SIDE from the required set, so the must-have labelling is
                # reliable regardless of what the model echoes back.
                "priority": "must-have" if skill.lower() in req else "nice-to-have",
                "effort": strip_ai_dashes(str(p.get("effort", "")).strip()),
                "how": strip_ai_dashes(str(p.get("how", "")).strip()),
                "resource": strip_ai_dashes(str(p.get("resource", "")).strip()),
                "leverage": strip_ai_dashes(str(p.get("leverage", "")).strip()),
            })
        # If the model returned nothing parseable, don't dead-end the user with an error: give an
        # honest, generic plan per gap (build something demonstrable + point at a resource kind).
        if not out:
            out = [{"skill": g,
                    "priority": "must-have" if g.lower() in req else "nice-to-have",
                    "effort": "a weekend",
                    "how": strip_ai_dashes(f"Build a small project that clearly uses {g} and put "
                                           "it on GitHub, so it is demonstrable on your resume."),
                    "resource": "a free online course plus a portfolio project",
                    "leverage": ""} for g in gaps]
        # Must-haves first (stable within each group), so the person tackles what a screener gates on.
        out.sort(key=lambda p: 0 if p.get("priority") == "must-have" else 1)
        return {"plan": out}

    def coach_project_step(self, project: dict, milestone: dict, question: str) -> dict:
        """Teach the person through a step they're stuck on. Explains the concept and guides the
        approach so they can write it and UNDERSTAND it themselves. Deliberately does not hand over
        a complete finished solution, because they have to defend this project at the gate, so a
        copy-paste answer would rob them of both the learning and the defense."""
        system = (
            "You are a patient engineering mentor helping someone who is building a portfolio "
            "project and is stuck on the current step. TEACH them:\n"
            "- Explain the concept and the WHY, in plain language, grounded in THIS project.\n"
            "- Guide the approach and, where useful, show a SMALL illustrative snippet or the "
            "shape of the solution, but do NOT write the whole thing for them to paste. They must "
            "build and understand it themselves so they can defend it in an interview.\n"
            "- If they ask you to 'just give the code', explain why doing it themselves matters "
            "here, then point them to the next concrete move.\n"
            "- Warm, direct, concise. No filler, no clichés.\n"
            "Return ONLY the explanation as plain text, no preamble, no markdown fences."
        )
        m = milestone or {}
        user = (f"PROJECT\ntitle: {project.get('title','')}\nsummary: {project.get('summary','')}\n"
                f"tech: {project.get('tech','')}\n\n"
                f"CURRENT STEP\ntitle: {m.get('title','')}\nbuild: {m.get('build','')}\n"
                f"learn: {m.get('learn','')}\n\n"
                f"THEIR QUESTION:\n{str(question or '').strip()[:1500]}")
        from tailoring.conform import strip_ai_dashes
        return {"answer": strip_ai_dashes(self._complete(system, user, max_tokens=700, effort="medium").strip())}

    def generate_interview_questions(self, profile: dict, role: str, company: str = "",
                                     jd: str = "", focus: str = "", difficulty: str = "standard") -> dict:
        """Likely questions for a REAL interview the person has landed, for a specific role/company,
        so they can PRACTICE before the interview (never a live copilot). Grounded in the JD and the
        person's own background so the questions are ones they can actually answer from real material.
        When `focus` (a competency) is set, every question drills that one competency. `difficulty`
        of 'hard' makes a demanding, senior-level screen."""
        focus_line = (f"FOCUS: every question must specifically probe the '{focus}' competency, since "
                      f"that is the person's weakest area and they are here to drill it. Vary the "
                      f"angle but keep all of them on '{focus}'.\n" if focus else "")
        hard_line = ("DIFFICULTY: make this a HARD screen. Ask demanding, senior-level questions with "
                     "pointed follow-up framing, higher-stakes situational scenarios, and probes that "
                     "make the person defend trade-offs and quantify impact. Assume a rigorous "
                     "interviewer who will not accept a vague answer.\n"
                     if str(difficulty).lower() == "hard" else "")
        company_line = (f"COMPANY REALISM: if {company} is a company with a KNOWN interview style or "
                        f"stated values (for example Amazon's Leadership Principles, a consultancy's "
                        f"case style, a startup's bias-to-action), reflect it in at least two "
                        f"questions so practice matches THIS employer, not a generic screen. Only do "
                        f"this if you actually know the company; never invent values.\n"
                        if company else "")
        system = (
            "You are preparing someone for a HireVue-style recorded screen for a REAL role they are "
            "interviewing for. Generate 6 likely questions to rehearse BEFOREHAND, in HireVue's "
            "actual style. Mix:\n"
            "- behavioral ('tell me about a time...', grounded in their real past),\n"
            "- motivational ('why this company', 'why this role'),\n"
            "- situational (a realistic 'what would you do if...' hypothetical for this job),\n"
            "- one or two technical/practical questions if the role is technical.\n"
            + focus_line + company_line + hard_line +
            "Ground them in what THIS person could answer from their real background and what the JD "
            "emphasizes, so practice is realistic, not generic. For each: q (the question), type "
            "('behavioral' | 'motivational' | 'situational' | 'technical'), competency (the one skill "
            "it probes, 1 to 3 words, for example Communication, Problem solving, Ownership, "
            "Adaptability), why (one line on what the interviewer is really probing). Do NOT write "
            "answers, this is a question set to practice against.\n"
            "Return ONLY JSON: {\"questions\": [{\"q\":\"...\",\"type\":\"...\",\"competency\":\"...\","
            "\"why\":\"...\"}, ...]}."
        )
        who = f"ROLE: {role}\n" + (f"COMPANY: {company}\n" if company else "")
        user = (who + (f"JOB DESCRIPTION:\n{jd[:2500]}\n\n" if jd else "\n")
                + f"CANDIDATE PROFILE (JSON):\n{json.dumps(profile)[:6000]}")
        raw = self._complete(system, user, max_tokens=1200, effort="medium")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes
        out = []
        for q in (data.get("questions") or [])[:8]:
            if not isinstance(q, dict) or not str(q.get("q", "")).strip():
                continue
            out.append({k: strip_ai_dashes(str(q.get(k, "")).strip())
                        for k in ("q", "type", "competency", "why")})
        return {"questions": out}

    def assess_cv_worthiness(self, text: str) -> dict:
        """Judge whether a note the person shared describes a real, CV-worthy accomplishment (a win,
        a project, an impact, a hard problem solved), and if so frame it. Honest: a vague 'did some
        work' is NOT worthy; grounds any framing ONLY in what the note says, inventing no numbers or
        results. Returns {worthy, reason, competency, headline}."""
        system = (
            "You judge whether a short note a person shared about their work is a CV-worthy "
            "accomplishment worth capturing (a real win, project, measurable impact, leadership "
            "moment, or hard problem solved), versus everyday chatter or something too vague to use.\n"
            "Be honest: a note with no concrete action or outcome is NOT worthy. Never invent a "
            "number, result, employer, or detail the note does not state.\n"
            "Return ONLY JSON: {\"worthy\": true|false, \"reason\": \"one short line\", "
            "\"competency\": \"the one skill it shows, 1-3 words, or ''\", \"headline\": \"a tight "
            "CV-style one-line framing grounded ONLY in the note, or ''\"}."
        )
        raw = self._complete(system, f"NOTE:\n{(text or '')[:1500]}", max_tokens=250, effort="low")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes
        return {
            "worthy": bool(data.get("worthy")),
            "reason": strip_ai_dashes(str(data.get("reason", "")).strip()),
            "competency": strip_ai_dashes(str(data.get("competency", "")).strip()),
            "headline": strip_ai_dashes(str(data.get("headline", "")).strip()),
        }

    def coach_interview_answer(self, profile: dict, question: str, answer: str) -> dict:
        """Coach the person's OWN practice answer before the interview. Structures their real story
        (STAR for behavioral), points out concrete gaps, and offers a tightened version built ONLY
        from what they said and their profile. Never fabricates experience, never a script to recite
        as a live copilot, and flags anything the answer claims that their real material doesn't back."""
        system = (
            "You are an interview coach giving feedback on a practice answer BEFORE a real interview. "
            "Help the person deliver THEIR real story better. STRICT RULES:\n"
            "- Ground everything in what they actually said and their PROFILE. NEVER invent an "
            "experience, employer, metric, or outcome. If the answer claims something the profile "
            "doesn't support, flag it in 'honesty' so they keep it truthful.\n"
            "- For a behavioral question, structure their story into STAR (situation, task, action, "
            "result); leave a field empty if they didn't give it (that's a gap to fill).\n"
            "- 'improve': 2-4 concrete, specific fixes (vague result, missing metric, too long, "
            "no ownership).\n"
            "- 'tighter': a stronger version of THEIR answer to practice out loud, built only from "
            "what they gave plus their real profile. It is theirs to rehearse and own, not a script "
            "to recite verbatim, and must stay truthful.\n"
            "- 'assessment': one honest sentence on the answer as it stands.\n"
            "Return ONLY JSON: {\"assessment\":\"...\",\"star\":{\"situation\":\"...\",\"task\":\"...\","
            "\"action\":\"...\",\"result\":\"...\"},\"improve\":[\"...\"],\"tighter\":\"...\","
            "\"honesty\":\"\"}. Set honesty to \"\" if nothing is overstated."
        )
        user = (f"QUESTION:\n{str(question or '').strip()[:800]}\n\n"
                f"THEIR PRACTICE ANSWER:\n{str(answer or '').strip()[:2500]}\n\n"
                f"CANDIDATE PROFILE (JSON):\n{json.dumps(profile)[:5000]}")
        raw = self._complete(system, user, max_tokens=1100, effort="medium")
        data = self._parse_json_object(raw)
        if not isinstance(data, dict):
            return {"assessment": "Couldn't coach this answer this time, try again.",
                    "star": {}, "improve": [], "tighter": "", "honesty": ""}
        from tailoring.conform import strip_ai_dashes
        star = data.get("star") if isinstance(data.get("star"), dict) else {}
        return {
            "assessment": strip_ai_dashes(str(data.get("assessment", "")).strip()),
            "star": {k: strip_ai_dashes(str(star.get(k, "")).strip())
                     for k in ("situation", "task", "action", "result")},
            "improve": [strip_ai_dashes(str(x).strip()) for x in (data.get("improve") or []) if str(x).strip()],
            "tighter": strip_ai_dashes(str(data.get("tighter", "")).strip()),
            "honesty": strip_ai_dashes(str(data.get("honesty", "")).strip()),
        }

    # -- chat edits of a finished CV (notify/cvreview.py) ------------------ #
    def plan_cv_edit(self, instruction: str, outline: str) -> dict:
        """Turn one plain-language edit request into a structured plan over the CV's span ids.
        The model only PLANS here; the app applies the plan to the named spans and nothing
        else, and asks the person to confirm any change to a fact."""
        system = (
            "You turn a person's instruction about THEIR OWN résumé into an edit plan. The résumé "
            "is listed as span ids with their text: S summary, E1.2 employer 1 bullet 2, "
            "E1.title / E1.dates / E1.company / E1.location, E1.R2.title for a second role at the "
            "same employer, P3.1 project 3 bullet 1, P3.name, ED1.school / ED1.degree / ED1.date, "
            "K2 skills line 2, X1.1 extracurricular, I interests. Sections are named summary, "
            "education, skills, projects, experience, extracurricular, interests.\n"
            "Ops:\n"
            "- rewrite: {op, target, instruction}: reword one span as asked (the app does the "
            "wording later).\n"
            "- set: {op, target, value}: the person gave the exact new text (dates, a title, an "
            "employer name, or a quoted sentence). Copy their value verbatim.\n"
            "- move_section: {op, section, before} or {op, section, after}.\n"
            "- drop: {op, target}: a bullet id (E1.2), a skills line (K2) or a whole entry (P2, E3).\n"
            "- add_bullet: {op, target, value}: target is the entry (E1, E1.R2, P2, X1); value is "
            "the person's text verbatim.\n"
            "Resolve names to ids ('my title at Stanbic' -> the E id whose company is Stanbic). "
            "Target only what the person asked for, never extra spans. If the instruction is "
            "unclear or names something that is not in the list, return no edits and a short "
            "clarify question that proposes the closest id ('Did you mean E1.2?').\n"
            "Return ONLY JSON: {\"edits\": [...], \"clarify\": \"\"}."
        )
        user = f"RÉSUMÉ SPANS:\n{outline[:9000]}\n\nINSTRUCTION:\n{instruction[:1000]}"
        raw = self._complete(system, user, max_tokens=800, effort="low")
        data = self._parse_json_object(raw)
        return data if isinstance(data, dict) else {"edits": [], "clarify": ""}

    def edit_cv_spans(self, instruction: str, targets: dict, context: dict,
                      jd_text: str = "", budgets: dict | None = None) -> dict:
        """Rewrite ONLY the target spans as the person asked. Returns {id: new text}; the app
        rejects the result if any other span would change."""
        import json as _json
        budgets = budgets or {}
        limits = "\n".join(f"- {sid}: at most {budgets.get(sid) or len(str(t)) + 20} characters"
                            for sid, t in targets.items())
        system = (
            "You edit specific spans of a person's résumé exactly as they ask, and nothing else.\n"
            "- Return ONLY the TARGET ids, each with its new text. Never return or alter any other "
            "span; the others are shown for context only.\n"
            "- Do what the instruction says (shorter, a different word, a different emphasis). "
            "Keep the same accomplishment and every real metric.\n"
            "- Never add a number, date, employer, product, or skill the span and its role do not "
            "already show, unless the person typed it in the instruction.\n"
            "- One complete sentence per bullet, plain text, no markup, no dashes as punctuation.\n"
            "Return ONLY JSON: {\"<id>\": \"<new text>\", ...}."
        )
        user = (f"INSTRUCTION:\n{instruction[:1000]}\n\nTARGET SPANS:\n"
                f"{_json.dumps(targets, ensure_ascii=False)}\n\nLENGTH LIMITS:\n{limits}\n\n"
                f"CONTEXT (do not change):\n{_json.dumps(context, ensure_ascii=False)[:4000]}\n\n"
                f"TARGET JOB (for vocabulary only):\n{(jd_text or '')[:1200]}")
        raw = self._complete(system, user, max_tokens=900, effort="low")
        data = self._parse_json_object(raw)
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int, float))}

    def plan_form_fill(self, fields: list[dict], available_keys: list[str], role: str = "",
                       company: str = "", jd: str = "") -> dict:
        """Map a job-application form's fields to a fill plan for Tailor's autonomous applier, WITHOUT
        ever seeing the person's real personal data. The prompt carries ONLY the form structure plus
        the placeholder KEYS the plan may reference; the app resolves those keys to real values
        locally, at fill time. So the model decides WHICH data goes WHERE, never the data itself.

        Returns {"actions":[{"ref","op","source"?,"value"?}, ...]}: ``source`` is a placeholder key
        (personal data, resolved locally); ``value`` is a non-personal literal the model chose (an
        option label, a date). Fields it can't confidently fill are left out."""
        import json
        keys = ", ".join(available_keys or [])
        system = (
            "You map a JOB APPLICATION form to a fill plan for an assistant that will fill it out. You "
            "are given the form's FIELDS (structure only, never any real value) and a list of "
            "PLACEHOLDER KEYS for the applicant's personal data. STRICT RULES:\n"
            "- NEVER output the applicant's real name, email, phone, or address. For a field that "
            "wants personal data, reference the matching KEY via \"source\" (the app substitutes the "
            "real value locally). You never see it.\n"
            "- For a résumé/CV file input, use op \"upload\".\n"
            "- NEVER answer a question about the applicant's own facts or choices: work authorization, "
            "visa, sponsorship, citizenship, salary, start date, notice, years of experience, degrees, "
            "demographics or EEO, disability, veteran status, criminal history, relocation, travel, "
            "how they heard about the job, consents and acknowledgements. Leave those OUT; the "
            "applicant answers them. Never invent facts.\n"
            "- NEVER write free text (no answers in text boxes or textareas).\n"
            "- Only for a plainly non-personal dropdown/radio choice may you put the EXACT option "
            "text in \"value\".\n"
            "- If you cannot confidently fill a field, leave it OUT (never guess).\n"
            "- One action per field, in form order.\n"
            "Return ONLY JSON: {\"actions\":[{\"ref\":\"...\",\"op\":\"fill|select|check|upload|skip\","
            "\"source\":\"<key or omit>\",\"value\":\"<literal or omit>\"}, ...]}."
        )
        user = (f"ROLE: {role or '(unknown)'}  COMPANY: {company or '(unknown)'}\n"
                f"AVAILABLE PLACEHOLDER KEYS: {keys}\n"
                f"JOB CONTEXT (for choosing option text): {(jd or '')[:1200]}\n\n"
                f"FORM FIELDS (JSON):\n{json.dumps(fields)[:9000]}")
        raw = self._complete(system, user, max_tokens=1800, effort="low")
        data = self._parse_json_object(raw)
        return data if isinstance(data, dict) else {"actions": []}

    def agent_step(self, system: str, user: str) -> str:
        """One step of the apply agent (autoapply/brain.py). The brain owns the prompt and the
        validation; this is only the call: a short JSON reply, low effort, so a Workday run of
        twenty steps stays at a few cents. Goes through _complete, so BrokerLLM inherits it."""
        return self._complete(system, user, max_tokens=700, effort="low")

    def app_assistant(self, system: str, user: str) -> str:
        """The SponsorJobs assistant's reply in the Telegram chat (notify/assistant.py owns the
        prompt and checks the reply). Short, low effort. Goes through _complete, so BrokerLLM
        inherits it."""
        from tailoring.conform import strip_ai_dashes
        return strip_ai_dashes(self._complete(system, user, max_tokens=300, effort="low").strip())

    def ask_reply(self, system: str, user: str) -> str:
        """Read the person's reply to the apply agent's question(s) (autoapply/asks.py owns the
        prompt and the validation). A short JSON reply at low effort."""
        return self._complete(system, user, max_tokens=300, effort="low")

    def map_labels(self, system: str, user: str) -> str:
        """Match new form wordings to the applicant brain's keys (applicant/mapper.py owns the
        prompt, the redaction and the validation). Labels, options and key names only, never a
        value. A short JSON reply at low effort."""
        return self._complete(system, user, max_tokens=400, effort="low")

    def classify_company(self, system: str, user: str) -> str:
        """Label an employer for the person's resume choices (applicant/scope.py owns the
        prompt): the company name and the start of its ad, never anything about the person. A
        short JSON reply at low effort, once per company."""
        return self._complete(system, user, max_tokens=60, effort="low")

    def draft_sponsorship_answer(self, profile: dict, status: str = "", future_need: str = "",
                                 role: str = "", company: str = "") -> str:
        """Draft a strong, honest spoken answer to the visa/sponsorship question international
        students dread: state the current status plainly and confidently, address future need
        directly, then pivot to the value they bring, grounded ONLY in their profile. Never invents
        a status: it uses exactly what the person gives."""
        system = (
            "You help an international student answer the interview question they dread most: work "
            "authorization and visa sponsorship. Write a SHORT spoken answer, 45 to 75 words, that:\n"
            "1) states their current work authorization plainly and confidently (use EXACTLY what "
            "they give, never invent or assume a status),\n"
            "2) addresses any future sponsorship need directly and honestly, no dodging,\n"
            "3) pivots immediately to the concrete value they bring, grounded ONLY in their profile.\n"
            "Calm and confident. No apologizing, no over-explaining, no immigration-law detail dumps, "
            "no 'I am the perfect candidate'. First person, natural and spoken. Return ONLY the "
            "answer text, no preamble."
        )
        who = (f"CURRENT WORK AUTHORIZATION: {status or '(not given, keep this part general and honest)'}\n"
               f"WILL NEED FUTURE SPONSORSHIP: {future_need or '(not given)'}\n")
        role_line = f"ROLE: {role}\nCOMPANY: {company}\n" if (role or company) else ""
        user = who + role_line + f"\nAPPLICANT PROFILE (JSON):\n{json.dumps(profile)[:4000]}"
        return self._complete(system, user, max_tokens=300, effort="medium")

    def assess_interview_readiness(self, profile: dict, role: str, company: str,
                                   answers: list[dict]) -> dict:
        """After a full mock run, give an honest overall read on how ready the person is for THIS
        interview, from all their answers together. Grounds in what they actually said and their
        profile; never inflates. This is the payoff at the end of a mock session."""
        system = (
            "You are an interview coach giving a candidate an honest overall readiness read after a "
            "full mock interview for a specific role. Judge ALL their answers together.\n"
            "- readiness: one of exactly 'not yet', 'getting there', or 'ready'. Be honest, thin or "
            "evasive answers are 'not yet'; do not flatter.\n"
            "- summary: 2-3 sentences on where they stand and the single most important thing to fix "
            "before the interview, grounded in what they actually said.\n"
            "- strengths: 2-3 real strengths shown across the answers.\n"
            "- gaps: 2-4 concrete things to work on before the interview.\n"
            "Ground everything in their real answers and PROFILE; never invent experience or inflate "
            "how ready they are.\n"
            "Return ONLY JSON: {\"readiness\":\"...\",\"summary\":\"...\",\"strengths\":[\"...\"],"
            "\"gaps\":[\"...\"]}."
        )
        qa = "\n\n".join(f"Q: {a.get('q','')}\nA: {a.get('a','')}" for a in (answers or []))
        who = f"ROLE: {role}\n" + (f"COMPANY: {company}\n" if company else "")
        user = (who + f"\nTHEIR MOCK-INTERVIEW ANSWERS:\n{qa[:6000]}\n\n"
                f"CANDIDATE PROFILE (JSON):\n{json.dumps(profile)[:4000]}")
        raw = self._complete(system, user, max_tokens=900, effort="medium")
        data = self._parse_json_object(raw)
        if not isinstance(data, dict):
            return {"readiness": "", "summary": "Couldn't assess this time, try again.",
                    "strengths": [], "gaps": []}
        from tailoring.conform import strip_ai_dashes
        readiness = str(data.get("readiness", "")).strip().lower()
        if readiness not in ("not yet", "getting there", "ready"):
            readiness = "getting there"
        return {
            "readiness": readiness,
            "summary": strip_ai_dashes(str(data.get("summary", "")).strip()),
            "strengths": [strip_ai_dashes(str(x).strip()) for x in (data.get("strengths") or []) if str(x).strip()],
            "gaps": [strip_ai_dashes(str(x).strip()) for x in (data.get("gaps") or []) if str(x).strip()],
        }

    def score_interview_answer(self, profile: dict, question: str, transcript: str,
                               delivery: dict) -> dict:
        """Score ONE recorded practice answer from its local transcript (P2). PRACTICE feedback,
        never a hiring decision. Judges content honestly and factors the delivery signals."""
        system = (
            "You score ONE recorded PRACTICE interview answer, from a local transcript. This is "
            "practice feedback to help someone improve, NEVER a hiring decision. Judge honestly:\n"
            "- relevance, structure (STAR), specificity: each an integer 0-5.\n"
            "- honesty: '' unless the answer claims something the profile can't support (then name it).\n"
            "- score: an integer 0-100 for THIS answer.\n"
            "- feedback: 2-3 concrete, kind sentences grounded in what they actually said.\n"
            "- delivery_note: one line on pace / length / filler, using the DELIVERY signals.\n"
            "Never invent experience. Return ONLY JSON {\"score\":int,\"relevance\":int,"
            "\"structure\":int,\"specificity\":int,\"honesty\":\"\",\"feedback\":\"...\","
            "\"delivery_note\":\"...\"}."
        )
        d = delivery or {}
        user = (f"QUESTION:\n{(question or '')[:600]}\n\n"
                f"DELIVERY SIGNALS: {d.get('words', 0)} words, about {d.get('speak_sec', 0)}s spoken, "
                f"{d.get('fillers', 0)} filler words.\n\n"
                f"THEIR ANSWER (transcript):\n{(transcript or '')[:2500]}\n\n"
                f"CANDIDATE PROFILE (JSON):\n{json.dumps(profile)[:3000]}")
        raw = self._complete(system, user, max_tokens=700, effort="medium")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes

        def _i(v, lo, hi, default=0):
            try:
                return max(lo, min(hi, int(round(float(v)))))
            except (TypeError, ValueError):
                return default
        return {
            "score": _i(data.get("score"), 0, 100),
            "relevance": _i(data.get("relevance"), 0, 5),
            "structure": _i(data.get("structure"), 0, 5),
            "specificity": _i(data.get("specificity"), 0, 5),
            "honesty": strip_ai_dashes(str(data.get("honesty", "")).strip()),
            "feedback": strip_ai_dashes(str(data.get("feedback", "")).strip()),
            "delivery_note": strip_ai_dashes(str(data.get("delivery_note", "")).strip()),
        }

    def score_interview_screen(self, role: str, company: str, scored_answers: list[dict]) -> dict:
        """Aggregate a recorded practice screen into an overall result (P2): score, pass/fail
        against a stated practice threshold, why, and ranked how-to-improve. PRACTICE only."""
        system = (
            "You give the OVERALL result of a recorded PRACTICE interview screen. This is practice "
            "feedback to help someone improve, NOT a real employer decision. Judge all answers "
            "together, honestly (thin or evasive answers do not pass):\n"
            "- score: an integer 0-100 overall.\n"
            "- passed: true ONLY if the practice bar is met (score >= 70).\n"
            "- why: 2-3 honest sentences on the result.\n"
            "- improvements: 3-5 concrete, ranked, do-this-next items.\n"
            "Return ONLY JSON {\"score\":int,\"passed\":bool,\"why\":\"...\",\"improvements\":[\"...\"]}."
        )
        qa = "\n\n".join(f"Q: {a.get('q', '')}\nSCORE: {a.get('score', 0)}\n"
                         f"TRANSCRIPT: {str(a.get('transcript', ''))[:600]}" for a in (scored_answers or []))
        who = f"ROLE: {role}\n" + (f"COMPANY: {company}\n" if company else "")
        raw = self._complete(system, who + f"\nANSWERS:\n{qa[:6000]}", max_tokens=800, effort="medium")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes
        try:
            score = max(0, min(100, int(round(float(data.get("score", 0))))))
        except (TypeError, ValueError):
            score = 0
        return {
            "score": score,
            "passed": bool(data.get("passed")) if "passed" in data else score >= 70,
            "threshold": 70,
            "why": strip_ai_dashes(str(data.get("why", "")).strip()),
            "improvements": [strip_ai_dashes(str(x).strip())
                             for x in (data.get("improvements") or []) if str(x).strip()],
            "assessment_disclaimer": "This is practice feedback to help you improve, not a real "
                                     "hiring decision or an employer's assessment.",
        }

    def generate_screen_questions(self, profile: dict, role: str, company: str = "",
                                  jd: str = "", kinds: list[str] | None = None,
                                  projects: list[dict] | None = None) -> dict:
        """Exactly N questions for a HireVue-style one-way recorded screen (Round 1), one per
        requested kind, in order: 'motivation' (why this company/role), 'behavioural' (STAR, from
        their real past), 'situational' (a realistic what-would-you-do for THIS job) or 'technical'
        (a light JD-specific practical question). Grounded in the JD and the person's profile/CV."""
        kinds = [str(k) for k in (kinds or ["motivation", "behavioural", "behavioural",
                                            "behavioural", "situational"])]
        n = len(kinds)
        want = "\n".join(f"{i + 1}. kind={k}" for i, k in enumerate(kinds))
        system = (
            "You write the questions for a HireVue-style ONE-WAY recorded video screen the person "
            f"is practising for. Produce EXACTLY {n} questions, in this order and of these kinds:\n"
            + want + "\n"
            "Kinds: 'motivation' = why this company and this role (name the company if given); "
            "'behavioural' = 'tell me about a time...' answerable with STAR from the person's REAL "
            "background; 'situational' = a realistic 'what would you do if...' for this job; "
            "'technical' = one light, practical, JD-specific question (no whiteboard puzzles).\n"
            "Ground every question in what the JOB DESCRIPTION emphasizes and what THIS person can "
            "answer from their real material. Each question must be answerable out loud in about "
            "two minutes. For each: q, kind (exactly the kind requested), competency (the one skill "
            "it probes, 1 to 3 words), why (one line on what the screener is really probing). Do NOT "
            "write answers.\n"
            "Return ONLY JSON: {\"questions\": [{\"q\":\"...\",\"kind\":\"...\",\"competency\":\"...\","
            "\"why\":\"...\"}, ...]}."
        )
        if projects:
            from llm.base import project_facts_text
            k = min(2, len(projects), max(1, n - 3))
            system += ("\nThe candidate's resume for this job includes the verified project(s) under "
                       f"PROJECTS THEY BUILT. Make exactly {k} of the behavioural or technical "
                       "questions about them (one per project), grounded in the facts given: why it "
                       "was built that way, a trade-off, or what breaks at scale. Name the project.")
        who = f"ROLE: {role}\n" + (f"COMPANY: {company}\n" if company else "")
        user = (who + (f"JOB DESCRIPTION:\n{jd[:3000]}\n\n" if jd else "\n")
                + f"CANDIDATE PROFILE (JSON):\n{json.dumps(profile)[:6000]}"
                + (f"\n\n{project_facts_text(projects)}" if projects else ""))
        raw = self._complete(system, user, max_tokens=1000, effort="medium")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes
        out = []
        for q in (data.get("questions") or [])[:n + 2]:
            if not isinstance(q, dict) or not str(q.get("q", "")).strip():
                continue
            out.append({k: strip_ai_dashes(str(q.get(k, "")).strip())
                        for k in ("q", "kind", "competency", "why")})
        return {"questions": out}

    def check_star_coverage(self, question: str, transcript: str) -> dict:
        """The report card's per-answer rubric: did the answer carry each STAR part, how fully did
        it answer the question asked (0 to 3), and one sentence of evidence. Strict JSON, judged
        from the transcript alone. PRACTICE feedback, never a hiring decision."""
        system = (
            "You grade ONE practice interview answer against a strict rubric. Judge only what was "
            "actually said.\n"
            "- star: for each of situation, task, action, result: true if that part is clearly "
            "present in the answer, else false.\n"
            "- coverage: an integer 0 to 3 for how fully the answer addresses the QUESTION ASKED: "
            "0 = did not answer it, 1 = touched it, 2 = answered most of it, 3 = answered it fully "
            "and specifically.\n"
            "- evidence: ONE short sentence quoting or pointing at the part of the answer that "
            "decided the coverage rating. Address the candidate directly as \"you\" (\"You said...\"), "
            "never \"he\", \"she\" or \"they\": the person reads this about themselves.\n"
            "Return ONLY JSON: {\"star\":{\"situation\":bool,\"task\":bool,\"action\":bool,"
            "\"result\":bool},\"coverage\":int,\"evidence\":\"...\"}."
        )
        user = (f"QUESTION:\n{(question or '')[:600]}\n\n"
                f"ANSWER (transcript):\n{(transcript or '')[:3000]}")
        raw = self._complete(system, user, max_tokens=300, effort="low")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes
        star = data.get("star") if isinstance(data.get("star"), dict) else {}
        try:
            cov = max(0, min(3, int(round(float(data.get("coverage"))))))
        except (TypeError, ValueError):
            cov = None
        return {"star": {k: bool(star.get(k)) for k in ("situation", "task", "action", "result")},
                "coverage": cov,
                "evidence": strip_ai_dashes(str(data.get("evidence", "")).strip())}

    def interview_goals(self, profile: dict, role: str, company: str = "", jd: str = "",
                        cv: str = "") -> dict:
        """The prep checklist for one job, built once and cached: the 5 questions this JD most
        likely leads to, the 3 things from the person's tailored CV to make sure they mention,
        and the facts about the company the JD itself reveals (nothing invented)."""
        system = (
            "You prepare a candidate for an interview for a specific job. From the JOB DESCRIPTION "
            "and the candidate's TAILORED CV / profile, produce:\n"
            "- questions: the 5 questions this interviewer is MOST likely to ask, in the order they "
            "would come up, each specific to what the JD emphasizes.\n"
            "- mentions: the 3 things from the candidate's OWN material they must make sure to "
            "mention, each tied to a JD requirement. Only material that is really in the CV / "
            "profile; never invent.\n"
            "- company_facts: up to 5 short facts about the company or team that the JD ITSELF "
            "states (products, mission, team size, customers, stage, location). Only what the JD "
            "says; if it says nothing, return an empty list.\n"
            "Plain sentences, no dashes. Return ONLY JSON: {\"questions\":[...],\"mentions\":[...],"
            "\"company_facts\":[...]}."
        )
        who = f"ROLE: {role}\n" + (f"COMPANY: {company}\n" if company else "")
        user = (who + (f"\nJOB DESCRIPTION:\n{(jd or '')[:4000]}\n" if jd else "\n")
                + (f"\nTAILORED CV (text):\n{(cv or '')[:4000]}\n" if cv else "")
                + f"\nCANDIDATE PROFILE (JSON):\n{json.dumps(profile)[:4000]}")
        raw = self._complete(system, user, max_tokens=900, effort="medium")
        data = self._parse_json_object(raw) or {}
        from tailoring.conform import strip_ai_dashes

        def _list(key, n):
            return [strip_ai_dashes(str(x).strip()) for x in (data.get(key) or [])
                    if str(x).strip()][:n]
        return {"questions": _list("questions", 5), "mentions": _list("mentions", 3),
                "company_facts": _list("company_facts", 5)}

    @staticmethod
    def _parse_profile_json(raw: str, essentials: dict) -> dict:
        """Parse the model's JSON, tolerating code fences; fall back to essentials."""
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            nl = text.find("\n")
            if nl != -1:
                text = text[nl + 1:]
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            try:
                data = json.loads(text[start: end + 1])
                if isinstance(data, dict) and data.get("experience"):
                    # Always trust the person's essentials for identity/education.
                    data["identity"] = essentials.get("identity", data.get("identity", {}))
                    if essentials.get("education"):
                        data["education"] = essentials["education"]
                    return data
            except json.JSONDecodeError:
                pass
        # Fall back: at least return a valid, if sparse, profile.
        return dict(essentials)
