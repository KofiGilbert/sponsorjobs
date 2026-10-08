"""Acceptance tests for the intelligent, LLM-driven intake (CLAUDE.md §4).

Covers:
  * natural, messy input in one message is understood (name, email, a link, two
    degrees, a job all at once) — no rigid form,
  * a URL is routed to the profile links, never mis-slotted as a degree/heading,
  * persistent memory: the profile + essentials are saved, and on a later visit
    they're preloaded so nothing is re-asked,
  * a one-page CV is produced from the conversation.

Driven with the deterministic FakeLLM extractor — no network.
"""

from __future__ import annotations

from intake.memory import ConversationMemory
from ui.records import CVRecords
from ui.session import WebIntake
from conftest import requires_latex

JD = ("AI Architect - Synechron - Chicago. Generative AI, RAG, MLOps, Azure, "
      "AWS, governance, machine learning.")

MESSY = ("I'm Maya Rodriguez, maya@example.com, https://github.com/maya . "
         "I did an M.S. in AI at Northwestern in 2018 and a B.S. at UIUC in 2015. "
         "I work as Principal Engineer at Continental Trust Bank since Feb 2021.")

# Satisfies the full template SKELETON gate in one message (name, contact, address, all
# three links, education with dates + courses, experience with location + dates, and an
# explicit "no projects" decline). The mandatory outside-work sections (Extracurricular +
# Interests) are collected separately (EXTRAS) before the CV builds.
COMPLETE = ("I'm Maya Rodriguez, maya@example.com, https://github.com/maya, "
            "https://www.linkedin.com/in/maya, https://mayablog.com . I live at 88 Oak St, Chicago, IL 60614. "
            "I did an M.S. in AI at Northwestern in 2018 and a B.S. at UIUC in 2015. "
            "My courses were Machine Learning, Optimization, and Statistics. "
            "I work as Principal Engineer at Continental Trust Bank in Chicago, IL since Feb 2021. "
            "I don't have projects to add.")

# The person's answer to the pre-build Extracurricular + Interests questions.
EXTRAS = ("Outside work I volunteer mentoring at a coding bootcamp, and I enjoy chess, "
          "climbing, and jazz.")


def _session(template_source, fake_llm, workdir, db=":memory:", jd=JD, jobname="cv"):
    mem = ConversationMemory(db)
    recs = CVRecords(db)
    return WebIntake(jd, template_source, fake_llm, mem, recs, workdir, jobname=jobname), mem, recs


# -- pure extractor ----------------------------------------------------- #

def test_extract_intake_understands_one_messy_message(fake_llm):
    known = {"identity": {}, "education": [], "experience": []}
    hist = [{"role": "agent", "content": "tell me about yourself"},
            {"role": "user", "content": MESSY}]
    out = fake_llm.extract_intake(JD, known, hist)
    e = out["essentials"]
    assert e["identity"]["name"] == "Maya Rodriguez"
    assert e["identity"]["email"] == "maya@example.com"
    assert "github.com/maya" in e["identity"]["github"]        # link routed
    assert len(e["education"]) == 2                             # two degrees at once
    assert e["experience"][0]["org"] == "Continental Trust Bank"
    assert e["experience"][0]["title"] == "Principal Engineer"
    assert e["experience"][0]["dates"] == "Feb 2021 - Present"  # "since X" -> ongoing range
    assert out["ready"] is True


def test_url_is_routed_to_links_not_education(fake_llm):
    known = {"identity": {}, "education": [], "experience": []}
    hist = [{"role": "user", "content": "here's my github https://github.com/jordan"}]
    out = fake_llm.extract_intake(JD, known, hist)
    assert "github.com/jordan" in out["essentials"]["identity"]["github"]
    assert out["essentials"]["education"] == []                # NOT a degree/heading


# -- session flow ------------------------------------------------------- #

@requires_latex
def test_one_messy_message_yields_one_page_cv(template_source, fake_llm, workdir):
    s, mem, recs = _session(template_source, fake_llm, workdir)
    s.start()
    s.submit(COMPLETE)                                   # asks the mandatory outside-work sections
    st = s.submit(EXTRAS)                                # provided -> a one-page CV builds
    assert st["phase"] == "review"
    assert s.assembled.compile.pages == 1
    assert st["one_page"] is True
    # essentials were extracted from the single natural message
    assert s.essentials["identity"]["name"] == "Maya Rodriguez"
    assert len(s.essentials["education"]) == 2
    assert s.essentials["experience"][0]["org"] == "Continental Trust Bank"


