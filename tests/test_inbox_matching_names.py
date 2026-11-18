"""Matching mail to an application by the employer's distinctive name (inbox/matching.py).

Most confirmations come from an applicant tracking system's own domain, which says nothing about
the employer, so the name in the sender's display name, the subject, the domain label or the
first lines of the body places them; the role title makes it sure. Made-up mail."""
from __future__ import annotations

import pytest

from inbox.matching import COMPANY, DOMAIN, ROLE, distinctive_name, match_application
from inbox.suggestions import build_suggestions

OCTANE = {"source_id": "pasted::octane-bi", "source": "pasted", "company": "Octane Lending Inc",
          "title": "Analyst, Business Intelligence", "url": "",          # a pasted job: no link to go by
          "stage": "applied"}
BARCLAYS = {"source_id": "workday::barclays-ra", "source": "workday", "company": "Barclays Services Corp",
            "title": "Research Analyst", "url": "https://barclays.wd3.myworkdayjobs.com/External/job/NY/Research-Analyst_R-1",
            "stage": "applied"}
COTIVITI = {"source_id": "careers::cotiviti-da", "source": "careers", "company": "Cotiviti",
            "title": "Data Analyst", "url": "https://careers.cotiviti.com/jobs/1", "stage": "applied"}
JOBS = [OCTANE, BARCLAYS, COTIVITI]


def mail(frm, subject, body):
    return {"id": f"alias:{abs(hash((frm, subject))) % 10**6}", "from": frm, "subject": subject, "body": body, "date": ""}


OCTANE_RECEIVED = mail("Octane <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Octane",
                       "Hi Zoe,\n\nThank you for applying to the Analyst, Business Intelligence role at Octane. "
                       "We have received your application and will review it shortly.\n\nThe Octane team")
BARCLAYS_ASSESSMENT = mail("Barclays Careers <workday@barclays.com>", "Action required: complete your online assessment",
                           "Dear Zoe,\n\nAs the next step for the Research Analyst position, please complete the "
                           "online assessment within five days.\n\nBarclays Careers")


@pytest.mark.parametrize("company, name", [
    ("Octane Lending Inc", "octane"),
    ("Barclays Services Corp", "barclays"),
    ("Cotiviti", "cotiviti"),
    ("Gusto, Inc.", "gusto"),
    ("Northwind Traders", "northwind traders"),
    ("Acme Robotics Technologies LLC", "acme robotics"),
    ("First American Financial Corporation", "first american"),
    ("First Financial Inc", "first financial"),          # never down to an everyday single word
    ("American Group Holdings", "american group"),
    ("The Dunmore Company", "dunmore"),
    ("", ""),
])
def test_distinctive_name_drops_legal_and_generic_tails(company, name):
    assert distinctive_name(company) == name


def test_greenhouse_confirmation_names_the_company_in_the_sender_and_subject():
    job, lane = match_application(OCTANE_RECEIVED, JOBS)
    assert job["source_id"] == OCTANE["source_id"] and lane == ROLE      # the role is in the body too


def test_workday_mail_from_the_employers_own_domain_label():
    # The job link is on myworkdayjobs.com, so there is no employer domain to compare: the
    # domain label "barclays" and the display name place it.
    job, lane = match_application(BARCLAYS_ASSESSMENT, JOBS)
    assert job["source_id"] == BARCLAYS["source_id"] and lane == ROLE
    bare = mail("workday@barclays.com", "An update", "Please sign in to see your status.")
    job, lane = match_application(bare, JOBS)
    assert job["source_id"] == BARCLAYS["source_id"] and lane == COMPANY    # the domain label alone


def test_ats_rejection_that_names_the_company_only_in_the_body():
    msg = mail("Recruiting <no-reply@hire.lever.co>", "An update on your application",
               "Hi Zoe,\n\nThank you for your interest in Cotiviti. Unfortunately, we have decided to pursue "
               "other candidates at this time.\n\nBest of luck")
    jobs = [OCTANE, BARCLAYS, {**COTIVITI, "url": "https://jobs.lever.co/cotiviti/abc"},
            {"source_id": "lever::other", "company": "Elkhorn Studio", "title": "Designer",
             "url": "https://jobs.lever.co/elkhorn/x", "stage": "applied"}]
    job, lane = match_application(msg, jobs)
    assert job["company"] == "Cotiviti" and lane == COMPANY


