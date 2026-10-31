"""When a saved value stops being safe to send (docs/applicant-brain.md, sections 7.1 and 7.2).

Phase 4. Code decides, never the model, and only with the brain on:

* **A work authorization that ended.** An OPT, STEM OPT, EAD or time-limited permit whose saved
  end date has passed no longer says whether the person may work now. The questions that hang
  on it (authorized, sponsorship now, the status, the end date) are asked, not filled. Citizenship,
  permanent residence, future sponsorship and relocation do not change with it and still fill.
* **A salary older than six months** (the key's ``reconfirm_days``): a REQUIRED salary field asks
  "Still $90,000?" once (per amount and date); after that it fills as before, the way a salary
  conflict does. An optional field fills as before.
* **A start date in the past** is never filled: "Your saved start date (May 1, 2026) has passed".
* **A salary that does not fit the job.** On a job outside the US, a US dollar amount (or a bare
  amount, which the app saves in US dollars) is not filled. The person's salary for that
  country (a brain fact, ``@salary`` with subject = the country) is used when they gave one;
  else they are asked once and that answer is kept for that country only.

Every check is a pure function of the stored rows and ``today``; tests pass their own day.
"""
from __future__ import annotations

import datetime as _dt
import re

from applicant.store import ACTIVE, PERSON, Store

SALARY = "@salary"
START = "@start_date"
STALE_DAYS = 183

# The work-authorization kinds (autoapply/workauth.classify) a passed end date makes unsafe.
EXPIRED_KINDS = frozenset({"authorized", "authorized_no_sponsor", "authorized_unrestricted", "sponsor_now",
                           "sponsor_any", "status", "auth_end"})

COUNTRY_NAMES = {"US": "the US", "UK": "the UK", "CA": "Canada", "AU": "Australia", "EU": "the EU"}
COUNTRY_CURRENCY = {"US": "USD", "UK": "GBP", "CA": "CAD", "AU": "AUD", "EU": "EUR"}


def today() -> _dt.date:
    return _dt.date.today()


