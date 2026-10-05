"""JD-term extraction quality and seniority-aware template suggestion
(case-study obs #18 and #28, replayed against the actual Amazon posting)."""

from __future__ import annotations

import ui.app as app
from intake.memory import ConversationMemory
from tailoring.keywords import extract_jd_terms

AMAZON_JD = """Area Manager 2026 at Amazon
Locations: AL, AR, GA, LA, MS (Recent and Upcoming Graduates)
Job ID: 3048863 | Amazon.com Services LLC

This is a full-time, entry level position located within one of Amazon's fulfillment
centers. Please note we are not able to provide sponsorship now or in the future.
Key job responsibilities
- Support, mentor and motivate 50-100 direct report Amazon Associates
- Stand/walk during shifts lasting up to 12 hours; support your team on the floor
- Willing and able to regularly work shift patterns that include nights and weekends
- Oversee truck deliveries, handle and sort packages using pallet jacks, operate PIT
  equipment (at applicable facilities) and step in as needed. While shifts vary site
  to site, most follow a fixed 4-day work week.
- Lift up to 49 pounds and climb stairs. May include holidays.
Basic Qualifications
- A bachelor's or master's degree completed between May 2024 and August 2026.
Preferred: degree in Supply Chain, Business/Management, Engineering. Python and SQL a plus.
The base salary range: 65,200.00 - 75,000.00 USD annually with RSUs. Relocation
benefits are offered. Site placement is matched after your interview."""


def test_boilerplate_never_reads_as_key_terms():
    terms = {t.lower() for t in extract_jd_terms(AMAZON_JD)}
    # The exact soup from the case-study coverage report (obs #18):
    for junk in ("please", "site", "locations", "while", "may", "stand", "willing",
                 "lift", "climb", "llc", "id", "usd", "rsus", "relocation",
                 "august", "al", "ar", "ga", "la", "ms", "key", "basic",
                 "preferred", "support", "oversee"):
        assert junk not in terms, f"boilerplate leaked into JD terms: {junk!r}"


def test_real_terms_survive_the_filters():
    terms = {t.lower() for t in extract_jd_terms(AMAZON_JD)}
    for real in ("amazon", "pit", "python", "sql"):
        assert real in terms, f"real term was over-filtered: {real!r}"


def _seed_profile(monkeypatch, experience):
    mem = ConversationMemory(":memory:")
    mem.save("default", {"experience": experience},
             {"identity": {}, "education": [], "experience": experience}, [])
    monkeypatch.setattr(app, "ConversationMemory", lambda _path: mem)


def test_suggester_weighs_candidate_seniority(monkeypatch):
    # An 8-year operations leader: the graduate-flavored JD must NOT pull the
    # education-first layout anymore (obs #28).
    _seed_profile(monkeypatch, [
        {"org": "Ampsel", "title": "Supply Chain Operations Manager",
         "dates": "June 2020 - Sept 2024", "bullets": []},
        {"org": "Stanbic", "title": "Regional Manager",
         "dates": "Feb 2018 - May 2020", "bullets": []},
        {"org": "Viva", "title": "Operations Lead",
         "dates": "Sept 2016 - Jan 2018", "bullets": []},
    ])
    out = app._suggest_template(AMAZON_JD)
    assert out["name"] in ("experienced", "summary")
    assert "years of experience" in out["reason"]


def test_suggester_unchanged_for_a_true_new_grad(monkeypatch):
    _seed_profile(monkeypatch, [])
    out = app._suggest_template(AMAZON_JD)
    # No seniority boost: the graduate posting keeps its graduate-flavored pick.
    assert out["name"] not in ("experienced", "summary") or out["reason"] == "" \
        or "years of experience" not in out["reason"]
