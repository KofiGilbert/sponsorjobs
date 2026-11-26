"""A pasted job link is read before anything is tailored (sourcing/posting_read.py, 2026-11-26).

Found on a real Lever application: pasting the link on Home started a resume chat whose job
description was the bare link, so nothing was tailored to the job. Every reader here runs on
saved fixtures (tests/fixtures/posting_read); no test touches the network.
"""

from __future__ import annotations

from pathlib import Path

from sourcing import posting_read as P
from sourcing.liveness import Resp

FX = Path(__file__).parent / "fixtures" / "posting_read"
LEVER = "https://jobs.lever.co/example-advisory/0a1b2c3d-1111-2222-3333-444455556666"
LEVER_API = "https://api.lever.co/v0/postings/example-advisory/0a1b2c3d-1111-2222-3333-444455556666?mode=json"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


class Net:
    """fetch(url, accept, follow) over a table of saved answers; anything else is a 404. Records
    every URL asked, so a test can prove what was (and was not) requested."""

    def __init__(self, table: dict):
        self.table, self.asked = table, []

    def __call__(self, url, accept="", follow=True):
        self.asked.append(url)
        hit = self.table.get(url)
        if hit is None:
            return Resp(404, "", url)
        status, body = hit if isinstance(hit, tuple) else (200, hit)
        return Resp(status, body, url)


# ------------------------------------------------------------------------- the link itself

def test_a_link_alone_or_with_a_short_line_is_a_link():
    assert P.link_in(LEVER) == LEVER
    assert P.link_in(f"Tailor my resume for this one: {LEVER}") == LEVER
    assert P.link_in("www.example.com/jobs/123").startswith("https://www.example.com/jobs/123")
    assert P.link_in(LEVER + "?utm_source=x&lever-source=y") == LEVER     # tracking dropped


def test_a_pasted_description_that_ends_with_its_apply_link_is_not_a_link():
    jd = ("About the role. " * 40) + "\nApply here: " + LEVER
    assert P.link_in(jd) == ""


def test_a_github_link_in_a_request_is_not_a_job_link():
    assert P.link_in("connect my github https://github.com/example") == ""


# ------------------------------------------------------------------------- the ATS readers

def test_lever_reads_the_public_json_and_the_company_from_the_hosted_page():
    net = Net({LEVER_API: _fx("lever_posting.json"), LEVER: _fx("lever_page.html")})
    res = P.read_posting(LEVER, fetch=net)
    assert res["ok"] is True
    assert res["title"] == "Business Intelligence Analyst"
    assert res["company"] == "Example Advisory"
    assert res["location"] == "Arlington, VA"
    assert res["source_id"] == "lever:example-advisory:0a1b2c3d-1111-2222-3333-444455556666"
    # the lists (what you will do, qualifications) are part of what is tailored to
    for words in ("Power BI dashboards", "Write SQL", "Qualifications", "ETL pipelines"):
        assert words in res["description"]
    assert P.confirmation(res) == "Read the posting: Business Intelligence Analyst at Example Advisory."
    jd = P.as_jd(res)
    assert jd.startswith("Business Intelligence Analyst\nExample Advisory\nArlington, VA\n\n")
    assert LEVER not in jd.split("\n\n", 1)[1][:40]                      # the text, not the link


def test_lever_without_its_page_names_the_company_from_the_link():
    net = Net({LEVER_API: _fx("lever_posting.json")})
    res = P.read_posting(LEVER, fetch=net)
    assert res["ok"] and res["company"] == "Example Advisory"


def test_greenhouse_reads_the_board_api():
    url = "https://job-boards.greenhouse.io/examplefreight/jobs/4012345?gh_src=abc"
    net = Net({"https://boards-api.greenhouse.io/v1/boards/examplefreight/jobs/4012345":
               _fx("greenhouse_job.json")})
    res = P.read_posting(url, fetch=net)
    assert res["ok"] and res["title"] == "Data Engineer" and res["company"] == "Example Freight"
    assert "Airflow and dbt" in res["description"] and "&lt;" not in res["description"]
    assert res["source_id"] == "greenhouse:examplefreight:4012345"