def fmt_date(d: _dt.date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


# ---------------------------------------------------------------------------- dates
_ISO = re.compile(r"^\s*(\d{4})-(\d{1,2})(?:-(\d{1,2}))?\s*$")
_US = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$")
_MONTHS = {m.lower(): i for i, m in enumerate(
    ("january february march april may june july august september october november december").split(), start=1)}
_WORDY = re.compile(r"^\s*(?:on\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\s*$")
_WORDY2 = re.compile(r"^\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\s*$")


def _month(word: str) -> int:
    w = str(word or "").lower()
    for name, n in _MONTHS.items():
        if len(w) >= 3 and name.startswith(w):
            return n
    return 0


def _end_of_month(y: int, m: int) -> _dt.date:
    nxt = _dt.date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return nxt - _dt.timedelta(days=1)


def parse_date(text) -> _dt.date | None:
    """A calendar date in the forms people save one ("2026-05-01", "05/01/2026", "May 1, 2026",
    "1 May 2026"; "2026-05" is the month's last day). None for anything else ("Immediately",
    "Two weeks after an offer"), which never expires."""
    s = str(text or "").strip()
    try:
        m = _ISO.match(s)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            return _dt.date(y, mo, int(m.group(3))) if m.group(3) else _end_of_month(y, mo)
        m = _US.match(s)
        if m:
            y = int(m.group(3))
            return _dt.date(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))
        m = _WORDY.match(s)
        if m and _month(m.group(1)):
            return _dt.date(int(m.group(3)), _month(m.group(1)), int(m.group(2)))
        m = _WORDY2.match(s)
        if m and _month(m.group(2)):
            return _dt.date(int(m.group(3)), _month(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return None


def start_date_passed(text, day: _dt.date | None = None) -> _dt.date | None:
    """The saved start date when it is a real date before ``day`` (today), else None."""
    d = parse_date(text)
    return d if d is not None and d < (day or today()) else None


# ---------------------------------------------------------------------------- work authorization
def workauth_ended(wa: dict | None, country: str = "US", day: _dt.date | None = None) -> _dt.date | None:
    """The end date of the job country's time-limited status (OPT, an EAD, a work permit) when it
    has passed, else None."""
    from autoapply import workauth as W
    ent = ((wa or {}).get("countries") or {}).get(str(country or "US").upper()) or {}
    if str(ent.get("status") or "") not in W.DATED:
        return None
    d = parse_date(ent.get("auth_end"))
    return d if d is not None and d < (day or today()) else None


def expired_kind(question: str, ended: _dt.date | None) -> bool:
    """True when this work-authorization question hangs on a status that ended."""
    if ended is None:
        return False
    from autoapply import workauth as W
    return W.classify(question) in EXPIRED_KINDS


# ---------------------------------------------------------------------------- salary
def _age_days(ts, day: _dt.date) -> int:
    try:
        return (day - _dt.date.fromtimestamp(float(ts))).days
    except (TypeError, ValueError, OSError):
        return 0


def salary_stale(row: dict | None, day: _dt.date | None = None, days: int = STALE_DAYS) -> bool:
    """True when the salary a form would get was saved more than six months ago."""
    if not row or not str(row.get("answer") or "").strip():
        return False
    return _age_days(row.get("updated_at"), day or today()) > days


def stale_sig(row: dict) -> str:
    from autoapply.basics import parse_salary
    return f"{parse_salary(row.get('answer')) or 0}:{int(float(row.get('updated_at') or 0))}"


def stale_asked(st: Store | None, row: dict) -> bool:
    return st is not None and st.meta("asked:stale:" + SALARY) == stale_sig(row)


def mark_stale_asked(st: Store, row: dict) -> None:
    st.set_meta("asked:stale:" + SALARY, stale_sig(row))
    st.log("stale_asked", key=SALARY)


_CUR = (("GBP", re.compile(r"£|\bgbp\b|\bpounds?\b|\bsterling\b", re.I)),
        ("EUR", re.compile(r"€|\beur\b|\beuros?\b", re.I)),
        ("CAD", re.compile(r"\bc\$|\bcad\b|ca\$|canadian", re.I)),
        ("AUD", re.compile(r"\ba\$|\baud\b|au\$|australian", re.I)),
        ("USD", re.compile(r"\$|\busd\b|\bdollars?\b", re.I)))


def currency(text) -> str:
    """The currency a saved salary is in. A bare amount is US dollars (the app saves it so)."""
    s = str(text or "")
    for code, rx in _CUR:
        if rx.search(s):
            return code
    return "USD"


def fits_job(text, country: str = "US") -> bool:
    """True when a saved salary is in the job country's currency."""
    want = COUNTRY_CURRENCY.get(str(country or "US").upper())
    return want is None or currency(text) == want


def country_salary(st: Store | None, country: str) -> str:
    """The person's salary for a job in this country (subject = the country), or ""."""
    if st is None or not country or country.upper() == "US":
        return ""
    rows = st.q("SELECT value FROM fact WHERE person=? AND key=? AND subject=? AND store='' AND status=?"
                " ORDER BY id DESC LIMIT 1", (PERSON, SALARY, country.upper(), ACTIVE))
    return str(rows[0]["value"]) if rows else ""


def save_country_salary(st: Store, country: str, value: str, *, question: str = "", source_ref: str = "") -> bool:
    """Keep the person's salary for jobs in one country (never the salary every US form gets)."""
    from applicant import facts as F
    v = " ".join(str(value or "").split())
    cc = str(country or "").upper()
    if not v or not cc or cc == "US":
        return False
    F.put(st, SALARY, v, source="you", subject=cc, question=question, source_ref=source_ref)
    return True


# ---------------------------------------------------------------------------- the words
def say_stale(amount_label: str) -> str:
    return f"Still {amount_label}? Tap it to keep it, or type a new amount."


def say_start(d: _dt.date) -> str:
    return f"Your saved start date ({fmt_date(d)}) has passed. When can you start?"


def say_country_salary(country: str) -> str:
    return f"This job is in {COUNTRY_NAMES.get(country.upper(), country)}. What salary do you want for it?"


def say_workauth(d: _dt.date, question: str) -> str:
    return f"Your saved work authorization ended on {fmt_date(d)}. {question}"
