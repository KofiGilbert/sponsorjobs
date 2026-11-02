"""Two made-up test people for the full-width line rule (tests/test_linefit.py and the
before/after gallery script). Neither is a real person; every name and number is invented."""

from __future__ import annotations

JD = (
    "Senior Business Intelligence Analyst. We are hiring an analyst to own reporting and "
    "performance insight. Required: SQL, Python, Power BI, Tableau, Looker, dbt, Snowflake, "
    "ETL, data modeling, data visualization, dashboards, forecasting, A/B testing, statistics, "
    "KPI design, stakeholder management, Agile, project management, financial modeling, "
    "variance analysis and Excel. Experience with Airflow and AWS is a plus."
)

# A person with plenty of real skills: enough for 5 to 6 full category lines.
RICH_POOL = [
    "SQL", "Python", "pandas", "NumPy", "R", "Power BI", "Tableau", "Excel", "Snowflake",
    "Airflow", "AWS", "Git", "ETL", "Data Modeling", "Data Visualization", "Dashboards",
    "Forecasting", "A/B Testing", "Statistics", "Regression", "Hypothesis Testing",
    "Agile", "Scrum", "Project Management", "Process Improvement", "Change Management",
    "Requirements Gathering", "Financial Modeling", "Variance Analysis", "Budgeting",
    "KPI Design", "Stakeholder Management", "Mentoring", "Vendor Management",
    "SAS", "VBA", "DAX", "Power Query", "BigQuery", "Google Analytics", "Jira", "Confluence",
    "Machine Learning", "Time Series", "Cohort Analysis", "Customer Segmentation", "Pricing",
    "Market Sizing", "Business Case", "Kanban", "Lean", "Six Sigma", "Training",
    "Public Speaking", "Docker",
]

RICH = {
    "identity": {"name": "Ama Owusu-Test", "email": "ama.test@example.com",
                 "phone": "(555) 010-2000", "address": "Austin, TX"},
    "education": [{"school": "State University", "location": "Austin, TX",
                   "degree": "Bachelor of Science in Economics", "date": "May 2016"}],
    "skills": {
        "Analytics & BI": "SQL, Power BI, Tableau",
        "Data & Automation": "Python, Airflow",
        "Strategy & Performance": "Forecasting, KPI Design",
        "Leadership & Delivery": "Stakeholder Management, Agile",
    },
    "experience": [
        {"org": "Northwind Retail", "location": "Austin, TX", "roles": [
            {"title": "Senior Data Analyst", "dates": "March 2021 - Present", "bullets": [
                "Built Power BI and Tableau dashboards tracking weekly sales, margin and stock "
                "for 120 stores, used by regional managers.",
                "Rebuilt the SQL and Python ETL that feeds the finance warehouse in Snowflake, "
                "cutting the nightly load from 4 hours to 50 minutes.",
                "Ran A/B testing on promotions with statistics and regression, lifting basket "
                "size 6%.",
                "Led forecasting and variance analysis for the 2023 budget across eight "
                "departments.",
            ]}]},
        {"org": "Contoso Logistics", "location": "Dallas, TX", "roles": [
            {"title": "Business Analyst", "dates": "June 2016 - February 2021", "bullets": [
                "Gathered requirements from operations and finance stakeholders and wrote the "
                "KPI design for a new carrier scorecard in Excel and Power BI.",
                "Automated weekly freight cost reporting with SQL and Airflow on AWS, saving the "
                "team 10 hours a week.",
                "Mentored three junior analysts in Agile and Scrum ways of working and in Git.",
            ]}]},
    ],
    "projects": [
        {"org": "Open Budget Explorer", "title": "Creator", "dates": "2022",
         "bullets": ["Built a Python and pandas app that turns city budget files into Tableau "
                     "data visualization for residents."]},
    ],
    "extracurricular": [
        {"title": "Volunteer Data Coach, Code Club", "date": "2019 - Present",
         "bullets": ["Teach SQL and Excel basics to adult learners on weekends."]},
    ],
    "interests": "Chess, trail running, community data projects",
}

# A person with only a handful of skills: not enough for 5 full lines. The builder must ask.
THIN_POOL = ["Excel", "SQL", "Customer Service"]

THIN = {
    "identity": {"name": "Kojo Mensah-Test", "email": "kojo.test@example.com"},
    "education": [{"school": "City College", "location": "Accra, Ghana",
                   "degree": "Diploma in Business", "date": "June 2020"}],
    "skills": {"Tools": "Excel, SQL", "People": "Customer Service"},
    "experience": [
        {"org": "Sunrise Bank", "location": "Accra, Ghana", "roles": [
            {"title": "Operations Assistant", "dates": "July 2020 - Present", "bullets": [
                "Kept daily Excel reports.",
                "Answered customer questions at the front desk and resolved account issues the "
                "same day for most walk-in customers.",
            ]}]},
    ],
    "extracurricular": [{"title": "Member, Rotaract", "date": "2021 - Present",
                         "bullets": ["Helped plan two charity drives."]}],
    "interests": "Football, music",
}
