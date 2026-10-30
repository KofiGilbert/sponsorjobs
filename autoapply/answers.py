"""The answer library: fix an answer once, reused on every later form.

A local SQLite store (in the app's data dir) of question -> answer. Questions are stored under a
NORMALIZED key, so "What are your salary expectations?*", "Desired compensation" and "Expected
salary (USD)" all find the same saved answer. Each answer says where it came from:

* ``you``: the person wrote or edited it (on Your answers, the package page, or a form correction);
* ``drafted``: drafted from their profile for an earlier application, kept so they can confirm it.

A "you" answer always wins over a drafted one, and a drafted answer never overwrites one the
person wrote. Questions that name a specific company or ask why this role/company are never
saved: their answer belongs to one application.

Lookup is exact on the key (after normalization and synonyms), never fuzzy, so a saved answer
is only reused for the same question.

Before any matching, a label loses the form furniture around its question (clean_label): a
validation error the page wrapped round it ("Error: The field What is your annual salary
requirement? is required and must have a value.") and the required markers ("*", "(required)").
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path

SOURCES = ("you", "drafted")
MAX_Q = 300
MAX_A = 4000

# Bumped when clean_label or _clean change what a question normalizes to, so the library re-keys
# its older rows once (the _CANON patterns are part of the version too).
_LABEL_RULES = "labels-1"

# --------------------------------------------------------------------------- label cleaning
# Form furniture around a question. A validation error the page wrapped round its label (Phenom
# at Lam Research, 2026-10-30: "Error: The field What is your annual salary requirement? is
# required and must have a value."; Workday: "Errors Found Error-<question> The field <question>
# is required"), and the required markers ("*", "(required)", "Required"). Mirrored in
# shell-electron/agent.js cleanQuestion.
# Workday's link reads "Error-What is..."; "Error-free code?" is a question (a capital after the
# hyphen, case-sensitive inside the case-insensitive pattern).
_ERR_HEAD = re.compile(r"^[\s*\u2731]*(?:(?:\d+\s+)?errors?\s+found\b[\s:.!\-]*|errors?\s*[:\u2013\u2014]+\s*"
                       r"|errors?\s+-\s*|errors?-(?=(?-i:[A-Z0-9\"'\u201c(])))+", re.I)
# "The field <question> ...": at the start, or after the end of a sentence (a banner that names
# the question twice).
_THE_FIELD = re.compile(r"(?:^|(?<=[?.!:]\s))the field\s+", re.I)
# What an error says after the question. The bare "is" and "is and must have a value" are what
# is left when the fill engine has already cut the word "required" out of a label.
_ERR_TAIL = re.compile(
    r"\s+(?:is\s+(?:(?:required|mandatory)\s+)?and\s+must\s+have\s+a\s+value"
    r"|is\s+(?:a\s+)?(?:required|mandatory)(?:\s+(?:field|question))?(?:\s+and\s+must\s+have\s+a\s+value)?"
    r"|must\s+have\s+a\s+value|cannot\s+be\s+(?:left\s+)?(?:blank|empty)|is\s+(?:blank|empty|missing|invalid)"
    r"|is)\s*[.!]*\s*$", re.I)
_REQ_SENTENCE = re.compile(r"\s*\b(?:this\s+)?(?:field|question|answer)\s+is\s+(?:required|mandatory)\W*$", re.I)
_REQ_PAREN = re.compile(r"\(\s*(?:required|mandatory)\s*\)", re.I)
# A trailing "Required" only after a marker ("?", "*", ":", " - "), never a word of the question
# ("Visa sponsorship required" keeps its last word).
_REQ_TRAIL = re.compile(r"(?:(?<=[?*\u2731)\]])\s*|\s*[:*\u2731]\s*|\s+-\s+)(?:required|mandatory)[\s*\u2731.]*$", re.I)
_REQ_LEAD = re.compile(r"^(?:required|mandatory)\s*[:*\u2731\-]+\s*", re.I)
_ONLY_FURNITURE = re.compile(r"^(?:is\s+)?(?:a\s+)?(?:required|mandatory)(?:\s+(?:field|question))?\W*$", re.I)


def clean_label(text: str) -> str:
    """The question a form label asks, without the furniture around it: "Error: The field What
    is your annual salary requirement? is required and must have a value." and "What is your
    annual salary requirement? (required)" both give "What is your annual salary requirement?".
    The words of the question itself are kept as written. "" when nothing but furniture is left."""
    s = " ".join(str(text or "").split())
    if not s:
        return ""
    erred = False
    m = _ERR_HEAD.match(s)
    if m:
        s, erred = s[m.end():], True
    fields = list(_THE_FIELD.finditer(s))
    if fields:
        tail = s[fields[-1].end():]
        cut = _ERR_TAIL.sub("", tail, count=1)
        if cut != tail or (erred and fields[-1].start() == 0):
            s, erred = cut, True
    if erred:
        s = _ERR_TAIL.sub("", s, count=1)
    else:
        cut = _ERR_TAIL.sub("", s, count=1)
        if cut != s and cut.rstrip().endswith("?"):    # "<question>? is required"
            s = cut
    s = _REQ_SENTENCE.sub("", s)
    s = _REQ_PAREN.sub(" ", s)
    s = _REQ_TRAIL.sub("", s)
    s = _REQ_LEAD.sub("", s)
    s = re.sub(r"^[\s*\u2731]+|[\s*\u2731]+$", "", s)
    s = " ".join(s.split())
    return "" if _ONLY_FURNITURE.match(s) else s

# Words that change nothing about what a question asks.
_FILLER = re.compile(
    r"\b(please|kindly|optional|required|mandatory|if applicable|if any|if so|briefly|"
    r"select one|choose one|select|choose|enter|provide|specify|indicate|tell us|let us know|"
    r"describe|state|list|your|you|do|does|are|is|what|which|how|the|a|an|of|to|for|in|on|"
    r"with|please note|e g|eg)\b")

# How a form leads into a short question, and what may trail it ("Please select your gender",
# "What is your race?", "Gender (optional)"), for the questions that must match as a WHOLE label.
_ASK = r"^(please )?((select|choose|indicate|specify|state|enter|provide|identify) )?(what is |what's )?(your )?"
_TAIL = r"( (optional|required|voluntary|if any))*$"

# Canonical keys for the questions that recur on nearly every form, worded many ways. A
# question that matches one of these (as its whole intent) gets that key.
_CANON: list[tuple[str, re.Pattern]] = [
    ("salary", re.compile(
        r"^(what (is|are) )?(your )?((desired|expected|target|minimum|annual|yearly|base) )*"
        r"(salary|compensation|pay|base salary|base pay|total compensation|rate)"
        r"( (expectations?|requirements?|range|requested|expected|desired))?"
        r"( for this (role|position))?( \(?(usd|annual|per year|yearly|in usd)\)?)*$"
        r"|^(desired|expected|target|annual|yearly) (annual |base )?(salary|compensation|pay)"
        r"|^salary (expectations?|requirements?|range)"
        r"|^compensation (expectations?|requirements?)"
        r"|^what (salary|compensation|pay) (are you|do you|would you)")),
    ("start_date", re.compile(
        r"^(what is )?(your )?(earliest|desired|available|possible|expected|preferred)? ?"
        r"(start date|date (you|that you) (can|could) start|availability to start)"
        r"|^when (can|could|would) you (start|begin|join)"
        r"|^(how soon|when) (are you available|can you be available)")),
    ("notice_period", re.compile(r"notice period|how much notice")),
    ("heard_about", re.compile(
        r"how did you (hear|learn|find out) (about|of)|where did you (hear|find|learn|see)"
        r"|how did you find (this|the|our)|(referral|application|candidate) source|^source$"
        r"|^how did you hear")),
    ("years_experience", re.compile(
        r"^(how many )?(total )?years (of )?(professional |relevant |work |industry )?experience"
        r"( do you have)?( in total| overall)?$")),
    ("over_18", re.compile(r"(at least|over|older than) (the age of )?18|18 years (of age|or older|old)"
                           r"|age of majority")),
    # Voluntary self-identification (EEO): asked once, answered the same way everywhere. Each is
    # the WHOLE label (_ASK ... _TAIL): "Race: Asian" is one box of a checkbox group, "Gender
    # pronouns" is another question, and an accommodation question is not the disability one.
    # Race and Hispanic/Latino ethnicity are separate questions on many US forms, and so are sex
    # and gender identity: separate keys, so one answer never overwrites the other.
    ("pronouns", re.compile(_ASK + r"(preferred |gender )?pronouns?" + _TAIL
                            + r"|what are your (gender |preferred )?pronouns")),
    ("gender", re.compile(_ASK + r"gender( identity)?" + _TAIL)),
    ("sex", re.compile(_ASK + r"(biological |legal )?sex( assigned at birth)?" + _TAIL)),
    ("race", re.compile(_ASK + r"(race|racial (identity|background|group|origin)|race and ethnicity"
                        r"|race ethnicity|race ethnic (identity|background|group|origin)|ethnicity and race"
                        r"|ethnicity race)" + _TAIL)),
    # The Hispanic or Latino question is the US forms' "ethnicity": "Are you Hispanic/Latino?",
    # "Hispanic or Latino", "Do you identify as Hispanic or Latino?". Never "Race: Hispanic or
    # Latino" (one box of a race group) or a combined race list's own label ("Race").
    ("ethnicity", re.compile(_ASK + r"(ethnicity|ethnic (identity|background|group|origin)"
                             r"|(hispanic|latin[oax]|latine)(( or | )(latin[oax]|latine|hispanic))?( origin| ethnicity)?)" + _TAIL
                             + r"|^(are you|do you identify as|do you consider yourself( to be)?)( of)? (hispanic|latin[oax]|latine)"
                             r"(( or | )(latin[oax]|latine|hispanic))?( origin| ethnicity| descent)?" + _TAIL)),
    # VEVRAA "protected veteran" status only: "Are you a veteran?" (any service) is another
    # question, and a "Yes" there is not a protected-veteran self-identification.
    ("veteran", re.compile(_ASK + r"(protected )?veteran status" + _TAIL + r"|^(protected )?veteran" + _TAIL
                           + r"|^are you an? protected veteran" + _TAIL)),
    # The CC-305 question, never "Do you have a disability that requires an accommodation?".
    ("disability", re.compile(_ASK + r"disability( status)?" + _TAIL + r"|^do you have an? disability" + _TAIL
                              + r"|^voluntary self identification of disability( form)?( cc 305)?$")),
    # A second country of citizenship ("Second country of citizenship", "Other nationality"):
    # before the first, whose pattern would take it. "Do you hold dual citizenship?" is a yes/no.
    ("citizenship2", re.compile(r"^(please (provide|enter|list|select) )?(your )?(second|secondary|other|additional|another|dual) "
                                r"(country (or region |region )?of )?(citizenship|nationality)( country)?( if any| if applicable)?$"
                                r"|^country (or region |region )?of (second|secondary|other|additional|dual) "
                                r"(citizenship|nationality)( if any| if applicable)?$")),
    # The COUNTRY of citizenship ("Country of citizenship", and Lam's export-control wording
    # "please provide your most recent country/region of citizenship or legal permanent
    # residence..."). A citizenship STATUS or a yes/no citizenship question keeps its own key.
    ("citizenship", re.compile(r"country (or region |region )?of (citizenship|nationality)"
                               r"|^(what is )?(your )?nationality" + _TAIL + r"|what country are you a citizen of")),
    ("preferred_name", re.compile(r"^(preferred|chosen) (first )?name$|what (name|should we call)")),
    ("linkedin", re.compile(r"^linkedin( profile)?( url| link)?$")),
    ("portfolio", re.compile(r"^(portfolio|personal website|website)( url| link)?$")),
    # Workday words it "were you previously an employee, contractor or intern of <company>, <its
    # affiliates...>" (Country Financial, 2026-10-28); the company names are not the question.
    ("previously_worked", re.compile(r"(have you )?(ever )?(previously )?(worked|been employed) "
                                     r"(for|at|by) (us|this company|our company) before"
                                     r"|previously (worked|been employed) (for|at|by) (us|this company)"
                                     r"|previously (an? )?(employee|contractor|intern|employed|worked)\b"
                                     r"|(former|past|prior) (employee|employment|contractor|intern)\b"
                                     r"|(ever )?(worked|interned|been employed) (for|at|by|with) (us|this|our|the company)")),
]

_EEO_KEYS = frozenset({"@gender", "@sex", "@race", "@ethnicity", "@veteran", "@disability"})
# Questions whose answer is one short choice, never a paragraph.
_SHORT_KEYS = _EEO_KEYS | {"@citizenship", "@citizenship2"}


def unusable(key: str, answer: str) -> bool:
    """True for a saved answer that must not be used for its question. Such a row STAYS in the
    library (the person sees it on Your answers and can delete it); the agent asks again, and
    the person's new answer replaces it through save(). Two shapes, both raw chat replies the
    agent saved before replies were read (Lam Research, 2026-10-30): a question back ("what are
    the options?") on one of the recurring questions, where a question is never the answer, and
    a paragraph where a self-identification or citizenship answer is one short choice. A question
    the person wrote as the answer to an open question ("Do you have any questions for us?")
    stays usable."""
    a = str(answer or "").strip()
    if not a:
        return True
    k = str(key or "")
    if k.startswith("@") and a.endswith("?"):
        return True
    return k in _SHORT_KEYS and "\n" in a

# Never saved: their answer belongs to ONE application.
_PER_JOB = re.compile(r"why (do you want|are you interested|this|our|us|join|apply|would you like)"
                      r"|why (are|is) .{0,40}(company|role|position|team)"
                      r"|interest(ed)? in (this|our|the) (role|position|company|team)"
                      r"|cover letter|what excites you about|tell us why", re.I)


def _clean(text: str) -> str:
    s = str(text or "").lower().replace("’", "'").replace("&", " and ")
    s = re.sub(r"\(.*?\)", lambda m: " " + m.group(0)[1:-1] + " ", s)
    s = re.sub(r"[*:?!.,;\"“”]+", " ", s)
    s = re.sub(r"[^a-z0-9'+#/ -]+", " ", s)
    s = re.sub(r"\s*[-/]\s*", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize(question: str) -> str:
    """The key a question is stored and looked up under: a canonical name for the recurring
    questions ("salary", "start_date", "heard_about", ...), else the question lowercased with
    punctuation, filler words and word order removed. "" for an empty question, and for a label
    that is only form furniture ("Errors Found", "This field is required")."""
    c = _clean(clean_label(question))
    if not c:
        return ""
    for key, rx in _CANON:
        if rx.search(c):
            return "@" + key
    words = [w for w in _FILLER.sub(" ", c).split() if w]
    if not words:
        words = c.split()
    return " ".join(sorted(set(words)))


def is_per_job(question: str, company: str = "") -> bool:
    """A question whose answer belongs to one application (why this company, this role)."""
    q = str(question or "")
    if _PER_JOB.search(q):
        return True
    co = str(company or "").strip().lower()
    return bool(co) and len(co) > 2 and co in q.lower()


class AnswerLibrary:
    """question -> answer, keyed by ``normalize(question)``. One row per key."""

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS answers ("
            " key TEXT PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL,"
            " source TEXT NOT NULL DEFAULT 'you', created_at REAL, updated_at REAL,"
            " last_used REAL, uses INTEGER NOT NULL DEFAULT 0)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self.conn.commit()
        self._stuck_keys: dict | None = None
        self._rekey()

    def _rekey(self):
        """Move answers saved under an older key to the key their question has now. When the
        canonical wordings widen ("annual salary requirement" became the salary question), or
        the label cleaning learns a new kind of furniture ("Error: The field ... is required"),
        an answer saved before must still be found. Runs once per change of the key rules; a row
        whose new key is already taken stays where it is (lookup still weighs it, see best).
        Nothing is ever deleted here: an answer that cannot be used (see unusable) is skipped on
        read instead, and when two wordings of one question meet, a usable answer claims the key
        first (the person's own before a drafted one, the newest first)."""
        version = hashlib.sha1((_LABEL_RULES + "|" + "|".join(k + rx.pattern for k, rx in _CANON))
                               .encode()).hexdigest()[:12]
        try:
            row = self.conn.execute("SELECT v FROM meta WHERE k='keys_version'").fetchone()
            if row and row[0] == version:
                return

            def rank(r):
                return (unusable(normalize(r["question"]), r["answer"]), r["source"] != "you",
                        -float(r["updated_at"] or 0))
            for _ in range(3):                     # a key freed by one move can take another row
                moved = False
                rows = self.conn.execute("SELECT key, question, answer, source, updated_at FROM answers").fetchall()
                for r in sorted(rows, key=rank):
                    new = normalize(r["question"])
                    if new and new != r["key"] and not self.conn.execute(
                            "SELECT 1 FROM answers WHERE key=?", (new,)).fetchone():
                        self.conn.execute("UPDATE answers SET key=? WHERE key=?", (new, r["key"]))
                        moved = True
                if not moved:
                    break
            self.conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('keys_version', ?)", (version,))
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def _row(r) -> dict:
        # A row saved before labels were cleaned still reads as its question, never as the
        # page's error round it (the stored text itself is left as it was).
        q = str(r["question"] or "")
        return {"key": r["key"], "question": clean_label(q) or q, "answer": r["answer"],
                "source": r["source"], "updated_at": r["updated_at"], "last_used": r["last_used"],
                "uses": r["uses"]}

    def save(self, question: str, answer: str, source: str = "you", *, company: str = "") -> dict | None:
        """Save (or replace) the answer to a question. A drafted answer never replaces one the
        person wrote; per-application questions, empty answers and answers their question can
        never use (a question back on a recurring question, a paragraph for a one-choice one)
        are not saved. The question is kept without its form furniture (clean_label). Returns
        the stored row, or None when nothing was saved."""
        raw = str(question or "").strip()
        q = (clean_label(raw) or raw)[:MAX_Q]
        a = str(answer or "").strip()[:MAX_A]
        source = source if source in SOURCES else "you"
        key = normalize(q)
        if not key or not a or is_per_job(q, company) or unusable(key, a):
            return None
        now = time.time()
        cur = self.conn.execute("SELECT * FROM answers WHERE key=?", (key,)).fetchone()
        if cur is not None:
            if source == "drafted" and cur["source"] == "you":
                return None
            self.conn.execute("UPDATE answers SET question=?, answer=?, source=?, updated_at=? WHERE key=?",
                              (q, a, source, now, key))
        else:
            self.conn.execute("INSERT INTO answers (key, question, answer, source, created_at, updated_at, uses)"
                              " VALUES (?,?,?,?,?,?,0)", (key, q, a, source, now, now))
        self.conn.commit()
        self._stuck_keys = None
        return self.get_key(key)

    def get_key(self, key: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM answers WHERE key=?", (key,)).fetchone()
        return self._row(r) if r else None

    def _stuck(self) -> dict:
        """{canonical key: [row keys]} for rows that sit under an older key although their
        question has a canonical key now (the re-key leaves a row in place when its new key is
        already taken). Read once per library, again after a write."""
        if self._stuck_keys is None:
            out: dict = {}
            for r in self.conn.execute("SELECT key, question FROM answers WHERE key NOT LIKE '@%'").fetchall():
                k = normalize(r["question"])
                if k.startswith("@") and k != r["key"]:
                    out.setdefault(k, []).append(r["key"])
            self._stuck_keys = out
        return self._stuck_keys

    def candidates(self, key: str) -> list[dict]:
        """Every saved row for a key: the row under it, and for a canonical key the rows left
        under an older key whose question is the same canonical question."""
        rows = []
        r = self.get_key(key) if key else None
        if r:
            rows.append(r)
        if key.startswith("@"):
            for k in self._stuck().get(key, []):
                x = self.get_key(k)
                if x:
                    rows.append(x)
        return rows

    def best(self, key: str) -> dict | None:
        """The answer a key has: the usable one first, the person's own before a drafted one,
        then the most recently updated. Two saved salaries that disagree (one under the salary
        key, a newer one the agent saved under an "Error: The field ..." label before labels were
        cleaned) give the newer one, here and on Your answers alike."""
        rows = [r for r in self.candidates(key) if not unusable(key, r["answer"])]
        if not rows:
            return None
        rows.sort(key=lambda r: (r["source"] != "you", -float(r["updated_at"] or 0)))
        return rows[0]

    def lookup(self, question: str) -> dict | None:
        """The saved answer to this question (same normalized key), or None. Every reader that
        USES an answer (the planner, the agent's known answers, its asks) comes through here, so
        an answer that cannot be used is never handed out, while its row stays saved until the
        person replaces or deletes it."""
        key = normalize(question)
        return self.best(key) if key else None

    def mark_used(self, key: str):
        self.conn.execute("UPDATE answers SET last_used=?, uses=uses+1 WHERE key=?", (time.time(), key))
        self.conn.commit()

    def update(self, key: str, *, question: str | None = None, answer: str | None = None) -> dict | None:
        """The person edited a saved answer (it becomes theirs). A changed question re-keys it."""
        cur = self.get_key(key)
        if cur is None:
            return None
        q = (question if question is not None else cur["question"]).strip()[:MAX_Q]
        a = (answer if answer is not None else cur["answer"]).strip()[:MAX_A]
        if not a or not normalize(q):
            return None
        new_key = normalize(q)
        row = self.save(q, a, "you")
        if row is not None and new_key != key:
            self.delete(key)               # moved to its new key: the old entry goes only now
        return row

    def delete(self, key: str) -> bool:
        n = self.conn.execute("DELETE FROM answers WHERE key=?", (key,)).rowcount
        self.conn.commit()
        self._stuck_keys = None
        return n > 0

    def list(self, query: str = "") -> list[dict]:
        """Every saved answer, the person's own first, newest first; ``query`` filters on the
        question and answer text."""
        rows = self.conn.execute(
            "SELECT * FROM answers ORDER BY (source='you') DESC, updated_at DESC").fetchall()
        out = [self._row(r) for r in rows]
        q = str(query or "").strip().lower()
        if q:
            out = [r for r in out if q in r["question"].lower() or q in r["answer"].lower()]
        return out

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0])


# The older flat answers in the prefs store, seeded into the library once as the person's own.
_SEED = (("years_experience", "How many years of experience do you have?"),
         ("desired_salary", "What are your salary expectations?"),
         ("earliest_start", "When can you start?"),
         ("hear_about_us", "How did you hear about us?"),
         ("over_18", "Are you at least 18 years of age?"))


def seed_from_prefs(lib: AnswerLibrary, prefs: dict) -> int:
    """Copy the person's saved flat answers (salary, start date, how they heard, years, over 18)
    into the library, without touching an answer already there. Returns how many were added."""
    n = 0
    for k, q in _SEED:
        v = str((prefs or {}).get(k) or "").strip()
        if v and lib.lookup(q) is None and lib.save(q, v, "you"):
            n += 1
    return n


def seed_once(lib: AnswerLibrary, prefs: dict) -> int:
    """Seed from the saved prefs the first time the library is opened, never again (so an
    answer the person deleted stays deleted)."""
    if lib.conn.execute("SELECT 1 FROM meta WHERE k='seeded'").fetchone():
        return 0
    n = seed_from_prefs(lib, prefs)
    lib.conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('seeded', '1')")
    lib.conn.commit()
    return n
