"""An uploaded resume keeps its projects and real links, and the chat never claims it cannot
read files or GitHub (2026-11-06).

Kofi, a returning user whose saved profile had no projects and "In" / "GitHub" stored as his
links, uploaded his current resume in the chat. The build used his new titles but dropped both
projects and both links, and when he asked "didn't you see the projects that were on the resume
i uploaded? or can't you check my github" the chat said it could not access files or GitHub.

Synthetic fixtures only: a LaTeX resume with hyperlinks whose anchor text is "In" and
"GitHub", and a Projects section with an arrow link after each name and tools on the right.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from conftest import requires_latex
from intake.links import clean_identity_links, link_ok, projects_from_text, resume_links
from intake.memory import ConversationMemory
from llm.anthropic_client import AnthropicLLM
from ui.records import CVRecords
from ui.session import WebIntake

JD = "Operations Analyst - Acme Logistics - Chicago. Python, SQL, simulation, dashboards."

TEX = r"""\documentclass[10pt]{article}
\usepackage[margin=0.6in]{geometry}
\usepackage[hidelinks]{hyperref}
\pagestyle{empty}
\begin{document}
\begin{center}\textbf{\LARGE Jane Doe}\\
101 Main St, Springfield, IL 62701 | (312) 555-0100 | \href{mailto:jane@example.com}{jane@example.com} |
\href{https://www.linkedin.com/in/jane-doe-test/}{In} | \href{https://github.com/janedoe}{GitHub}
\end{center}
\noindent\textbf{Experience}\\
\textbf{Northwind Freight} \hfill Chicago, IL\\
\textbf{Operations Analyst} \hfill Jan 2021 - Present
\begin{itemize}
\item Built SQL dashboards that tracked on-time delivery for 120 routes and cut late loads by a fifth.
\end{itemize}
\noindent\textbf{Education}\\
\textbf{State University} \hfill Springfield, IL\\
Bachelor of Science in Economics \hfill May 2020

\noindent\textbf{Projects}\\
\textbf{Route Planner}~\href{https://github.com/janedoe/route-planner}{$\nearrow$} \hfill Python, OR-Tools, simulation
\begin{itemize}
\item Modeled a 40-truck fleet to test shift patterns and found a roster that lifted on-time delivery.
\end{itemize}
\textbf{Dock Scheduler}~\href{https://github.com/janedoe/dock-scheduler}{$\nearrow$} \hfill JavaScript, Firebase
\begin{itemize}
\item Shipped a dock booking app that replaced a paper sign-in sheet for inbound carriers.
\end{itemize}
\noindent\textbf{Skills}\\
Python, SQL, Excel, Tableau
\end{document}
"""


@pytest.fixture(scope="module")
def resume_pdf(tmp_path_factory):
    d = tmp_path_factory.mktemp("pdf")
    (d / "resume.tex").write_text(TEX)
    env = dict(os.environ, PATH="/opt/homebrew/bin:/Library/TeX/texbin:" + os.environ.get("PATH", ""))
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "resume.tex"], cwd=d, env=env,
                   capture_output=True, timeout=120)
    pdf = d / "resume.pdf"
    if not pdf.exists():
        pytest.skip("pdflatex could not build the fixture")
    return pdf


def _session(template_source, fake_llm, workdir, memory=None):
    return WebIntake(JD, template_source, fake_llm, memory or ConversationMemory(":memory:"),
                     CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")


# -- links -------------------------------------------------------------------------------- #

def test_anchor_text_is_never_a_link():
    for bad in ("In", "GitHub", "LinkedIn", "Portfolio", "https://www.linkedin.com/", "github.com"):
        assert not link_ok("linkedin", bad), bad
    assert link_ok("linkedin", "https://www.linkedin.com/in/jane-doe-test/")
    assert link_ok("github", "github.com/janedoe")
    ident = {"linkedin": "In", "github": "GitHub", "blog": "janedoe.dev"}
    clean_identity_links(ident)
    assert ident == {"linkedin": "", "github": "", "blog": "janedoe.dev"}


@requires_latex
def test_pdf_links_give_real_urls_and_project_rows(resume_pdf):
    from intake.cv_import import extract_text, pdf_links
    found = resume_links(pdf_links(resume_pdf))
    assert found["identity"]["linkedin"] == "https://www.linkedin.com/in/jane-doe-test/"
    assert found["identity"]["github"] == "https://github.com/janedoe"
    rows = {p["title"]: p for p in found["projects"]}
    assert rows["Route Planner"]["link"] == "https://github.com/janedoe/route-planner"
    assert "OR-Tools" in rows["Route Planner"]["tools"]
    assert rows["Dock Scheduler"]["link"] == "https://github.com/janedoe/dock-scheduler"
    projects = projects_from_text(extract_text(resume_pdf))
    assert [p["org"] for p in projects] == ["Route Planner", "Dock Scheduler"]
    assert projects[0]["bullets"][0].startswith("Modeled a 40-truck fleet")


# -- the upload path (the chat's upload button) ------------------------------------------- #

def _returning_memory():
    """A returning person's older saved profile: old title, no projects, anchor-text links."""
    mem = ConversationMemory(":memory:")
    ident = {"name": "Jane Doe", "email": "jane@example.com", "phone": "(312) 555-0100",
             "address": "101 Main St, Springfield, IL 62701", "linkedin": "In", "github": "GitHub"}
    exp = [{"org": "Northwind Freight", "title": "Junior Clerk", "dates": "Jan 2021 - Present",
            "location": "Chicago, IL", "bullets": ["Filed shipping paperwork for the night shift."]}]
    edu = [{"school": "State University", "degree": "Bachelor of Science in Economics",
            "date": "May 2020", "location": "Springfield, IL", "courses": ""}]
    profile = {"identity": ident, "experience": [{"org": "Northwind Freight", "location": "Chicago, IL",
               "roles": [{"title": "Junior Clerk", "dates": "Jan 2021 - Present",
                          "bullets": exp[0]["bullets"]}]}],
               "education": edu, "projects": [], "skills": {}}
    mem.save("default", profile, {"identity": ident, "education": edu, "experience": exp,
                                  "projects": [], "interests": "Chess.",
                                  "declined": ["projects", "extracurricular"]}, [])
    return mem


@requires_latex
def test_upload_keeps_projects_and_links_over_an_older_saved_profile(
        resume_pdf, template_source, fake_llm, workdir):
    from intake.cv_import import extract_text, pdf_links
    mem = _returning_memory()
    s = _session(template_source, fake_llm, workdir, mem)
    s.start()
    assert s.returning
    # The offline extractor cannot read this layout's roles; stand in for the model's reading
    # of them (what the real model returns), and check the saved title does not win.
    real = s.llm.extract_intake
    seen_known = []

    def reads_roles(jd, known, history, with_projects=False):
        seen_known.append(known)
        out = real(jd, known, history, with_projects=with_projects)
        out["essentials"]["experience"] = [{"org": "Northwind Freight", "title": "Operations Analyst",
                                            "dates": "Jan 2021 - Present", "location": "Chicago, IL",
                                            "bullets": []}]
        return out
    s.llm.extract_intake = reads_roles
    s.ingest_document(extract_text(resume_pdf), "Jane_Doe_Resume.pdf", links=pdf_links(resume_pdf))

    ident = s.essentials["identity"]
    assert ident["linkedin"] == "https://www.linkedin.com/in/jane-doe-test/"
    assert ident["github"] == "https://github.com/janedoe"
    names = [p["org"] for p in s.essentials["projects"]]
    assert names == ["Route Planner", "Dock Scheduler"]
    route = s.essentials["projects"][0]
    assert route["link"] == "https://github.com/janedoe/route-planner"
    assert "OR-Tools" in route["location"]                 # tools in the right-hand slot
    assert "projects" not in s.essentials.get("declined", [])
    # The upload's title is the newer truth, not the saved "Junior Clerk".
    assert [j["title"] for j in s.essentials["experience"]] == ["Operations Analyst"]
    assert seen_known[0]["experience"] == []          # the file is read on its own, not merged
    # The upload had no bullets for the role in this stand-in, and its title differs, so the
    # old clerk bullet is not borrowed onto the new title.
    assert "Filed shipping paperwork" not in str(s.essentials["experience"])

    # The built resume shows both projects and the real links.
    st = s._state()
    assert s.assembled is not None, st.get("messages")
    used = s.assembled.profile_used
    assert {p["org"] for p in used["projects"]} == {"Route Planner", "Dock Scheduler"}
    tex = "\n".join(p.read_text() for p in workdir.rglob("*.tex"))
    assert "github.com/janedoe/route-planner" in tex
    assert "linkedin.com/in/jane-doe-test" in tex and "https://In" not in tex

    # Saved for next time (§4b): a new session starts from the upload.
    s2 = _session(template_source, fake_llm, workdir / "again", mem)
    assert [p["org"] for p in s2.essentials["projects"]] == ["Route Planner", "Dock Scheduler"]
    assert s2.essentials["identity"]["github"] == "https://github.com/janedoe"


def test_merge_upload_fills_only_blanks_from_the_saved_entry():
    from ui.session import _merge_upload
    saved = {"identity": {"name": "Jane Doe", "phone": "1"}, "skills_input": ["SQL"],
             "experience": [{"org": "Stanbic Bank", "title": "Analyst", "dates": "2018 - 2020",
                             "location": "Accra, GH", "bullets": ["Old real bullet."]},
                            {"org": "Old Co", "title": "Clerk", "dates": "2010"}],
             "education": [{"school": "DePaul University", "degree": "MBA", "courses": "Stats"}],
             "projects": []}
    up = {"identity": {"name": "Jane Doe", "email": "j@x.com"},
          "experience": [{"org": "Stanbic Bank Ghana", "title": "Analyst", "dates": "2018 - 2024",
                          "location": "", "bullets": []}],
          "education": [{"school": "DePaul University", "degree": "MBA", "courses": ""}],
          "projects": [{"org": "Gate Check", "bullets": ["Built it."]}]}
    out = _merge_upload(saved, up)
    assert out["identity"] == {"name": "Jane Doe", "phone": "1", "email": "j@x.com"}
    assert len(out["experience"]) == 1 and out["experience"][0]["dates"] == "2018 - 2024"
    assert out["experience"][0]["location"] == "Accra, GH"          # gap filled
    assert out["experience"][0]["bullets"] == ["Old real bullet."]   # gap filled, same role
    assert out["education"][0]["courses"] == "Stats"
    assert out["projects"][0]["org"] == "Gate Check" and out["skills_input"] == ["SQL"]


def test_old_anchor_text_links_are_dropped_on_load(template_source, fake_llm, workdir):
    s = _session(template_source, fake_llm, workdir, _returning_memory())
    s._conform_essentials()
    assert s.essentials["identity"]["linkedin"] == ""
    assert s.saved_profile["identity"]["github"] == ""


# -- the chat's reply --------------------------------------------------------------------- #

def _uploaded(template_source, fake_llm, workdir, connected=False):
    s = _session(template_source, fake_llm, workdir)
    s.start()
    s.uploads.append({"name": "Jane_Doe_Resume.pdf", "kind": "resume",
                      "roles": ["Operations Analyst at Northwind Freight"],
                      "projects": ["Route Planner", "Dock Scheduler"],
                      "links": {"linkedin": "https://www.linkedin.com/in/jane-doe-test/",
                                "github": "https://github.com/janedoe"},
                      "project_links": {"Route Planner": "https://github.com/janedoe/route-planner"}})
    s.github_status = lambda: {"connected": connected, "login": "janedoe" if connected else ""}
    return s


def test_app_context_reaches_the_reply_prompt(template_source, fake_llm, workdir):
    s = _uploaded(template_source, fake_llm, workdir)
    ctx = s._app_context()
    assert "Jane_Doe_Resume.pdf" in ctx and "Route Planner" in ctx
    assert "GitHub is NOT connected" in ctx and "Connect GitHub button" in ctx
    assert "Settings, then Connections" not in ctx            # no menu steps (2026-11-09)
    assert "Projects page" in ctx

    llm = AnthropicLLM.__new__(AnthropicLLM)          # no client, no network
    seen = {}
    llm._complete = lambda system, user, **kw: seen.update(system=system, user=user) or "ok"
    llm.converse(JD, [{"role": "user", "content": "did you see my projects?"}], [], [],
                 app_context=ctx)
    assert "Route Planner" in seen["system"] and "never say you cannot" in seen["system"].lower()


def test_question_about_the_upload_is_answered_from_what_was_read(template_source, fake_llm, workdir):
    s = _uploaded(template_source, fake_llm, workdir)
    st = s.submit("didn't you see the projects that were on the resume i uploaded?")
    reply = " ".join(st.get("messages") or [])
    assert "Route Planner" in reply
    assert fake_llm.last_app_context and "Jane_Doe_Resume.pdf" in fake_llm.last_app_context


def test_a_reply_that_denies_reading_files_is_replaced(template_source, fake_llm, workdir):
    s = _uploaded(template_source, fake_llm, workdir)
    s.llm.converse = lambda *a, **k: ("I can't access files or GitHub directly, only what you "
                                      "paste into our chat.")
    st = s.submit("didn't you see the projects on the resume i uploaded? or can't you check my github")
    reply = " ".join(st.get("messages") or [])
    assert "can't access files" not in reply
    assert "I read Jane_Doe_Resume.pdf" in reply and "Route Planner" in reply
    assert "GitHub isn't connected" in reply and "Settings" not in reply
    assert any(b.get("nav") == "github" for c in st["cards"] for b in c.get("buttons") or [])
    assert "—" not in reply and "–" not in reply


def test_the_question_does_not_decline_projects_or_github(template_source, fake_llm, workdir):
    """The proof run: the question itself read as "declined projects and GitHub", which wiped
    the projects the upload had just added."""
    s = _uploaded(template_source, fake_llm, workdir)
    s.essentials["projects"] = [{"org": "Route Planner", "bullets": ["Modeled a fleet."]}]
    s.submit("didn't you see the projects on the resume i uploaded? or can't you check my github")
    assert not {"projects", "github"} & set(s.essentials.get("declined") or [])
    assert [p["org"] for p in s.essentials["projects"]] == ["Route Planner"]
    s.submit("I don't have a github")                 # a real decline still counts
    assert "github" in s.essentials["declined"]


def test_connected_github_is_said_as_connected(template_source, fake_llm, workdir):
    s = _uploaded(template_source, fake_llm, workdir, connected=True)
    ans = s._capability_answer("can you check my github?")
    assert "connected as janedoe" in ans and "isn't connected" not in ans


def test_material_with_a_link_is_not_hijacked(template_source, fake_llm, workdir):
    s = _uploaded(template_source, fake_llm, workdir)
    assert not s._asks_about_upload("add my repo https://github.com/janedoe/new-thing please")
    assert not s._asks_about_upload("I don't have a github")
    assert s._asks_about_upload("can't you check my github for more projects?")
