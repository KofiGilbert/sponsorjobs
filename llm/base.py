"""LLM backend protocol and a deterministic fake for offline tests.

Phase 1 is required to be "testable offline with a sample TEMPLATE and a saved
PROFILE, no job board or browser needed" (CLAUDE.md §12). The engine therefore
depends only on this small, prompt-free interface. The real Anthropic client
(``llm/anthropic_client.py``) implements the same three methods by building
prompts and parsing responses; the :class:`FakeLLM` implements them with pure,
deterministic string logic so the acceptance tests never touch the network.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMBackend(Protocol):
    """The only surface the tailoring engine and intake depend on."""

    def generate_intake_questions(
        self, jd_text: str, saved_profile: dict | None
    ) -> list[str]:
        """Return JD-driven intake questions (CLAUDE.md §4a).

        Questions must be derived from *this* JD, not a fixed generic list.
        """

    def reword_bullet(
        self,
        original_text: str,
        supported_jd_terms: list[str],
        target_len_chars: int,
        jd_text: str,
    ) -> str:
        """Reword one résumé bullet toward the JD (CLAUDE.md §8).

        Must weave in supported JD terms using the JD's own phrasing and stay
        within the per-bullet length budget (``target_len_chars``).
        """

    def shorten_bullet(self, text: str, max_len_chars: int) -> str:
        """Shorten a bullet to fit ``max_len_chars`` for the self-heal loop.

        Must actually reduce length while preserving meaning as far as possible.
        """

    def expand_bullets(
        self, role_title: str, bullets: list[str], jd_text: str
    ) -> list[str]:
        """Turn a role's raw notes into strong, complete, page-width bullets.

        Given the person's real bullets for one role (often terse fragments or a
        single comma-list that merges several distinct accomplishments), return one
        COMPLETE, full-line sentence PER DISTINCT accomplishment present in the
        input, splitting merged lists, expanding fragments, keeping already-full
        bullets. Must NOT invent new accomplishments or verifiable facts (numbers,
        dates, employer/product names); it re-presents the person's own content.
        """

    def extract_intake(self, jd_text: str, known: dict, history: list[dict]) -> dict:
        """Run one turn of the intelligent intake conversation (CLAUDE.md §4a).

        Given the target job, what's already known (``known`` = essentials:
        identity/education/experience), and the full chat ``history`` (a list of
        ``{"role": "user"|"agent", "content": str}``), understand the person's
        latest, possibly-messy message, several degrees at once, a link on the
        wrong line, casual phrasing, extract the fields, MERGE them into what's
        known, and decide whether enough has been gathered to draft the CV.

        Returns ``{"essentials": {...full merged...}, "reply": str, "ready": bool}``.
        Companies, dates, and schools are kept exactly as the person stated them.
        """

    def draft_profile(self, jd_text: str, essentials: dict) -> dict:
        """Draft the *content-heavy* parts of a PROFILE from the JD + essentials.

        ``essentials`` carries only what the person supplied and the agent can't
        infer, identity, education (schools/degrees/dates), and an experience
        skeleton (companies, titles, employment dates). This method drafts the
        rest, skills, project entries, experience bullets, extracurriculars,
        interests, matched to the JD, and may suggest a JD-aligned variant of a
        job **title** (flagged for the person to confirm; never a company/date).

        Returns a full profile dict ready for the assembler. Reworded titles are
        marked with ``title_suggested: True`` and ``title_original: <as given>``.
        """

    def ask_enrich(self, topics: list[str], answered: list[str], role: str = "",
                   guidance: dict | None = None, person: dict | None = None) -> str:
        """Ask the person, conversationally, for REAL material to fill the CV out
        to the template's fullness (CLAUDE.md §8): 2-3 accomplishments per role,
        real projects, skills, certifications, extracurriculars/interests.

        ``guidance`` (optional) carries the selected template's interview style and,
        per topic, its goal + example leading questions from the template manifest, material to phrase NATURALLY (indirect, friend-like), not a script to recite.

        Same manners as :meth:`ask_missing`: at most two topics at a time, warmly
        acknowledge what was just added, never re-ask, and always leave an easy
        out ("skip" / "that's everything"), nothing here is required, and no
        section is ever fabricated to fill space.
        """

    def absorb_enrichment(self, jd_text: str, essentials: dict, message: str) -> dict:
        """Fold the person's REAL enrichment answer into ``essentials`` and return
        it: accomplishment bullets attached to the role they describe, real
        projects, a skills list, certifications, extracurriculars, interests.

        Invents nothing, only what the message actually states. Bullets attach to
        the experience entry whose company the message names (or the sole/most-
        recent role when unambiguous).
        """

    def draft_cover_letter(
        self, jd_text: str, profile: dict, role: str = "", company: str = "",
        tone: str = "professional",
    ) -> str:
        """Draft a concise, truthful cover letter for this JD from the person's PROFILE
        (CLAUDE.md §5 drafting).

        Uses ONLY facts the profile supports, never invents employers, metrics, dates,
        or claims the material doesn't back. Mirrors the JD's language for skills the
        person genuinely has (ATS alignment), in 3-4 short paragraphs. Returns the
        letter body as plain text (no preamble, no markdown fences).
        """

    def answer_screening_questions(
        self, jd_text: str, profile: dict, questions: list[str]
    ) -> list[str]:
        """Answer each free-text screening question from the JD + PROFILE, in order.

        One first-person answer per input question (2-5 sentences), grounded ONLY in
        what the profile supports. When the profile lacks the material to answer
        truthfully, says so plainly rather than inventing. Returns a list aligned to
        ``questions``.
        """

    def draft_email_reply(self, subject: str, body: str, sender: str, profile: dict) -> str:
        """Draft a concise, professional reply to a recruiter's email (CLAUDE.md §5),
        grounded ONLY in the person's PROFILE.

        Answers what the message asks, interest, availability, a question, in a few
        sentences, never inventing facts (specific dates, employers, availability) the
        profile doesn't state. First person, signed with the person's name. Returns the
        reply body as plain text (no preamble, no markdown). The person reviews and sends.
        """


# --------------------------------------------------------------------------- #
# Deterministic fake used by the acceptance tests.
# --------------------------------------------------------------------------- #

# Strong résumé action verbs, a clause that leads with one reads as a real
# accomplishment worth turning into a bullet (used by the offline enrichment
# capture; the real model does this far better).
_ACTION_VERBS = (
    "built", "led", "designed", "developed", "created", "launched", "shipped",
    "managed", "owned", "drove", "delivered", "automated", "analyzed", "analysed",
    "improved", "implemented", "migrated", "scaled", "reduced", "increased", "grew",
    "cut", "boosted", "optimized", "optimised", "architected", "engineered",
    "deployed", "maintained", "mentored", "coordinated", "streamlined", "established",
    "introduced", "spearheaded", "orchestrated", "generated", "saved", "wrote",
    "ran", "founded", "organized", "organised", "presented", "published", "trained",
)
_ACTION_RE = re.compile(r"\b(?:" + "|".join(_ACTION_VERBS) + r")\b", re.I)

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#.\-]*")


def _jd_role(jd_text: str) -> str:
    """The role title the JD is for (e.g. 'Product Manager'), from its first line, used as a sensible flagged title suggestion instead of a lone JD keyword."""
    first = next((ln.strip() for ln in (jd_text or "").splitlines() if ln.strip()), "")
    role = re.split(r"\s+[, \-|·]\s+|\s+at\s+", first)[0]
    role = re.split(r"[.,;:(]", role)[0].strip()
    return role if 0 < len(role.split()) <= 6 else ""


def _trim_to_len(text: str, max_len: int) -> str:
    """Trim ``text`` to at most ``max_len`` chars on a word boundary."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    # Back off to the last whitespace so we don't slice a word in half.
    sp = cut.rfind(" ")
    if sp > 0:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-")