@requires_latex
def test_returning_recognized_without_accept(template_source, fake_llm, workdir):
    """The user's real scenario: build a CV but never click Accept & Save, close
    and reopen — the app must still recognize the person (autosave every turn),
    and "use what you know about me" must build, not error."""
    db = str(workdir / "mem.db")
    palace = workdir / "palace"
    s1 = WebIntake(JD, template_source, fake_llm, ConversationMemory(db), CVRecords(db),
                   workdir, jobname="c1", palace_dir=palace)
    s1.start()
    s1.submit(COMPLETE)                             # builds a CV, but NO accept()
    assert ConversationMemory(db).exists("default")  # remembered mid-flow, without Accept

    # Reopen for a different job (fresh session, same local store).
    s2 = WebIntake("ML Platform Lead - Northwind - Remote. Python AWS.",
                   template_source, fake_llm, ConversationMemory(db), CVRecords(db),
                   workdir, jobname="c2", palace_dir=palace)
    assert s2.returning is True
    assert s2.essentials["identity"]["name"] == "Maya Rodriguez"
    st = s2.submit("use what you know about me already to write the cv")
    assert st["phase"] == "review"
    assert s2.assembled.compile.pages == 1
    assert all("say that again" not in m.lower() for m in st["messages"])


@requires_latex
def test_memory_preloads_and_does_not_reask(template_source, fake_llm, workdir):
    db = str(workdir / "mem.db")
    s, mem, recs = _session(template_source, fake_llm, workdir, db=db)
    s.start()
    s.submit(COMPLETE)
    s.submit(EXTRAS)                                     # mandatory outside-work sections -> builds
    s.accept()
    assert mem.exists("default")

    # A later visit for a different job: profile preloaded, nothing re-asked.
    s2 = WebIntake("ML Platform Lead - Northwind - Remote. Python AWS MLOps.",
                   template_source, fake_llm, ConversationMemory(db), CVRecords(db),
                   workdir, jobname="cv2")
    assert s2.returning is True
    assert s2.essentials["identity"]["name"] == "Maya Rodriguez"     # already known
    assert [e["org"] for e in s2.essentials["experience"]] == ["Continental Trust Bank"]
    greeting = s2.start()["messages"][0]
    assert "Welcome back" in greeting and "Maya" in greeting

    # "just tailor it" builds a fresh one-page CV without collecting anything new.
    st = s2.submit("nothing new, just tailor it")
    assert st["phase"] == "review"
    assert s2.assembled.compile.pages == 1


@requires_latex
def test_start_from_sourced_job_loads_saved_profile(template_source, fake_llm, workdir):
    """Tailoring from a watchlist job must recognize a returning user (same memory
    as the New CV path) and not re-ask — even if only the profile was stored."""
    from sourcing.watchlist import Watchlist

    db = str(workdir / "mem.db")
    # A returning user: build + accept once so a profile is saved.
    s0 = WebIntake(JD, template_source, fake_llm, ConversationMemory(db),
                   CVRecords(db), workdir, jobname="c0")
    s0.start()
    s0.submit(COMPLETE)
    s0.accept()
    assert ConversationMemory(db).exists("default")

    # A job sourced into the watchlist.
    wl = Watchlist(db)
    wl.upsert_jobs([{
        "source": "lever", "source_id": "lever:spotify:x", "company": "Spotify",
        "title": "Analytics Engineer II", "location": "Stockholm", "remote": "hybrid",
        "url": "https://jobs.lever.co/spotify/x",
        "jd_text": "Analytics Engineer at Spotify. SQL, Python, experimentation.",
        "posted_at": "",
    }])
    job = wl.get_job("lever:spotify:x")

    # Start a CV from that job — the exact construction used by /api/session/start_job.
    s = WebIntake(job["jd_text"], template_source, fake_llm, ConversationMemory(db),
                  CVRecords(db), workdir, jobname="cjob")
    s.company, s.role = job["company"], job["title"]
    st = s.start()

    assert s.returning is True                                     # recognized
    assert st["preview"]["identity"]["name"] == "Maya Rodriguez"   # profile loaded, not blank
    assert "Welcome back" in st["messages"][0]

    # "use what you already know" builds from memory — no re-asking, no parse error.
    st2 = s.submit("use what you already know about me to build this cv")
    assert st2["phase"] == "review"
    assert s.assembled.compile.pages == 1
    assert all("say that again" not in m.lower() for m in st2["messages"])


def test_essentials_recovered_from_profile_when_essentials_thin():
    """A rich saved profile but empty stored essentials still recognizes the user."""
    from ui.session import _essentials_from, _wants_saved_build
    profile = {"identity": {"name": "Maya Rodriguez"},
               "education": [{"school": "Northwestern", "degree": "M.S. AI", "date": "2018"}],
               "experience": [{"org": "Acme", "roles": [{"title": "Engineer", "dates": "2021-"}]}]}
    ess = _essentials_from({"identity": {}, "education": [], "experience": []}, profile)
    assert ess["identity"]["name"] == "Maya Rodriguez"
    assert ess["education"][0]["school"] == "Northwestern"
    assert ess["experience"][0]["org"] == "Acme" and ess["experience"][0]["title"] == "Engineer"
    # phrase recognition
    assert _wants_saved_build("use what you already know about me to build this cv")
    assert _wants_saved_build("just build it from my saved profile")
    assert not _wants_saved_build("I'm Maya, I work at Acme")


