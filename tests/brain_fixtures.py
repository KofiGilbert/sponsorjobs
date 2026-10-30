"""A legacy data folder for the applicant brain tests (tests/test_brain_*.py): the five stores
as the app holds them before Phase 1, with the shapes that matter for parity. The Lam Research
run's raw replies, two salaries that disagree (one left under the key an "Error: The field ..."
label had before labels were cleaned), wording keys, a drafted answer, self-identification
answers, citizenship, a structured work authorization with the flat prefs answers, a resume
profile with two overlapping roles, chat facts and two diary files."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from autoapply.answers import AnswerLibrary
from intake.memory import ConversationMemory

LAM_SALARY = "Error: The field What is your annual salary requirement? is required and must have a value."
LAM_REPLY = "Yes, I will need H1B\n\nMale\n\nDon't you have this information on my profile already ?"
OLD_KEY = "and annual error field have must requirement salary value"   # the key that label had before
LAM_EXPORT = ("For the sole purpose of determining export licensing requirements, please provide your most recent "
              "country/region of citizenship or legal permanent residence (if you are a citizen of more than one, "
              "list each)")
CUBA_Q = "Are you a citizen of Cuba, Iran, North Korea, Syria, or the Crimea region of Ukraine?"

# (key, question, answer, source, seconds ago)
ROWS = [
    ("@salary", "What are your salary expectations?", "$120,000", "you", 300),
    (OLD_KEY, LAM_SALARY, "130000", "you", 100),
    ("@gender", "Gender", LAM_REPLY, "you", 200),
    ("@ethnicity", "Ethnicity", "What are the options under ethnicity ?", "you", 190),
    ("@race", "Race", "Asian", "you", 180),
    ("@heard_about", "How did you hear about us?", "LinkedIn", "you", 170),
    ("@years_experience", "How many years of experience do you have?", "5", "you", 160),
    ("@citizenship", "Country of citizenship", "Ghana", "you", 150),
    ("@start_date", "When can you start?", "Two weeks after an offer", "drafted", 140),
    ("experience have sql", "Do you have experience with SQL?", "Yes, 3 years in production.", "drafted", 130),
    ("certifications cloud hold", "Which cloud certifications do you hold?", "AWS Solutions Architect Associate",
     "you", 120),
]

PREFS = {
    "work_authorized": "Yes", "work_auth_us": "Yes", "needs_sponsorship": "Future", "willing_to_relocate": "Yes",
    "over_18": "Yes", "years_experience": "5", "desired_salary": "95000", "earliest_start": "2 weeks",
    "hear_about_us": "LinkedIn", "eeo_decline": False,
    "work_auth": {"countries": {"US": {"status": "f1_opt", "authorized": "Yes", "sponsor_now": "No",
                                       "sponsor_future": "Yes", "auth_end": "2027-06"}}, "relocate": "Yes"},
}

PROFILE = {
    "identity": {"name": "Ama Test", "email": "ama.test@example.com", "phone": "312-555-0100",
                 "location": "Chicago, IL", "linkedin": "linkedin.com/in/amatest"},
    "experience": [
        {"org": "Stanbic Bank", "roles": [{"title": "Analyst", "dates": "2020 to 2021", "bullets": ["Built reports"]}]},
        {"org": "Viva", "title": "Data Analyst", "dates": "2020", "bullets": ["Cleaned data"]},
    ],
    "education": [{"school": "University of Ghana", "degree": "BSc Economics", "date": "2019"}],
    "projects": [{"title": "Loan default model", "dates": "2021"}],
    "skills": {"Languages": "SQL, Python", "Tools": "Tableau"},
}

# A value seeded into every store, so a test can search any output for a leak.
SECRET_VALUES = ("130000", "$120,000", "Ghana", "Asian", "f1_opt", "95000", "ama.test@example.com")


def write_legacy(data: Path, *, prefs: dict | None = None, rows=None, profile=True) -> Path:
    """Build the legacy stores in ``data`` and return it."""
    data.mkdir(parents=True, exist_ok=True)
    AnswerLibrary(data / "answers.db").close()          # the tables, at the current key rules
    con = sqlite3.connect(data / "answers.db")
    now = time.time()
    for key, q, a, src, ago in (ROWS if rows is None else rows):
        con.execute("INSERT INTO answers (key, question, answer, source, created_at, updated_at, uses)"
                    " VALUES (?,?,?,?,?,?,0)", (key, q, a, src, now - ago - 1000, now - ago))
    con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('seeded', '1')")
    con.commit()
    con.close()
    (data / "app_prefs.json").write_text(json.dumps(PREFS if prefs is None else prefs, indent=2), encoding="utf-8")
    if profile:
        mem = ConversationMemory(str(data / "resume_agent.db"))
        mem.save("default", PROFILE, {"identity": PROFILE["identity"]}, [])
        mem.upsert_fact("default", "preference", "tone", "Plain and direct", provenance="chat", confidence=0.8)
        mem.upsert_fact("default", "constraint", "viva", "Do not mention Viva for bank jobs", provenance="chat")
        mem.upsert_fact("default", "fact", "salary", "About 100k", provenance="chat", confidence=0.7)
        mem.upsert_fact("default", "fact", "gender", "female", provenance="chat")       # chat never sets EEO
        old = mem.upsert_fact("default", "fact", "pet", "A cat named Mo", provenance="chat")
        mem.retract_fact("default", old["id"])
        mem.close()
        tdir = data / "palace" / "transcripts"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "default.jsonl").write_text("\n".join(json.dumps({"role": "user", "content": f"turn {i}"})
                                                      for i in range(4)) + "\n", encoding="utf-8")
        (tdir / "autoapply_123.jsonl").write_text(json.dumps({"role": "user", "content": "x"}) + "\n",
                                                  encoding="utf-8")
    return data


def file_bytes(data: Path) -> dict:
    """{relative path: bytes} for every file in the folder (what "byte-identical" compares)."""
    return {str(p.relative_to(data)): p.read_bytes() for p in sorted(data.rglob("*")) if p.is_file()}