def _profile_skill_phrases(profile: dict) -> list[str]:
    """Flat list of the person's skills from a profile's ``skills`` block (dict of
    labeled lines, or a list), real material to ground drafted prose in."""
    skills = (profile or {}).get("skills") or {}
    out: list[str] = []
    if isinstance(skills, dict):
        for line in skills.values():
            out.extend(t.strip() for t in str(line).split(",") if t.strip())
    elif isinstance(skills, (list, tuple)):
        out.extend(str(s).strip() for s in skills if str(s).strip())
    return out


def _profile_first_role(profile: dict) -> str:
    """A short 'Title at Org' for the person's most prominent experience, or ''."""
    for e in (profile or {}).get("experience") or []:
        roles = e.get("roles") or [e]
        title = (roles[0] or {}).get("title") or e.get("title")
        org = e.get("org") or e.get("company")
        if title and org:
            return f"{title} at {org}"
        if title:
            return str(title)
    return ""


class FakeLLM:
    """A deterministic, network-free stand-in for a real LLM.

    Its behaviour is intentionally simple and predictable so tests can assert
    exact outcomes:

    * ``generate_intake_questions`` builds questions out of the JD's own salient
      terms, so two different JDs yield different, role-relevant questions.
    * ``reword_bullet`` prepends the supported JD terms (mirroring the JD's
      wording) to a length-budgeted version of the original bullet, modelling
      "aggressive rewording toward the JD" without inventing facts.
    * ``shorten_bullet`` genuinely shortens, so the self-heal loop converges.
    """

    def __init__(self, max_questions: int = 4) -> None:
        self.max_questions = max_questions

    # -- intake ---------------------------------------------------------- #
    def generate_intake_questions(
        self, jd_text: str, saved_profile: dict | None
    ) -> list[str]:
        from tailoring.keywords import extract_jd_terms

        terms = extract_jd_terms(jd_text)[: self.max_questions]
        questions: list[str] = []
        for t in terms:
            questions.append(
                f"This role emphasizes {t}. Which of your experiences or "
                f"projects best demonstrate {t}, and should I feature it?"
            )
        # Always confirm the identity/date fields the JD itself can't supply.
        questions.append(
            "What name, contact details, and role dates should appear on this "
            "application?"
        )
        return questions

    # -- tailoring ------------------------------------------------------- #
    def reword_bullet(
        self,
        original_text: str,
        supported_jd_terms: list[str],
        target_len_chars: int,
        jd_text: str,
    ) -> str:
        original_text = original_text.strip()
        # Only surface terms the bullet's own material plausibly supports:
        # a term the original already mentions, or one we can lead with while
        # keeping the original substance. We never invent unsupported claims.
        lead = ", ".join(dict.fromkeys(supported_jd_terms))  # de-dupe, keep order
        if lead:
            candidate = f"{lead}: {original_text}"
        else:
            candidate = original_text
        return _trim_to_len(candidate, max(target_len_chars, len(lead) + 2))

    def expand_bullets(
        self, role_title: str, bullets: list[str], jd_text: str
    ) -> list[str]:
        # Deterministic fake: return the person's bullets unchanged (invents
        # nothing). The real backend does the splitting/expanding; FakeLLM never
        # produces merged comma-lists in the offline suite, so a no-op is faithful.
        return [str(b).strip() for b in bullets if str(b).strip()]

    # -- self-heal ------------------------------------------------------- #
    def shorten_bullet(self, text: str, max_len_chars: int) -> str:
        return _trim_to_len(text, max_len_chars)

    # -- chat edits of a finished CV (notify/cvreview.py) ------------------ #
    def plan_cv_edit(self, instruction: str, outline: str) -> dict:
        """Deterministic planner for the offline suite: reads span ids, a handful of plain
        phrasings, and the outline (``E1.company: Stanbic Bank``) to resolve names."""
        t = (instruction or "").strip()
        low = t.lower()
        ids = {}
        for line in (outline or "").splitlines():
            sid, _, txt = line.partition(": ")
            if sid and not sid.startswith("SECTIONS"):
                ids[sid.strip()] = txt.strip()
        sec_names = {"summary", "education", "skills", "projects", "experience",
                     "extracurricular", "interests"}
        m = re.search(r"move (\w+) (above|before|below|after) (\w+)", low)
        if m and m.group(1) in sec_names and m.group(3) in sec_names:
            key = "before" if m.group(2) in ("above", "before") else "after"
            return {"edits": [{"op": "move_section", "section": m.group(1), key: m.group(3)}]}
        m = re.search(r"add (?:a )?bullet to ([A-Z]+\d+(?:\.R\d+)?)\s*:?\s*(.+)", t, re.I)
        if m:
            return {"edits": [{"op": "add_bullet", "target": m.group(1).upper(),
                               "value": m.group(2).strip()}]}
        m = re.search(r"^(?:drop|remove|delete)\s+([A-Z]+\d+(?:\.\d+)?)\b", t, re.I)
        if m:
            return {"edits": [{"op": "drop", "target": m.group(1).upper()}]}
        m = re.search(r"change ([A-Z]+\d+) (dates|title|location|company|degree|school|date) to (.+)",
                      t, re.I)
        if m:
            return {"edits": [{"op": "set", "target": f"{m.group(1).upper()}.{m.group(2).lower()}",
                               "value": m.group(3).strip().rstrip(".")}]}
        m = re.search(r"change my (title|dates|location) at (.+?) to (.+)", t, re.I)
        if m:
            name = m.group(2).strip().lower()
            for sid, txt in ids.items():
                if sid.endswith(".company") and name in txt.lower():
                    eid = sid.split(".")[0]
                    return {"edits": [{"op": "set", "target": f"{eid}.{m.group(1).lower()}",
                                       "value": m.group(3).strip().rstrip(".")}]}
            return {"edits": [], "clarify": f"Which employer is {m.group(2).strip()}?"}
        if "summary" in low and not re.search(r"\b[EPKX]\d", t):
            return {"edits": [{"op": "rewrite", "target": "S", "instruction": t}]}
        found = re.findall(r"\b((?:ED|[EPKX])\d+(?:\.R\d+)?(?:\.(?:\d+|[a-z]+))?|S|I)\b", t)
        if found:
            m = re.search(r"\bto\s*:\s*(.+)$|\bwith\s*:\s*(.+)$", t, re.I)
            if m:
                return {"edits": [{"op": "set", "target": found[0],
                                   "value": (m.group(1) or m.group(2)).strip()}]}
            return {"edits": [{"op": "rewrite", "target": f, "instruction": t}
                              for f in dict.fromkeys(found)]}
        return {"edits": [], "clarify": ""}

    def edit_cv_spans(self, instruction: str, targets: dict, context: dict,
                      jd_text: str = "", budgets: dict | None = None) -> dict:
        """Deterministic span editor: 'shorter' trims, 'use the word X' works X in, anything
        else leaves the wording as is. Only the target ids come back."""
        low = (instruction or "").lower()
        out = {}
        for sid, text in (targets or {}).items():
            text = str(text)
            budget = int((budgets or {}).get(sid) or max(len(text), 165))
            new = text
            m = re.search(r"use (?:the )?(?:word|term|phrase) [\"']?([\w+#./ ]+?)[\"']? in", instruction or "", re.I)
            if m and m.group(1).lower() not in text.lower():
                word = m.group(1).strip()
                new = _trim_to_len(f"{word}: {text}", budget)
            if "shorter" in low or "shorten" in low or "tighter" in low:
                new = _trim_to_len(new, max(20, int(len(new) * 0.7)))
                if not new.endswith("."):
                    new = new.rstrip(" ,;:") + "."
            out[sid] = new
        return out

    # -- conversational gate --------------------------------------------- #
    def ask_missing(self, missing: list[str], answered: list[str], role: str = "",
                    declined: list[str] | None = None) -> str:
        """One short, natural message: neutrally acknowledge the last response (which
        may include things the person said they DON'T have, never praise those as if
        provided), then ask for at most TWO still-missing items."""
        take = list(missing)[:2]
        if not take:
            return ""
        ack = "Got it. " if (answered or declined) else "Thanks! "
        if len(take) == 1:
            body = f"could you tell me {take[0]}?"
        else:
            body = f"could you tell me {take[0]}, and {take[1]}?"
        return ack + body[0].upper() + body[1:]

    # -- transcript courses + project suggestions ------------------------ #
    def select_courses(self, jd_text: str, transcript_text: str) -> list[str]:
        """Extract the REAL courses from a transcript and return the ones most
        relevant to the JD (deterministic stand-in; the real backend does better)."""
        from tailoring.keywords import skill_terms

        skills = [s.lower() for s in skill_terms(jd_text)]
        jd_words = {w for w in re.findall(r"[a-z]{4,}", jd_text.lower())}
        _NOISE = ("transcript", "gpa", "semester", "fall", "spring", "winter", "summer",
                  "university", "college", "total", "grade", "credits", "credit", "units",
                  "name", "student", "cumulative", "term", "degree", "major", "quarter")
        cands: list[str] = []
        for raw in re.split(r"[\n;]+", transcript_text or ""):
            line = raw.strip()
            line = re.sub(r"\b[A-Z]{2,4}\s?\d{2,4}[A-Z]?\b", "", line)          # course codes
            line = re.sub(r"\b\d+(?:\.\d+)?\s*(?:credits?|units?|hrs?)\b", "", line, flags=re.I)
            line = re.sub(r"\s+[A-D][+-]?\s*$", "", line).strip(" .\t-|:")       # trailing grade
            low = line.lower()
            if len(line) < 4 or len(line) > 70 or any(n in low for n in _NOISE):
                continue
            title_words = sum(1 for w in line.split() if w[:1].isupper())
            if title_words >= 2 or any(s in low for s in skills):
                cands.append(line)
        cands = list(dict.fromkeys(cands))

        def score(c: str) -> int:
            low = c.lower()
            return (sum(2 for s in skills if s in low)
                    + sum(1 for w in re.findall(r"[a-z]{4,}", low) if w in jd_words))
        relevant = [c for c in cands if score(c) > 0]
        ranked = sorted(relevant or cands, key=score, reverse=True)
        return ranked[:8]

    def suggest_projects(self, jd_text: str) -> list[dict]:
        """Draft 1-2 sample JD-matching projects as persona reference. Each is
        flagged ``suggested`` so it cannot survive into a finalized CV."""
        from tailoring.keywords import skill_terms

        sk = skill_terms(jd_text) or ["Python"]
        a, b = sk[0], (sk[1] if len(sk) > 1 else sk[0])
        return [
            {"org": f"{a} Mini-Project (public data)", "location": "Personal Project",
             "dates": "2024", "suggested": True,
             "link": "", "how": f"Build a small {a} project on a public dataset and put it on GitHub.",
             "bullets": [f"Built a small {a} project on public data applying {b}, "
                         f"and published the code and results on GitHub."]},
        ]

    # -- intelligent intake --------------------------------------------- #
    def extract_intake(self, jd_text: str, known: dict, history: list[dict]) -> dict:
        """Deterministic stand-in for a conversational extractor.

        Parses only the *latest* user message and merges it into ``known``, so
        it never re-parses old turns, using simple, predictable rules that the
        acceptance tests can rely on. The real backend understands free text far
        better; this just proves the plumbing offline.
        """
        ess = {
            "identity": dict(known.get("identity", {})),
            "education": list(known.get("education", [])),
            "experience": list(known.get("experience", [])),
        }
        msg = ""
        for h in reversed(history or []):
            if h.get("role") == "user":
                msg = h.get("content", "")
                break

        # Links anywhere in the message → routed to the right identity slot.
        for url in re.findall(r"(?:https?://|www\.)\S+|\b(?:github|linkedin)\.com/\S+", msg, re.I):
            low = url.lower()
            if "linkedin" in low:
                ess["identity"]["linkedin"] = url
            elif "github" in low:
                ess["identity"]["github"] = url
            else:
                ess["identity"]["blog"] = url
        clean = re.sub(r"\S*(?:https?://|github\.com|linkedin\.com)\S*", " ", msg, flags=re.I)

        em = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", clean)
        if em:
            ess["identity"]["email"] = em.group(0).rstrip(".")   # drop a sentence period
            clean = clean.replace(em.group(0), " ")
        nm = re.search(
            r"(?:[Ii]'?m|[Ii] am|[Mm]y name is|[Tt]his is)\s+"
            r"([A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+){0,2})",
            msg,
        )
        if nm and not ess["identity"].get("name"):
            ess["identity"]["name"] = nm.group(1).strip()
        from tailoring.conform import is_full_address, is_region
        # A full mailing address (street number … city, ST[, ZIP]) is what the
        # template header wants; fall back to a bare city only as a placeholder.
        full_addr = re.search(
            r"\b(\d{1,6}\s+[A-Za-z0-9][\w .,'#\-]*?,\s*[A-Za-z][\w .]*,\s*"
            r"[A-Za-z]{2}\.?\s*\d{5})\b", clean)
        # A full address always wins, it replaces a bare-city placeholder captured
        # on an earlier turn (so "Chicago, IL" then a real street address advances).
        if full_addr and not is_full_address(ess["identity"].get("address")):
            ess["identity"]["address"] = full_addr.group(1).strip().rstrip(". ")
        else:
            city = re.search(
                r"(?:based in|live in|in)\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,2},\s*[A-Za-z.]+)",
                clean)
            if city and is_region(city.group(1).rsplit(",", 1)[1]) and not ess["identity"].get("address"):
                ess["identity"]["address"] = city.group(1).strip().rstrip(". ")

        # Word-bounded so "ba" inside "Bank"/"Master" doesn't read as a degree.
        _DEG = r"\b(?:bachelor|master|mba|doctorate|ph\.?\s?d|phd|[bm]\.?[sa]\.?)\b"
        # Split into clauses on newlines, semicolons, sentence boundaries (but not
        # inside abbreviations like "M.S."), and " and ".
        # Split on sentence/clause boundaries, including an orphaned period left by a
        # removed email/link ("Name,  . I'm a Title …") so the name can't leak into a
        # later clause's title.
        for clause in re.split(r"[\n;]+|(?<=[a-z0-9])\.\s+|\s+\.\s+|,?\s+and\s+", clean):
            clause = clause.strip(" .,")
            if not clause:
                continue
            year = re.search(r"\b(19|20)\d\d\b", clause)
            if re.search(_DEG, clause, re.I) and (" at " in f" {clause.lower()} " or year):
                sch = re.search(
                    r"\bat\s+([A-Z][\w&.\-]*(?:\s+(?:of|the|and|for|de|&)?\s*[A-Z][\w&.\-]*)*)",
                    clause)
                deg = re.search(
                    _DEG + r"\.?(?:\s+(?:in|of)\s+[A-Za-z&.\- ]+?)?(?=\s+at\b|\s+from\b|,|$)",
                    clause, re.I)
                # Capture a fuller date if present (optional month, optional range,
                # optional 'graduated' prefix) so it reformats to the template style.
                edate = re.search(
                    r"(?:graduated|grad|expected|class of)?\s*"
                    r"((?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d\d"
                    r"(?:\s*(?:-|, |, |to)\s*(?:[Pp]resent|(?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d\d))?)",
                    clause)
                ess["education"].append({
                    "school": sch.group(1).strip() if sch else "",
                    "degree": (deg.group(0).strip() if deg else clause),
                    "date": (edate.group(1).strip() if edate else
                             (year.group(0) if year else "")), "location": "", "courses": "",
                })
            elif "|" in clause:
                p = [x.strip() for x in clause.split("|")]
                ess["experience"].append({"org": p[0] if p else "", "title": p[1] if len(p) > 1 else "",
                                          "dates": p[2] if len(p) > 2 else "", "location": p[3] if len(p) > 3 else ""})
            elif re.search(r"\bat\s+[A-Z]", clause):
                # Pull a "City, Region" location (real region only) and strip it so
                # it doesn't confuse title/date parsing.
                locm = re.search(
                    r"(?:\bin\s+|\()([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,2},\s*[A-Za-z.]+)\)?",
                    clause)
                location = (locm.group(1).strip()
                            if locm and is_region(locm.group(1).rsplit(",", 1)[1]) else "")
                cl = clause.replace(locm.group(0), " ") if (locm and location) else clause
                org = re.search(
                    r"\bat\s+([A-Z][\w&.\-]*(?:\s+(?:of|the|and|for|de|&)?\s*[A-Z][\w&.\-]*)*)", cl)
                title = (re.search(r"\bas\s+(?:an?\s+)?([\w &/\-]+?)\s+at\b", cl)
                         or re.search(r"^(.*?)\s+at\s+", cl))
                # Dates render as the template's range style (CLAUDE.md §8):
                # an explicit range wins; "since/from X" (an ongoing role) becomes
                # "X - Present"; a bare trailing year stays as-is.
                _yr = r"[A-Za-z]{0,9}\.?\s*(?:19|20)\d\d"
                mrange = re.search(
                    rf"({_yr})\s*(?:-|, |, |\bto\b|\bthrough\b|\buntil\b)\s*"
                    rf"([Pp]resent|[Cc]urrent|[Nn]ow|{_yr})", cl)
                msince = re.search(rf"\b(?:since|from)\s+({_yr})\b", cl, re.I)
                mbare = re.search(rf"(?:,|\bin\b)\s*({_yr})\s*$", cl)
                if mrange:
                    dstr = f"{mrange.group(1).strip()} - {mrange.group(2).strip()}"
                elif msince:
                    dstr = f"{msince.group(1).strip()} - Present"
                elif mbare:
                    dstr = mbare.group(1).strip()
                else:
                    dstr = ""
                ess["experience"].append({
                    "org": org.group(1).strip() if org else "",
                    "title": (title.group(1).strip() if title else ""),
                    "dates": dstr, "location": location,
                })

        low = msg.lower()
        forced = any(k in low for k in (
            "just build", "build it", "that's all", "thats all", "that's everything",
            "go ahead", "tailor it", "nothing new", "nothing changed", "that's it", "im done", "i'm done"))
        ready = bool(ess["identity"].get("name") and ess["experience"]) or forced

        if ready:
            reply = "Great, I've got what I need. Building your resume now."
        else:
            need = []
            if not ess["identity"].get("name"):
                need.append("your name")
            if not ess["education"]:
                need.append("your education")
            if not ess["experience"]:
                need.append("your work history")
            reply = ("Thanks! Could you also share " + ", ".join(need) + "?") if need \
                else "Got it. Anything else to add, or should I build it?"
        return {"essentials": ess, "reply": reply, "ready": ready}

    # -- P1 memory: deterministic stand-ins (real backend does the real work) ----------- #
    def converse(self, jd_text: str, history: list[dict], recalled: list[dict],
                 facts: list[dict]) -> str:
        last = next((t.get("content", "") for t in reversed(history or [])
                     if t.get("role") == "user"), "")
        return f"Got it, I'll remember that. ({str(last)[:60]})".strip()

    def distill_memory(self, history: list[dict], known_keys: list[str]) -> dict:
        """Deterministic distillation: detect the few preference/constraint shapes the offline
        tests rely on from the user turns. The real backend distills far more from free text."""
        text = " ".join(t.get("content", "") for t in (history or []) if t.get("role") == "user")
        low = text.lower()
        known = set(known_keys or [])
        facts: list[dict] = []

        def _add(ftype, key, value, conf=0.85):
            if key and key not in known and not any(f["key"] == key for f in facts):
                facts.append({"type": ftype, "key": key, "value": value, "confidence": conf})

        for m in re.finditer(r"(?:do ?n['’]?t mention|do not mention|leave (?:off|out)|"
                             r"avoid mentioning|please (?:omit|don't mention)|omit)\s+(?:the\s+)?"
                             r"([^.,;\n]{2,60})", low):
            # Trim trailing "on my cv / in my resume" so the phrase is the thing itself.
            phrase = re.split(r"\s+(?:on|in|from)\s+(?:my|the)\b", m.group(1).strip())[0].strip()
            _add("constraint", "avoid_" + re.sub(r"[^a-z0-9]+", "_", phrase).strip("_")[:30],
                 f"do not mention {phrase}")
        for m in re.finditer(r"(?:targeting|target|interested in|care about|focus(?:ed|ing)? on|"
                             r"aiming for|going for)\s+([a-z][a-z0-9 /&+-]{2,30})", low):
            ind = m.group(1).strip().split(" and ")[0].strip()
            if ind and ind not in ("a", "the", "my", "this", "that"):
                _add("preference", "target_industry", ind)
                break
        if re.search(r"\b(?:need|needs|require[sd]?|will need)\b[^.\n]{0,25}"
                     r"\b(?:sponsor|sponsorship|visa)\b", low) or "visa sponsorship" in low:
            _add("constraint", "needs_sponsorship", "needs visa sponsorship")
        return {"facts": facts}

    def select_for_cv(self, jd_text: str, profile: dict, recalled: list[dict],
                      preferences: list[dict]) -> dict:
        """Deterministic selection: include the person's experiences/projects, EXCLUDING any that
        an active 'do not mention' constraint forbids (matched by substring). Records a why for
        each. The real backend reasons about relevance far better."""
        # Distinctive tokens (a year, or a word >= 4 chars) of each active "avoid" constraint;
        # an item is excluded if it contains any of them (substring-tolerant, not exact phrase).
        _stop = {"mention", "resume", "please", "about", "with", "from", "that", "this", "your"}

        def _distinctive(phrase):
            return [t for t in re.findall(r"[a-z0-9]+", phrase.lower())
                    if (t.isdigit() and len(t) == 4) or (len(t) >= 4 and t not in _stop)]
        avoid_tokens = []
        for p in (preferences or []):
            if str(p.get("type")) != "constraint":
                continue
            val = str(p.get("value", "")).lower()
            if not re.search(r"do ?n['’]?t mention|do not mention|avoid|leave (?:off|out)|omit", val):
                continue
            m = re.search(r"(?:mention|avoid|omit|off|out)\s+(?:the\s+)?(.+)", val)
            toks = _distinctive(m.group(1) if m else val)
            if toks:
                avoid_tokens.append((" ".join(toks), set(toks)))
        def _item(e):
            label = str(e.get("org") or e.get("company") or e.get("title") or "").strip()
            parts = [str(e.get(k) or "") for k in ("org", "company", "title", "dates")]
            parts += [str(b) for b in (e.get("bullets") or [])]
            for r in (e.get("roles") or []):   # experiences nest title/dates/bullets under roles
                parts += [str(r.get(k) or "") for k in ("title", "dates")]
                parts += [str(b) for b in (r.get("bullets") or [])]
            return label, " ".join(parts)
        items = [_item(e) for e in (profile.get("experience") or [])]
        items += [_item(p) for p in (profile.get("projects") or [])]
        selected, excluded = [], []
        for label, text in items:
            if not label:
                continue
            itoks = set(re.findall(r"[a-z0-9]+", text.lower()))
            hit = next((lbl for lbl, toks in avoid_tokens if toks & itoks), None)
            if hit:
                excluded.append({"item": label, "why": f"You asked me to leave this off ({hit})."})
            else:
                selected.append({"item": label, "why": "Relevant to this role."})
        return {"selected": selected, "excluded": excluded,
                "summary": f"Selected {len(selected)}, excluded {len(excluded)} per your preferences."}

    def extract_profile(self, cv_text: str) -> dict:
        """Offline stand-in: a fixed, well-formed profile so the CV → profile flow is
        testable without a model. Pulls a name/email out of the text where obvious;
        the real backend parses the actual résumé faithfully."""
        text = cv_text or ""
        em = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        name = ""
        for line in text.splitlines()[:6]:
            s = line.strip()
            if re.fullmatch(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}", s):
                name = s
                break
        return {
            "identity": {"name": name or "Sample Applicant",
                         "email": em.group(0) if em else "", "phone": "", "address": "",
                         "linkedin": "", "github": "", "blog": ""},
            "summary": "Software professional with a track record of shipping reliable, well-tested systems.",
            "education": [{"school": "State University", "degree": "B.S. in Computer Science",
                           "date": "2019", "location": "", "courses": ""}],
            "skills": {"Languages & Tools": "Python, SQL, JavaScript, Git",
                       "Cloud & Data": "AWS, Docker, PostgreSQL"},
            "projects": [{"title": "Portfolio Site", "dates": "2023", "location": "Personal Project",
                          "bullets": ["Built and deployed a personal site."]}],
            "experience": [{"org": "Acme Corp", "location": "Remote",
                            "roles": [{"title": "Software Engineer", "dates": "2020 - Present",
                                       "bullets": ["Shipped features across the stack.",
                                                   "Improved reliability and test coverage."]}]}],
            "extracurricular": [],
            "interests": "Reading, hiking",
        }

    # -- intake drafting ------------------------------------------------- #
    def draft_profile(self, jd_text: str, essentials: dict) -> dict:
        """Deterministically draft a JD-matched profile from essentials.

        Complete-sentence bullets, JD terms woven in so coverage is real, and a
        flagged title suggestion on the first job (to exercise the confirm path).
        """
        from tailoring.keywords import (SKILL_CATEGORIES, classify_skill,
                                        extract_jd_terms, skill_terms)

        terms = extract_jd_terms(jd_text) or ["the role"]

        def term(i: int) -> str:
            return terms[i % len(terms)]

        profile: dict = {
            "identity": dict(essentials.get("identity", {})),
            # Real education verbatim, courses are never fabricated (ask-not-invent).
            "education": [dict(e) for e in essentials.get("education", [])],
        }

        # Real, JD-relevant skills grouped like the template, no company/location
        # filler (never "Beacon, Chicago"). The template's Skills block runs ~4 dense,
        # labeled categories to fill the page (4-5, never more), so the fake matches that.
        grouped: dict = {}
        for t in skill_terms(jd_text):
            grouped.setdefault(classify_skill(t), []).append(t)
        ordered = [t for cat in SKILL_CATEGORIES for t in grouped.get(cat, [])]
        # Four DISJOINT categories (no term repeated across lines, case-insensitively) so
        # the dedup step never empties and drops one, the template runs ~4 dense, distinct
        # categories. Case-fold the pool first so 'ML'/'ml' can't split across buckets.
        seen: set = set()
        pool: list = []
        for t in ordered + [term(i) for i in range(10)]:
            if t.lower() not in seen:
                seen.add(t.lower())
                pool.append(t)
        pool = pool or ["the role"]
        # The CV's real category labels. The test double used to invent its own vocabulary
        # ("Computing"/"Knowledge"), so fake CVs showed rows a real one never would.
        labels = list(SKILL_CATEGORIES)
        buckets: dict = {label: [] for label in labels}
        for idx, tterm in enumerate(pool):
            buckets[labels[idx % len(labels)]].append(tterm)
        profile["skills"] = {label: ", ".join(items) for label, items in buckets.items() if items}

        # Experience: keep company/dates as given; draft bullets; suggest a
        # JD-aligned title on the first entry (flagged).
        from tailoring.conform import clean_title
        jd_role = clean_title(_jd_role(jd_text))
        experience = []
        for idx, job in enumerate(essentials.get("experience", [])):
            org = job.get("org", "the company")
            real_title = clean_title(job.get("title", ""))   # never a conversational fragment
            # Prefer the person's REAL accomplishments (gathered at enrichment); only
            # fall back to drafted prose for a role they haven't detailed yet.
            provided = [str(b).strip() for b in (job.get("bullets") or []) if str(b).strip()]
            entry = {
                "org": org,
                "location": job.get("location", ""),
                "dates": job.get("dates", ""),
                "title": real_title,
                "title_original": real_title,
                "title_suggested": False,
                "drafted_bullets": not provided,
                "bullets": provided or [
                    f"Delivered {term(idx)} initiatives at {org}, improving "
                    f"delivery speed and quality.",
                    f"Partnered across teams on {term(idx + 1)} to drive "
                    f"measurable business outcomes.",
                ],
            }
            if idx == 0 and jd_role and jd_role.lower() != real_title.lower():
                # Suggest the JD's actual role title (not a lone keyword), flagged
                # for the person to confirm or revert.
                entry["title"] = jd_role
                entry["title_suggested"] = True
            experience.append(entry)
        profile["experience"] = experience

        profile["projects"] = [
            {
                "org": "Open Source Project",
                "location": "",
                "title": "Maintainer",
                "dates": "2023 - Present",
                "drafted": True,
                "bullets": [
                    f"Built an open-source project applying {term(0)} and "
                    f"{term(1)}, adopted by the community.",
                ],
            }
        ]
        profile["extracurricular"] = [
            {
                "title": "Community Organizer",
                "date": "2022 - Present",
                "drafted": True,
                "bullets": [
                    f"Organized local talks on {term(0)} for practitioners.",
                ],
            }
        ]
        profile["interests"] = "Reading, hiking, open-source software, mentoring"
        # A leading professional summary (for summary-first templates), synthesized from
        # the drafted profile, no invented facts.
        _role = _profile_first_role(profile) or (jd_role or "professional")
        _sk = _profile_skill_phrases(profile)[:3]
        _sk_bit = ", ".join(_sk) if _sk else "the areas this role needs"
        profile["summary"] = (
            f"{jd_role or 'Professional'} with hands-on experience{(' as ' + _role) if _role else ''} "
            f"and a working command of {_sk_bit}. Turns real requirements into dependable, "
            "well-tested results and partners closely with stakeholders to ship outcomes."
        )
        return profile

    # -- drafting: cover letter & screening answers (CLAUDE.md §5) -------- #
    def draft_cover_letter(self, jd_text: str, profile: dict, role: str = "",
                           company: str = "", tone: str = "professional") -> str:
        ident = (profile or {}).get("identity", {}) or {}
        name = ident.get("name") or "The Applicant"
        role = role or _jd_role(jd_text) or "this role"
        company = company or "your organization"
        skills = _profile_skill_phrases(profile)[:3]
        skill_bit = ", ".join(skills) if skills else "the skills this role calls for"
        exp = _profile_first_role(profile)
        p1 = (f"I am writing to apply for the {role} position at {company}. The role's focus "
              f"aligns closely with my background, and I would be glad to contribute.")
        p2 = (f"My experience{(' as ' + exp) if exp else ''} has given me hands-on work with "
              f"{skill_bit}. I have used these to deliver real results, and I am confident they "
              f"transfer directly to what {company} needs.")
        p3 = ("I would welcome the chance to discuss how I can help your team. Thank you for "
              "considering my application.")
        return f"Dear Hiring Team,\n\n{p1}\n\n{p2}\n\n{p3}\n\nSincerely,\n{name}"

    def draft_referral_message(self, role: str, company: str, profile: dict,
                               jd_text: str = "", recipient_name: str = "",
                               relationship: str = "") -> str:
        ident = (profile or {}).get("identity", {}) or {}
        name = ident.get("name") or "The Applicant"
        role = role or _jd_role(jd_text) or "the open role"
        company = company or "your company"
        skills = _profile_skill_phrases(profile)[:2]
        skill_bit = " and ".join(skills) if skills else "the skills this role calls for"
        greet = f"Hi {recipient_name}," if recipient_name else "Hi there,"
        tie = f" {relationship}." if relationship else ""
        return (f"{greet}\n\nI came across the {role} opening at {company} and it lines up well "
                f"with my background in {skill_bit}.{tie} I would really value your perspective on "
                f"the team, and if it feels right, a referral would mean a lot. Happy to share "
                f"more so it is easy for you. Thanks so much for your time.\n\n{name}")

    def answer_screening_questions(self, jd_text: str, profile: dict,
                                   questions: list[str]) -> list[str]:
        role = _jd_role(jd_text) or "this role"
        skills = _profile_skill_phrases(profile)[:2]
        skill_bit = " and ".join(skills) if skills else "relevant, transferable skills"
        exp = _profile_first_role(profile)
        as_exp = (" as " + exp) if exp else ""
        answers: list[str] = []
        for q in questions:
            ql = (q or "").lower()
            if "why" in ql and any(w in ql for w in ("you", "interest", "want", "join", "company")):
                a = (f"I'm drawn to {role} because it fits my background{as_exp}, where I worked "
                     f"with {skill_bit}. I'd be excited to bring that experience to your team.")
            elif any(w in ql for w in ("strength", "why should", "fit", "best", "unique")):
                a = (f"My strongest fit for {role} is hands-on experience with {skill_bit}"
                     f"{(', built ' + ('as ' + exp)) if exp else ''}. I focus on delivering "
                     f"practical, dependable results.")
            else:
                a = (f"Drawing on my experience{as_exp} with {skill_bit}, I'd approach this by "
                     f"applying what has worked in similar situations and adapting to your context.")
            answers.append(a)
        return answers

    def draft_email_reply(self, subject: str, body: str, sender: str, profile: dict) -> str:
        ident = (profile or {}).get("identity", {}) or {}
        name = ident.get("name") or "The Applicant"
        # Address the sender by first name when the From header carries one.
        m = re.match(r'^\s*"?([^"<@]+?)"?\s*(?:<|$)', (sender or "").strip())
        greet_name = (m.group(1).strip().split() or [""])[0] if m else ""
        greet = f"Hi {greet_name}," if greet_name and "@" not in greet_name else "Hello,"
        topic = (subject or "").strip() or "the role"
        skills = _profile_skill_phrases(profile)[:2]
        skill_bit = " and ".join(skills) if skills else "the areas this role focuses on"
        p1 = (f"Thank you for reaching out about {topic}. I'm very interested and would be "
              f"glad to move forward.")
        p2 = (f"My background in {skill_bit} maps closely to what you described, and I'm happy "
              f"to share more or set up a time to talk, I'm generally flexible on scheduling.")
        p3 = "Please let me know what works best on your end, and thank you again for your time."
        return f"{greet}\n\n{p1} {p2}\n\n{p3}\n\nBest regards,\n{name}"

    # -- Project builder: deterministic stand-ins for the defend-your-work gate --
    def generate_project_test(self, project: dict) -> dict:
        return {"questions": [
            "Why did you choose this approach over an alternative, and what were the trade-offs?",
            "Walk me through how the core part of it actually works.",
            "What did you give up with this design, and what would break as it scaled up?",
            "How would you add a significant new feature to it?",
            "What went wrong or was harder than you expected, and how did you handle it?",
        ]}

    def suggest_buildable_projects(self, profile: dict, target: str = "") -> dict:
        # Deterministic stand-in: echo the target/profile so the flow is testable offline.
        tgt = (target or "").strip() or "your target roles"
        skills = profile.get("skills") if isinstance(profile, dict) else None
        base = ""
        if isinstance(skills, dict) and skills:
            base = next(iter(skills.values()), "") or ""
        elif isinstance(skills, str):
            base = skills
        seed = (base.split(",")[0].strip() if base else "your background")
        return {"suggestions": [
            {"title": "End-to-end pipeline demo", "skill": "data pipelines",
             "summary": f"Build a small service that ingests, transforms, and serves data, "
                        f"extending {seed} toward {tgt}.",
             "tech": "Python, SQLite, Flask", "why": f"Shows hands-on delivery relevant to {tgt}."},
            {"title": "Metrics dashboard", "skill": "reporting",
             "summary": f"A dashboard that turns raw records into the KPIs {tgt} cares about.",
             "tech": "Python, Pandas, a charting lib", "why": f"Demonstrates measurable impact for {tgt}."},
            {"title": "Automation script", "skill": "automation",
             "summary": f"Automate a repetitive task from {seed} with a clean CLI and tests.",
             "tech": "Python, argparse, pytest", "why": f"Proves you can ship reliable tools for {tgt}."},
        ]}

    def generate_build_plan(self, project: dict) -> dict:
        # Deterministic stand-in: a fixed 4-step plan so the guided-build flow is testable offline.
        title = (project.get("title") if isinstance(project, dict) else "") or "the project"
        return {"milestones": [
            {"title": "Scaffold the project", "build": f"Set up the repo and skeleton for {title}.",
             "learn": "How the pieces fit together and why this structure.", "check": "It runs and prints hello."},
            {"title": "Build the core", "build": "Implement the main feature end to end.",
             "learn": "The key algorithm or data flow at the heart of it.", "check": "The core path works on one real input."},
            {"title": "Handle the edges", "build": "Add validation and error handling.",
             "learn": "Why the happy path is not enough and what breaks.", "check": "Bad input fails gracefully, not with a crash."},
            {"title": "Polish and document", "build": "Add a README and a couple of tests.",
             "learn": "How to make it defensible and reproducible.", "check": "A stranger could clone and run it."},
        ]}

    def upskill_plan(self, role: str, gaps: list[str], profile: dict,
                     required: list[str] | None = None) -> dict:
        # Deterministic stand-in: one concrete step per gap so the upskill flow is testable offline.
        # Must-haves (the ``required`` subset) are labelled and sorted to the front, mirroring the
        # real backend, so the smarter-ordering seam is exercisable without a model.
        gaps = [str(g).strip() for g in (gaps or []) if str(g).strip()][:8]
        req = {str(x).strip().lower() for x in (required or [])}
        ordered = sorted(gaps, key=lambda g: 0 if g.lower() in req else 1)   # stable: must-haves first
        return {"plan": [
            {"skill": g, "priority": "must-have" if g.lower() in req else "nice-to-have",
             "effort": "a weekend",
             "how": f"Build a small project that clearly uses {g}, and put it on GitHub.",
             "resource": "a free online course plus a portfolio project", "leverage": ""}
            for g in ordered]}

    def coach_project_step(self, project: dict, milestone: dict, question: str) -> dict:
        # Deterministic stand-in: echo the step so the coach flow is exercisable without a model.
        step = (milestone or {}).get("title", "this step")
        return {"answer": f"For '{step}': think about what you're trying to prove, then take the "
                          f"smallest next step and test it before moving on. (dev coach stand-in)"}

    # -- Interview prep: deterministic stand-ins for the pre-interview practice flow --
    def generate_interview_questions(self, profile: dict, role: str, company: str = "",
                                     jd: str = "", focus: str = "", difficulty: str = "standard") -> dict:
        r = (role or "the role").strip()
        co = (company or "").strip()
        # When the employer is known, one question is tied to THAT company's values, not a generic
        # screen (the real backend reflects the company's actual interview style; this keeps the seam).
        company_q = ([{"q": f"What about {co}'s values and way of working draws you, and where have "
                            f"you shown that?", "type": "motivational", "competency": "Company fit",
                       "why": f"Ties motivation to {co} specifically, not a generic 'why us'."}]
                     if co else [])
        # A hard screen adds a demanding, senior-level probe that makes you defend trade-offs.
        hard_q = ([{"q": f"Give me a senior-level {r} example and defend the trade-offs you made, "
                        f"with numbers.", "type": "behavioral", "competency": "Problem solving",
                    "why": "Rigorous probe: quantify impact and justify decisions."}]
                  if str(difficulty).lower() == "hard" else [])
        if focus:
            # Focused practice set: every question drills the one weak competency.
            return {"questions": [
                {"q": f"Tell me about a time your {focus} made the difference in a {r} task.",
                 "type": "behavioral", "competency": focus, "why": f"Drills {focus}."},
                {"q": f"What would you do if a situation tested your {focus} on this team?",
                 "type": "situational", "competency": focus, "why": f"Drills {focus} under pressure."},
                {"q": f"Where is your {focus} still growing, and how are you working on it?",
                 "type": "motivational", "competency": focus, "why": f"Self-awareness on {focus}."},
            ]}
        return {"questions": company_q + hard_q + [
            {"q": f"Tell me about a time you delivered a result relevant to {r}.", "type": "behavioral",
             "competency": "Ownership", "why": "Checks real, specific experience and ownership."},
            {"q": f"Why this role and what would you do first in it?", "type": "motivational",
             "competency": "Motivation", "why": "Checks motivation and how you'd add value early."},
            {"q": f"What would you do if you disagreed with your manager on an approach?",
             "type": "situational", "competency": "Communication",
             "why": "Checks judgment and how you handle conflict."},
            {"q": f"Walk me through a hard problem you solved and how.", "type": "behavioral",
             "competency": "Problem solving", "why": "Checks problem-solving depth and honesty."},
            {"q": f"What's a skill this {r} needs that you're still growing?", "type": "motivational",
             "competency": "Self-awareness", "why": "Checks self-awareness without overclaiming."},
        ]}

    def assess_cv_worthiness(self, text: str) -> dict:
        t = str(text or "").strip()
        low = t.lower()
        # Worthy when there's a concrete signal: a number/percent, or an accomplishment verb with
        # enough substance. Vague or tiny notes are not.
        has_number = bool(re.search(r"\d", t))
        has_verb = any(v in low for v in _ACTION_VERBS)
        worthy = len(t) >= 40 and (has_number or has_verb)
        if not worthy:
            return {"worthy": False,
                    "reason": "Too vague to use, add a concrete action and a result.",
                    "competency": "", "headline": ""}
        return {"worthy": True, "reason": "A concrete accomplishment worth capturing.",
                "competency": "Ownership" if has_verb else "Impact",
                "headline": t[:100]}

    def coach_interview_answer(self, profile: dict, question: str, answer: str) -> dict:
        a = str(answer or "").strip()
        thin = len(a) < 60
        return {
            "assessment": "Thin, add a specific situation and a measurable result." if thin
                          else "Solid base, tighten the result and keep it concise.",
            "star": {"situation": a[:80], "task": "", "action": "", "result": ""},
            "improve": (["Give a concrete situation and your specific actions.", "End with a measurable result."]
                        if thin else ["Lead with the result.", "Cut filler, keep it under 90 seconds."]),
            "tighter": (a + " and the result was a clear, measurable win.") if a else "",
            "honesty": "",
        }

    def suggest_star_stories(self, profile: dict) -> dict:
        # Deterministic stand-in: turn the person's REAL experience bullets and projects into
        # candidate STAR drafts (action seeded from the bullet; the other fields left as gaps to
        # fill in), so the suggest flow is testable offline. Invents nothing -- it only echoes their
        # own material for them to shape and save.
        out: list[dict] = []

        def add(title: str, action: str, comp: str) -> None:
            action = str(action or "").strip()
            if action:
                out.append({"title": (title or "Experience")[:80], "situation": "", "task": "",
                            "action": action[:400], "result": "",
                            "competencies": [comp] if comp else []})

        for e in (profile.get("experience") or []):
            org = str(e.get("org") or "").strip()
            for r in (e.get("roles") or [e]):
                rt = str(r.get("title") or "").strip()
                title = f"{rt} at {org}" if (rt and org) else (rt or org or "Experience")
                for b in (r.get("bullets") or [])[:2]:
                    add(title, b, "Impact")
        for p in (profile.get("projects") or []):
            title = str(p.get("title") or p.get("org") or "Project").strip()
            for b in (p.get("bullets") or [])[:1]:
                add(title, b, "Problem solving")
        return {"stories": out[:6]}

    def plan_form_fill(self, fields: list[dict], available_keys: list[str], role: str = "",
                       company: str = "", jd: str = "") -> dict:
        # Deterministic stand-in for the autonomous form-filler: map each field to a placeholder KEY
        # by simple label keywords (file inputs -> upload), so the fill loop is testable offline with
        # no model. Unknown fields are skipped, never guessed. Mirrors the real backend's contract:
        # the plan references KEYS only, never real personal values.
        keyset = set(available_keys or [])
        hints = [
            (("first name", "given name"), "first_name"),
            (("last name", "surname", "family name"), "last_name"),
            (("full name", "your name", "name"), "full_name"),
            (("email",), "email"),
            (("phone", "mobile", "telephone"), "phone"),
            (("address", "street"), "address"),
            (("city", "town"), "city"),
            (("state", "province"), "state"),
            (("zip", "postal"), "postal_code"),
            (("country",), "country"),
            (("linkedin",), "linkedin"),
            (("github",), "github"),
            (("website", "portfolio", "blog"), "website"),
        ]
        actions = []
        for f in fields or []:
            lbl = str(f.get("label", "")).lower()
            typ = str(f.get("type", "")).lower()
            ref = f.get("ref")
            if typ == "file":
                actions.append({"ref": ref, "op": "upload"})
                continue
            key = next((k for hs, k in hints if k in keyset and any(h in lbl for h in hs)), None)
            if key:
                op = "select" if typ in ("select", "radio") else "fill"
                actions.append({"ref": ref, "op": op, "source": key})
        return {"actions": actions}

    def draft_sponsorship_answer(self, profile: dict, status: str = "", future_need: str = "",
                                 role: str = "", company: str = "") -> str:
        auth = status.strip() if status.strip() else "authorized to work in the US on OPT"
        need = ("I will need sponsorship down the line, and I am upfront about that. "
                if future_need.strip() else "I can start right away with no sponsorship needed now. ")
        return (f"I am currently {auth}. {need}"
                f"What I want to focus on is the value I bring: real, relevant experience I can put "
                f"to work here from day one.")

    def assess_interview_readiness(self, profile: dict, role: str, company: str,
                                   answers: list[dict]) -> dict:
        # Deterministic stand-in: readiness from how substantive the answers were.
        subst = sum(1 for a in (answers or []) if len(str(a.get("a") or "").strip()) >= 60)
        n = len(answers or [])
        readiness = "ready" if n and subst == n else ("getting there" if subst else "not yet")
        return {
            "readiness": readiness,
            "summary": f"You answered {subst} of {n} questions substantively. Tighten the thin ones "
                       "with a specific situation and a measurable result.",
            "strengths": ["Showed up and practiced the real questions."],
            "gaps": ["Add concrete metrics to your results.", "Keep each answer under about 90 seconds."],
        }

    # -- P2 recorded screen: deterministic scoring stand-ins (real backend does the real work) --
    def score_interview_answer(self, profile: dict, question: str, transcript: str,
                               delivery: dict) -> dict:
        words = len((transcript or "").split())
        fillers = int((delivery or {}).get("fillers", 0))
        substantive = words >= 40
        base = 82 if substantive else 45
        score = max(0, min(100, base - min(10, fillers * 2)))
        band = 4 if substantive else 2
        return {
            "score": score,
            "relevance": band, "structure": band, "specificity": band,
            "honesty": "",
            "feedback": ("Solid, specific answer. Tighten the result with a number."
                         if substantive else "Too thin. Give a concrete situation and a measurable result."),
            "delivery_note": (delivery or {}).get("hint", ""),
        }

    def score_interview_screen(self, role: str, company: str, scored_answers: list[dict]) -> dict:
        scores = [int(a.get("score", 0)) for a in (scored_answers or [])]
        overall = round(sum(scores) / len(scores)) if scores else 0
        passed = overall >= 70
        return {
            "score": overall,
            "passed": passed,
            "threshold": 70,
            "why": (f"Averaged {overall} across {len(scores)} answers, at or above the practice bar."
                    if passed else
                    f"Averaged {overall} across {len(scores)} answers, below the practice bar of 70."),
            "improvements": ["Lead each answer with a clear result.",
                             "Use the STAR structure: situation, task, action, result.",
                             "Cut filler and keep each answer under about 90 seconds."],
            "assessment_disclaimer": "This is practice feedback to help you improve, not a real "
                                     "hiring decision or an employer's assessment.",
        }

    def generate_screen_questions(self, profile: dict, role: str, company: str = "",
                                  jd: str = "", kinds: list[str] | None = None) -> dict:
        # Deterministic Round-1 set: exactly one question per requested kind, in order.
        r = (role or "the role").strip()
        co = (company or "").strip() or "this company"
        kinds = list(kinds or ["motivation", "behavioural", "behavioural", "behavioural", "situational"])
        pool = {
            "motivation": [(f"Why do you want to work at {co}, and why this {r} role?", "Motivation")],
            "behavioural": [
                (f"Tell me about a time you delivered a result relevant to {r}.", "Ownership"),
                ("Tell me about a time you had to work with someone who disagreed with you.", "Communication"),
                ("Walk me through a hard problem you solved and how you approached it.", "Problem solving"),
                ("Tell me about a time you had to learn something quickly to get a job done.", "Adaptability"),
            ],
            "situational": [(f"What would you do if a key deadline for a {r} deliverable slipped?",
                             "Judgment")],
            "technical": [(f"How would you approach the core technical work this {r} role describes?",
                           "Technical depth")],
        }
        used: dict = {}
        out = []
        for k in kinds:
            items = pool.get(k) or pool["behavioural"]
            q, comp = items[used.get(k, 0) % len(items)]
            used[k] = used.get(k, 0) + 1
            out.append({"q": q, "kind": k, "competency": comp, "why": f"Probes {comp.lower()}."})
        return {"questions": out}

    def grade_project_defense(self, project: dict, qa: list[dict]) -> dict:
        # Length heuristic so the flow is exercisable in tests without a real model.
        subst = sum(1 for p in (qa or []) if len(str(p.get("a") or "").strip()) >= 40)
        passed = bool(qa) and subst >= max(1, len(qa) - 1)
        return {
            "pass": passed,
            "per_question": [{"verdict": "solid" if len(str(p.get("a") or "").strip()) >= 40 else "shaky",
                              "note": "(dev grade)"} for p in (qa or [])],
            "summary": "Looks defensible." if passed else "Some answers were too thin to show you understand it.",
            "gaps": [] if passed else ["Explain your key design decisions in more depth."],
        }

    # -- enrichment (draw out REAL material to fill the page) ------------- #
    def ask_enrich(self, topics: list[str], answered: list[str], role: str = "",
                   guidance: dict | None = None, person: dict | None = None) -> str:
        if not topics:
            return ""
        # If the template manifest offers a warm leading question for a topic, use it
        # (a natural opener beats the generic phrase); else fall back to the phrase.
        leads = []
        for t in (guidance or {}).get("topics", {}).values() if guidance else []:
            if t.get("leads"):
                leads.append(t["leads"][0])
        picks = leads[:2] if leads else list(topics)[:2]
        joined = picks[0] if len(picks) == 1 else f"{picks[0]}  Also, {picks[1]}"
        ack = "Perfect, added that. " if answered else ""
        return (f"{ack}{joined} "
                "(Real details only, or say “skip” / “that's everything” and I'll finalize.)")

    def absorb_enrichment(self, jd_text: str, essentials: dict, message: str) -> dict:
        e = essentials
        msg = str(message or "").strip()
        if not msg:
            return e

        # Skills: "my skills are X, Y", "skills: X, Y", "I know/use/work with X, Y".
        ms = re.search(r"(?:skills?\s*(?:are|:|include)|i (?:know|use|work with))\s+([^.]+)",
                       msg, re.I)
        if ms:
            got = [s.strip(" .") for s in re.split(r",|;|/|\band\b", ms.group(1)) if s.strip(" .")]
            if got:
                e.setdefault("skills_input", [])
                for s in got:
                    if s and s not in e["skills_input"]:
                        e["skills_input"].append(s)

        # Certifications: up to 3 Capitalized words around "Certified/Certification"
        # (so "I am AWS Certified Data Analytics" -> "AWS Certified Data Analytics",
        # not the whole sentence), plus the common acronym certs.
        certs = []
        for m in re.finditer(
                r"((?:[A-Z][\w+/&.-]*\s+){0,3})(Certified|Certification|Certificate)"
                r"((?:\s+[A-Z][\w+/&.-]*){0,3})", msg):
            pre = re.sub(r"^(?:And|The|A|An|My|I|Am|I'm)\s+", "", m.group(1).strip())
            certs.append(f"{pre} {m.group(2)}{m.group(3)}".strip())
        certs += re.findall(r"\b(CFA(?:\s+Level\s+[\w]+)?|PMP|CPA|CISSP|Six Sigma[\w ]*|"
                            r"Scrum Master[\w ]*)", msg)
        for c in certs:
            c = c.strip(" .,")
            if len(c) > 2:
                e.setdefault("certifications", [])
                if c not in e["certifications"]:
                    e["certifications"].append(c)

        # Interests: "interests: …", "I enjoy/like/love …".
        mi = re.search(r"(?:interests?\s*(?:are|:|include)|i (?:enjoy|like|love))\s+([^.]+)",
                       msg, re.I)
        if mi and not str(e.get("interests") or "").strip():
            e["interests"] = mi.group(1).strip(" .")

        # Extracurricular: volunteering / clubs / teams / self-driven builds (not a
        # paid job). Captured as its own section so it isn't mistaken for a role bullet.
        _extra_cue = re.compile(
            r"\b(volunteer(?:ed|ing)?|organi[sz]ed|founded|co-?founded|captain(?:ed)?|"
            r"president|treasurer|secretary|coach(?:ed)?|mentor(?:ed)?|hackathon|meetup|"
            r"fundrais\w*|charity|\bclub\b|society|drive)\b", re.I)
        extra_captured = False
        if _extra_cue.search(msg) and "extracurricular" not in set(e.get("declined", [])):
            for c in re.split(r"(?:\n|[.;])", msg):
                c = c.strip(" -•*,.")
                if len(c) > 12 and _extra_cue.search(c):
                    tm = re.search(r"\b(?:of|at|for|called|named)\s+"
                                   r"((?:the\s+)?[A-Z][\w&'.\-]+(?:\s+[A-Z][\w&'.\-]+){0,4})", c)
                    title = tm.group(1).strip() if tm else _trim_to_len(c, 40)
                    bullet = c[0].upper() + c[1:] + ("" if c.endswith(".") else ".")
                    e.setdefault("extracurricular", [])
                    if not any(str(x.get("title") or "").lower() == title.lower()
                               for x in e["extracurricular"]):
                        e["extracurricular"].append({"title": title, "date": "", "bullets": [bullet]})
                        extra_captured = True

        # Accomplishment bullets -> the role the message names (or the sole/most-recent).
        # Skip when the message was really about extracurriculars, so those don't
        # land as a job's bullets.
        exp = e.get("experience", []) or []
        target = None
        low = msg.lower()
        for j in exp:
            org = str(j.get("org") or "").strip()
            if org and org.lower() in low:
                target = j
                break
        if target is None and len(exp) == 1 and not extra_captured:
            target = exp[0]
        if target is not None and not extra_captured:
            # Break the answer into accomplishment-sized clauses: on sentence marks,
            # bullet markers, and an " and " that starts a fresh action ("…, and I built").
            split_re = re.compile(
                r"(?:\n|[.;]|\s*[-•*]\s+|,?\s+and\s+(?=i\b|we\b|" + "|".join(_ACTION_VERBS) + r"))",
                re.I)
            new_bullets = []
            for c in split_re.split(msg):
                c = re.sub(r"^\s*(?:at\s+[\w &.\-]+?\s+)?(?:i|we)\s+", "", (c or "").strip(),
                           flags=re.I)
                c = c.strip(" -•*,.")
                if len(c) > 12 and _ACTION_RE.search(c):
                    c = c[0].upper() + c[1:]
                    if not c.endswith("."):
                        c += "."
                    new_bullets.append(c)
            if new_bullets:
                existing = [str(b).strip() for b in (target.get("bullets") or []) if str(b).strip()]
                for b in new_bullets:
                    if b not in existing:
                        existing.append(b)
                target["bullets"] = existing[:4]

        # Real projects: "a project called X", "built X, a … project", "project: X".
        pm = re.search(r"(?:project(?:\s+called|\s+named|:)?\s+)([A-Z][\w &.\-]{2,40})", msg)
        if pm and "projects" not in set(e.get("declined", [])):
            name = pm.group(1).strip(" .,")
            projects = e.setdefault("projects", [])
            if name and not any(str(p.get("org") or "").lower() == name.lower() for p in projects):
                bullets = []
                for c in re.split(r"(?:\n|[.;])", msg):
                    c = c.strip(" -•*,.")
                    if len(c) > 12 and _ACTION_RE.search(c):
                        bullets.append(c[0].upper() + c[1:] + ("" if c.endswith(".") else "."))
                projects.append({"org": name, "title": "", "location": "Personal Project",
                                 "dates": "", "bullets": bullets[:2], "suggested": False})
        return e