def test_extracurricular_and_interests_survive_reload():
    """draft() rebuilds profile extracurricular/interests FROM essentials, so
    _essentials_from must carry them or a returning user's saved sections are
    silently deleted and re-asked every session (the fills-the-page regression)."""
    from ui.session import _essentials_from
    extra = [{"title": "Team Captain, Intramural Soccer", "date": "", "bullets": []}]
    profile = {"identity": {"name": "Maya Rodriguez"},
               "education": [], "experience": [],
               "extracurricular": extra, "interests": "Chess; trail running."}
    # Fallback: stored essentials predate the fields -> recovered from the profile.
    ess = _essentials_from({"identity": {}, "education": [], "experience": []}, profile)
    assert ess["extracurricular"] == extra
    assert ess["interests"] == "Chess; trail running."
    # Preference: stored essentials win over the profile when both exist.
    stored = {"identity": {}, "education": [], "experience": [],
              "extracurricular": [{"title": "Debate Club", "date": "", "bullets": []}],
              "interests": "Sailing."}
    ess = _essentials_from(stored, profile)
    assert ess["extracurricular"][0]["title"] == "Debate Club"
    assert ess["interests"] == "Sailing."
    # Stated skills and certifications are data too: losing them on reload starves
    # _validate_skills of the person's own claims and guts the skills block.
    stored["skills_input"] = ["team leadership", "dispatch operations"]
    stored["certifications"] = ["Six Sigma Green Belt"]
    ess = _essentials_from(stored, profile)
    assert ess["skills_input"] == ["team leadership", "dispatch operations"]
    assert ess["certifications"] == ["Six Sigma Green Belt"]


@requires_latex
def test_asks_for_a_job_when_only_identity_given(template_source, fake_llm, workdir):
    s, mem, recs = _session(template_source, fake_llm, workdir)
    s.start()
    st = s.submit("Hi, I'm Alex Kim, alex@example.com")
    # Not ready — no work history yet; it should ask, not draft.
    assert st["phase"] == "chatting"
    assert s.assembled is None
    assert any("work history" in m.lower() or "job" in m.lower() for m in st["messages"])


def test_extractor_echo_cannot_wipe_unmanaged_state(template_source, fake_llm, workdir):
    """The extractor re-emits the whole essentials bag each turn; when it echoes
    back EMPTY copies of keys it doesn't manage (skills_input, interests,
    extracurricular, certifications), the session's real values must win.
    A setdefault-based merge let the empty echo win, nondeterministically wiping
    the person's own data turn by turn (the Amazon case-study skills bug)."""
    s, mem, recs = _session(template_source, fake_llm, workdir)
    s.start()
    s.essentials["skills_input"] = ["team leadership", "dispatch operations"]
    s.essentials["interests"] = "Competitive soccer."
    s.essentials["extracurricular"] = [{"title": "Team Captain", "date": "", "bullets": []}]

    real_extract = s.llm.extract_intake
    def echoing_extract(jd, essentials, history):
        out = real_extract(jd, essentials, history)
        ess = dict(out.get("essentials") or {})
        ess["skills_input"] = []          # the fatal echo
        ess["interests"] = ""
        ess["extracurricular"] = []
        out["essentials"] = ess
        return out
    s.llm.extract_intake = echoing_extract

    s.submit("Hi, I'm Alex Kim, alex@example.com")
    assert s.essentials["skills_input"] == ["team leadership", "dispatch operations"]
    assert s.essentials["interests"] == "Competitive soccer."
    assert s.essentials["extracurricular"][0]["title"] == "Team Captain"


@requires_latex
def test_whole_page_is_editable_and_the_one_page_rule_holds(template_source, fake_llm, workdir):
    """Kofi (2026-10-08): "the whole page should be completely editable but restrict the user
    to the one page resume rule, only when it will overflow to 2 pages"."""
    s, _mem, _recs = _session(template_source, fake_llm, workdir)
    s.start(); s.submit(COMPLETE); s.submit(EXTRAS)
    assert s.assembled.compile.pages == 1
    # Any text: the name, a school, a company, a skills line.
    st = s.edit_field("id-name", "Maya R. Rodriguez")
    assert st["edit_ok"] is True and s.profile["identity"]["name"] == "Maya R. Rodriguez"
    st = s.edit_field("edu-0-school", "Universidad de Chile")
    assert st["edit_ok"] and s.profile["education"][0]["school"] == "Universidad de Chile"
    skills_before = dict(s.profile["skills"])
    first_label = next(iter(skills_before))
    st = s.edit_field("skill-0", f"{first_label}: SQL, Python, Tableau")
    assert st["edit_ok"] and s.profile["skills"][first_label] == "SQL, Python, Tableau"
    assert s.assembled.compile.pages == 1
    # An edit that would overflow is refused, and the page is exactly as it was.
    before = s.profile["identity"]["name"]
    long_school = "Universidad de Chile, " * 400
    st = s.edit_field("edu-0-school", long_school)
    assert st["edit_ok"] is False and "second page" in st["edit_error"]
    assert s.profile["education"][0]["school"] == "Universidad de Chile"   # untouched
    assert s.profile["identity"]["name"] == before and s.assembled.compile.pages == 1
    # An unknown address is refused politely, never a crash.
    assert s.edit_field("nope-3", "x")["edit_ok"] is False
