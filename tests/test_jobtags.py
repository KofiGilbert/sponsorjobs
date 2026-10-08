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
