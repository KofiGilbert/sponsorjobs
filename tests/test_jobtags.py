"""Function tags from title + description (sourcing/jobtags.py), on real titles from the feed."""
from sourcing.jobtags import job_tags

BA_AD = ("Technical Analyst IV. Gathers and documents business requirements, writes user stories and "
         "acceptance criteria, works with Data Science & Analytics teams, builds dashboards, runs "
         "SQL analytics, and supports requirements gathering across stakeholders. Analytics-driven.")


def test_title_hits_lead_and_body_adds_specific_areas():
    tags = job_tags("Technical Analyst IV", BA_AD)
    assert tags[:2] == ["Data Science & Analytics", "Business Analysis"] or set(tags[:2]) == {"Data Science & Analytics", "Business Analysis"}


def test_a_stray_word_does_not_tag_a_job():
    # "building great products" and "supports the sales team" appear once each: not tags.
    tags = job_tags("Software Engineer", "We are building great products. You will support the sales team "
                    "occasionally. Python, APIs, backend services, cloud infrastructure on AWS and Kubernetes.")
    assert "Construction & Facilities" not in tags and "Sales" not in tags
    assert "Software Engineering" in tags and "Cloud & Infrastructure" in tags


def test_title_stems_match_full_words():
    assert "Product Management" in job_tags("Product Manager II, AI & Data Security", "")
    assert "Manufacturing Operations" in job_tags("Manufacturing Engineer", "")


def test_capped_and_never_empty_for_a_recognised_title():
    assert len(job_tags("Data Scientist", "data " * 50 + " analytics " * 50 + " machine learning " * 50 +
                        " research " * 50 + " statistics " * 50)) <= 9
    assert job_tags("Registered Nurse", "") == ["Healthcare & Nursing"]
    assert job_tags("Zorblax Operator", "") == []


# Real ad text from the feed (2026-11-10): a stem that ran on into another word, or a short word
# with a second meaning, put the wrong industry on the card.
MONGO_SRE = ("Our relatively new team is building performant, multi-tenant distributed storage services. "
             "Work on our multi-tenant distributed storage systems, moving from legacy storage stacks to new "
             "multi-tenant storage architectures. Designing secure, multi-tenant runtime environments at scale "
             "on AWS, GCP, or Azure. Kubernetes, site reliability, on-call. Apache Kafka message broker.")


def test_multi_tenant_software_is_not_real_estate():
    tags = job_tags("Site Reliability Engineer (Senior or Staff), Storage Layer Services (SLS)", MONGO_SRE)
    assert "Real Estate" not in tags
    assert "Cloud & Infrastructure" in tags
    # Real real estate still tags.
    assert "Real Estate" in job_tags("Leasing Consultant", "Show apartments to prospective residents. "
                                     "Handle tenant relations and lease renewals for the property manager.")


def test_brokerage_and_product_portfolio_are_not_real_estate_or_investing():
    bank = "Recommend brokerage products. Build a financial plan with brokerage products."
    assert "Real Estate" not in job_tags("Financial Solutions Advisor", bank)
    ad = "Own the product portfolio. Grow the solutions portfolio across our portfolio of brands."
    assert "Investment & Trading" not in job_tags("Senior Product Marketing Manager", ad)
    assert "Investment & Trading" in job_tags("Portfolio Manager, Fixed Income", "")


def test_stems_stop_at_a_different_word():
    assert "Maintenance & Repair" not in job_tags(
        "Thermal & Mechanical Packaging Engineer", "mechanical design, mechanical engineer, mechanical tests")
    assert "Maintenance & Repair" in job_tags("Automotive Technician", "Diagnose and repair brakes. Explain repairs.")
    assert "Healthcare & Nursing" not in job_tags("Sales Coordinator", "hospitality team, hospitality group")
    assert "Data Engineering" not in job_tags("Seasonal Sales Associate-1383 Sparks North, NV 89436", "")
    assert "Communications & PR" not in job_tags("Fund Tax Manager", "calm under pressure, the pressures "
                                                 "facing our clients, communications with clients")


def test_short_words_with_a_second_meaning():
    shifts = "Shifts 6 pm to 2 am. Weekends 10am-6pm. Pay $20/hr, 40 hr weeks, 24 hr store."
    tags = job_tags("Sales Associate", shifts)
    assert "Product Management" not in tags and "Human Resources" not in tags
    assert "Medical & Physicians" not in job_tags("Seasonal Sales Associate-8227 Woodlawn, MD 21207", "")
    assert "Supply Chain & Logistics" not in job_tags(
        "Data Architect", "Design the data warehouse. Migrate the enterprise data warehouse to Azure.")
    assert "Cloud & Infrastructure" not in job_tags(
        "Animal Welfare Specialist", "Good Clinical Practice (GCP). Experience with GLP, GCP, and/or GMP.")
    assert "Finance & Accounting" not in job_tags(
        "Growth Marketing Manager", "Analyze CTR, CPC, CPA, Conversion Rate. Optimize CPA, ROAS, etc.")
    assert "Finance & Accounting" in job_tags("Senior Accountant", "CPA preferred. GAAP, month-end close.")


def test_a_hiring_line_about_ai_tools_is_not_an_ai_job():
    ad = ("We ask that candidates do not use AI tools during interviews. AI tools are not permitted. "
          "Help guests find the right gear and run the register.")
    assert "Machine Learning & AI" not in job_tags("Teammate - Sales", ad)
    assert "Machine Learning & AI" in job_tags("Machine Learning Engineer", "")