def test_ashby_finds_the_posting_on_the_public_board():
    url = "https://jobs.ashbyhq.com/examplelabs/5f6e7d8c-aaaa-bbbb-cccc-ddddeeeeffff"
    net = Net({"https://api.ashbyhq.com/posting-api/job-board/examplelabs": _fx("ashby_board.json")})
    res = P.read_posting(url, fetch=net)
    assert res["ok"] and res["title"] == "Product Analyst" and res["company"] == "Examplelabs"
    assert "experimentation" in res["description"]
    assert not any("jobs.ashbyhq.com/examplelabs/" in u for u in net.asked)   # its robots disallow /api/ only; the API is used


def test_a_gone_posting_is_said_so():
    res = P.read_posting(LEVER, fetch=Net({}))
    assert res["ok"] is False and res["reason"] == "closed"
    assert P.failure_message(res).startswith("I could not read that posting: Lever says this posting is gone.")


# ------------------------------------------------------------------------- any other page

def test_a_careers_page_is_read_from_its_jobposting_block_when_robots_allows():
    url = "https://careers.example-bank.com/jobs/98765-financial-analyst"
    net = Net({"https://careers.example-bank.com/robots.txt": _fx("robots_allow.txt"),
               url: _fx("careers_jsonld.html")})
    res = P.read_posting(url, fetch=net)
    assert res["ok"] and res["title"] == "Financial Analyst" and res["company"] == "Example Bank"
    assert res["location"] == "Charlotte, NC"
    assert "PnL reporting" in res["description"] and res["source_id"] == ""
    assert net.asked[0].endswith("/robots.txt")                    # the rules first, then the page


def test_robots_disallow_means_the_page_is_never_fetched():
    url = "https://careers.example-bank.com/jobs/98765-financial-analyst"
    net = Net({"https://careers.example-bank.com/robots.txt": _fx("robots_disallow_jobs.txt"),
               url: _fx("careers_jsonld.html")})
    res = P.read_posting(url, fetch=net)
    assert res["ok"] is False and res["reason"] == "robots"
    assert url not in net.asked


def test_a_bot_wall_is_a_failure_never_a_retry():
    url = "https://jobs.example.org/job/123"
    net = Net({"https://jobs.example.org/robots.txt": (404, ""), url: _fx("challenge.html")})
    res = P.read_posting(url, fetch=net)
    assert res["ok"] is False and res["reason"] == "blocked"
    assert net.asked.count(url) == 1


def test_a_page_that_loads_its_text_only_in_a_browser_asks_for_the_text():
    url = "https://jobs.example.org/job/123"
    net = Net({"https://jobs.example.org/robots.txt": (404, ""), url: _fx("js_shell.html")})
    res = P.read_posting(url, fetch=net)
    assert res["ok"] is False and res["reason"] == "no_text"
    msg = P.failure_message(res)
    assert "Paste the job description text here" in msg and "—" not in msg and "–" not in msg


def test_smartrecruiters_api_is_never_asked():
    url = "https://jobs.smartrecruiters.com/ExampleCo/744000012345678-data-analyst"
    net = Net({})
    P.read_posting(url, fetch=net)
    assert not any("api.smartrecruiters.com" in u for u in net.asked)


# ------------------------------------------------------------------------- LinkedIn

def test_linkedin_is_never_fetched_and_the_person_is_asked_to_paste():
    for url in ("https://www.linkedin.com/jobs/view/4012345678/", "https://lnkd.in/abcd"):
        net = Net({})
        res = P.read_posting(url, fetch=net)
        assert res["ok"] is False and res["reason"] == "paste_site" and net.asked == []
        msg = P.failure_message(res)
        assert msg.startswith("I do not read LinkedIn pages.") and "paste it here" in msg