def test_two_applications_at_one_company_the_role_in_the_mail_decides():
    older = {**OCTANE, "source_id": "pasted::octane-de", "title": "Data Engineer"}
    jobs = [older, OCTANE]                                   # the board lists the most recent first
    job, lane = match_application(OCTANE_RECEIVED, jobs)
    assert job["source_id"] == OCTANE["source_id"] and lane == ROLE
    # No role in the mail: the most recent one still in play.
    plain = mail("Octane <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Octane", "We received it.")
    job, lane = match_application(plain, jobs)
    assert job["source_id"] == older["source_id"] and lane == COMPANY
    job, _ = match_application(plain, [{**older, "stage": "archived"}, OCTANE])
    assert job["source_id"] == OCTANE["source_id"]           # an archived one gives way to one in play


def test_a_newsletter_that_mentions_a_company_far_down_does_not_match():
    body = ("Your weekly roundup of new roles near Boston.\n\n" + "\n".join(
        f"{i}. Something Engineer at Company {i}, posted this week." for i in range(1, 12))
        + "\n\nAlso hiring this week: Octane, Barclays and Cotiviti are growing their teams.")
    news = mail("Job Board Weekly <news@jobboard.example>", "12 new roles for you", body)
    assert match_application(news, JOBS) == (None, "")


def test_the_name_must_be_a_whole_word():
    msg = mail("Team <hello@mailer.example>", "High-octane growth tips", "Read about cotivitinew and octanes.")
    job, _lane = match_application(msg, [COTIVITI, BARCLAYS])
    assert job is None
    assert match_application(mail("x@mailer.example", "Barclaysbank news", "nothing"), [BARCLAYS]) == (None, "")


def test_a_short_or_everyday_name_needs_the_role_too():
    ibm = {"source_id": "c::abc", "company": "ABC Inc", "title": "Supply Planner", "url": "", "stage": "applied"}
    first = {"source_id": "c::first", "company": "First", "title": "Credit Analyst", "url": "", "stage": "applied"}
    assert match_application(mail("ABC <no-reply@icims.com>", "Thanks from ABC", "We got it."), [ibm]) == (None, "")
    job, lane = match_application(mail("ABC <no-reply@icims.com>", "Thanks from ABC",
                                       "We received your application for Supply Planner."), [ibm])
    assert job["source_id"] == "c::abc" and lane == ROLE
    assert match_application(mail("News <n@mailer.example>", "First look at the week", "Hello"), [first]) == (None, "")
    job, lane = match_application(mail("First <no-reply@myworkday.com>", "Your application",
                                       "Thank you for applying for Credit Analyst."), [first])
    assert job["source_id"] == "c::first" and lane == ROLE


def test_an_ats_domain_label_never_names_an_employer():
    lever = {"source_id": "c::lever", "company": "Lever Tools Inc", "title": "Machinist II", "url": "", "stage": "applied"}
    msg = mail("no-reply@hire.lever.co", "Application update", "Please sign in.")
    assert match_application(msg, [lever]) == (None, "")


def test_two_applications_on_one_ats_board_the_role_decides_there_too():
    a = {"source_id": "greenhouse::dunmore-pe", "company": "Dunmore", "title": "Platform Engineer",
         "url": "https://boards.greenhouse.io/dunmore/jobs/1", "stage": "applied"}
    b = {**a, "source_id": "greenhouse::dunmore-sre", "title": "Site Reliability Engineer",
         "url": "https://boards.greenhouse.io/dunmore/jobs/2"}
    msg = mail("Dunmore <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Dunmore",
               "We received your application for Site Reliability Engineer.")
    job, lane = match_application(msg, [a, b])
    assert job["source_id"] == b["source_id"] and lane == DOMAIN


def test_the_sender_domain_still_wins_over_the_name():
    msg = mail("Cotiviti Talent <talent@cotiviti.com>", "Thank you for applying", "We received your application.")
    job, lane = match_application(msg, JOBS)
    assert job["source_id"] == COTIVITI["source_id"] and lane == DOMAIN


def test_a_confirmation_moves_the_card_only_when_the_role_matches_too():
    saved = [{**OCTANE, "stage": "saved"}]
    sure = build_suggestions([OCTANE_RECEIVED], saved)
    assert [(s["matched_by"], s["label"], s["stage"], s["auto"]) for s in sure] == [("role", "acknowledgement", "applied", True)]
    name_only = mail("Octane <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Octane",
                     "Hi Zoe,\n\nThank you for applying. We have received your application.")
    tap = build_suggestions([name_only], saved)
    assert [(s["matched_by"], s["stage"], s["auto"]) for s in tap] == [("company", "applied", False)]
