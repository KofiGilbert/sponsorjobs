"""The employer a pasted job description names (ui/session._jd_company).

Kofi's Smithsonian D365 paste (2026-11-10) opened with metadata ("OPEN DATE: November 9, 2026")
and no "at Company" line, so the session showed the title alone. The company now comes from the
shapes real ads use; with no match it stays blank, never a guess."""

from ui.session import _jd_company, _jd_label

D365 = """OPEN DATE: November 9, 2026
CLOSING DATE: November 23, 2026
POSITION TYPE: Trust Fund
APPOINTMENT TYPE: Indefinite
SCHEDULE: Full Time
*DUTY LOCATION: Washington, DC
*

Position sensitivity and risk: Low Risk

*Open to all qualified applicants
*

What are Trust Fund Positions?
Trust Fund positions are unique to the Smithsonian. They are paid for from a variety of sources.

The D365 Software Developer plays a key role in advancing Smithsonian Enterprises (SE) technology
initiatives by designing, developing and supporting secure, scalable applications.

The Smithsonian Institution provides reasonable accommodation to applicants with disabilities.
Applicants requiring reasonable accommodation should contact SICareers@si.edu.
The Smithsonian Institution is an Equal Opportunity Employer."""


def test_the_smithsonian_paste_names_its_employer():
    assert _jd_label(D365) == ("D365 Software Developer", "Smithsonian Institution")


def test_a_company_label():
    jd = "Senior Data Analyst\nCompany: Northwind Traders\nLocation: Chicago, IL\n\nYou will build dashboards."
    assert _jd_label(jd) == ("Senior Data Analyst", "Northwind Traders")
    assert _jd_company("Job Title: Analyst\nEmployer: Contoso Ltd.\nSalary: $90,000") == "Contoso Ltd"


def test_is_seeking_and_is_hiring():
    jd = ("Business Analyst\n\nBank of America is seeking a Business Analyst to join our "
          "Consumer Banking team. You will gather requirements.")
    assert _jd_label(jd)[1] == "Bank of America"
    assert _jd_company("Data Engineer\nOverview\nAcme Robotics is hiring a Data Engineer.") == "Acme Robotics"
    # "We are seeking" or "The ideal candidate is looking for" names no one.
    assert _jd_company("Analyst\nWe are seeking an analyst. The ideal candidate is looking for growth.") == ""


def test_about_heading():
    jd = "Product Manager\n\nAbout Stripe\nStripe is a financial infrastructure platform.\n\nAbout the role\nYou will..."
    assert _jd_label(jd)[1] == "Stripe"
    assert _jd_company("Analyst\n\nAbout us\nWe build software.\n\nAbout the team\nSmall.") == ""


def test_a_name_line_beside_the_title_when_the_ad_names_it_again():
    jd = ("Duolingo\nSenior Data Scientist\nPittsburgh, PA\n\n"
          "Join Duolingo to make learning free for everyone. You will run experiments.")
    assert _jd_label(jd) == ("Senior Data Scientist", "Duolingo")
    # A lone line the ad never names again is not taken (it may be a team or a place).
    assert _jd_company("Data Platform\nSenior Data Engineer\n\nYou will build pipelines.",
                       "Senior Data Engineer") == ""


def test_the_address_or_link_when_the_ad_names_it_in_words():
    jd = ("Operations Analyst\n\nJoin the Ridgeline Health operations team. "
          "Send your resume to careers@ridgelinehealth.com.")
    assert _jd_company(jd, "Operations Analyst") == "Ridgeline Health"
    # A job board's link or a mail host names no employer.
    assert _jd_company("Analyst\nApply at https://boards.greenhouse.io/x or mail jo@gmail.com",
                       "Analyst") == ""


def test_no_match_stays_blank():
    jd = "Data Analyst\n\nYou will write SQL and build Tableau dashboards for the finance team."
    assert _jd_label(jd) == ("Data Analyst", "")
