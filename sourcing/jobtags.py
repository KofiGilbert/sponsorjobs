"""Functional tags for a job, read from its title AND description.

Migrate Mate shows up to ~9 tags per card ("Business Analysis", "Data Science & Analytics",
"Civil & Structural Engineering") because it reads the whole description. Ours came from the
title alone, through 14 broad buckets, capped at two, so a Technical Analyst got "Operations".
This module gives each job a ranked list of specific functions, computed once in the feed build
(the app's slim list has no description text) and carried on the row as `tags`.

Scoring: a hit in the TITLE is worth far more than one in the body, and a tag needs either a
title hit or at least three body mentions, so a stray word ("...supports the sales team",
"building great products") does not tag an engineering job as Sales or Construction. Patterns
for the common-word functions (Sales, Marketing, Legal, Operations) require a compound phrase. Order is by score, then by the order below. Vocabulary
mirrors the categories a US job board uses; it is data, add to it freely.
"""
from __future__ import annotations

import re

# (tag, pattern). Patterns are matched case-insensitively on whole words.
TAGS: tuple[tuple[str, str], ...] = (
    ("Software Engineering", r"software engineer|software develop|backend|back-end|frontend|front-end|full[- ]?stack|web develop|mobile develop|ios|android|api develop"),
    ("Data Science & Analytics", r"data scien|data analy|analytics|statistic|predictive|forecast|a/b test|experimentation"),
    ("Machine Learning & AI", r"machine learning|\bml\b|deep learning|\bai\b|artificial intelligence|\bllm|generative|nlp|computer vision|model training"),
    ("Data Engineering", r"data engineer|data pipeline|\betl\b|\belt\b|data warehouse|spark|airflow|dbt|snowflake|databricks|kafka"),
    ("Business Analysis", r"business analy|requirements gathering|requirements analysis|user stor|acceptance criteria|process mapping|gap analysis|business requirements|functional requirements"),
    ("Product Management", r"product manag|product owner|roadmap|product strategy|backlog|product requirements|\bprd\b|product lead|head of product|\bpm\b|\bpm\s+(i{1,3}|iv|v)\b|product vision"),
    ("Project & Program Management", r"project manag|program manag|programme manag|\bpmp\b|project plan|project deliver|portfolio manag|scrum master|agile delivery"),
    ("Cloud & Infrastructure", r"\baws\b|azure|\bgcp\b|google cloud|kubernetes|docker|terraform|devops|site reliability|\bsre\b|cloud infrastructure|cloud platform|cloud engineer"),
    ("Cybersecurity", r"cyber|infosec|information security|security engineer|threat|vulnerabilit|\bsoc\b|penetration|incident response|\biam\b"),
    ("QA & Testing", r"quality assurance|\bqa\b|test automation|software test|test plan|test case|test framework"),
    ("UX & Design", r"\bux\b|\bui\b|user experience|user research|usability|figma|interaction design|visual design|product design|graphic design"),
    ("Systems & Architecture", r"systems engineer|system architect|solutions architect|enterprise architect|systems integration|technical architect|system design"),
    ("Database Administration", r"\bdba\b|database admin|sql server|oracle db|postgres|mysql|database management"),
    ("Finance & Accounting", r"accountant|accounting|financial report|\bgaap\b|\bifrs\b|audit|reconciliation|month-end|general ledger|controller|\bcpa\b|\bfp&a\b|financial planning"),
    ("Banking & Lending", r"credit analy|underwrit|loan (officer|origination|portfolio|servicing)|lending|mortgage|commercial bank|retail bank|treasury|wealth manag"),
    ("Investment & Trading", r"investment bank|equity research|trading|portfolio|hedge fund|asset manag|private equity|venture capital|\bm&a\b"),
    ("Risk & Compliance", r"risk manag|compliance (team|program|officer|requirements|framework)|regulatory (compliance|requirements|reporting)|\baml\b|\bkyc\b|\bsox\b|internal control|audit readiness"),
    ("Legal", r"attorney|lawyer|general counsel|legal counsel|paralegal|legal (team|department|operations|systems|review)|litigation|contract review|intellectual property"),
    ("Sales", r"sales (engineer|executive|manager|team|cycle|rep|development|quota|target)|account executive|business development|\bbdr\b|\bsdr\b|quota|pipeline generation|closing deals|pre-?sales"),
    ("Customer Success & Support", r"customer success|customer support|customer service|client success|help desk|technical support|account manag|client relationship"),
    ("Marketing", r"marketing (team|manager|campaign|strategy|analytics|automation)|\bseo\b|\bsem\b|content strategy|brand (manag|strategy)|demand generation|social media|growth marketing|go-to-market"),
    ("Communications & PR", r"public relations|communications|press|media relations|copywrit|editorial"),
    ("Human Resources", r"human resources|\bhr\b|recruiter|recruiting team|talent acquisition|people operations|employee onboarding|benefits admin|hris|payroll"),
    ("Operations", r"operations (manag|lead|team|analyst|coordinator|engineer)|business operations|process improvement|operational excellence|\bkpis?\b"),
    ("Supply Chain & Logistics", r"supply chain|logistics|procurement|strategic sourcing|inventory manag|warehouse|fulfillment center|distribution center"),
    ("Manufacturing Operations", r"manufactur|production line|assembly|plant|\bcnc\b|machin(e|ing) operator|shop floor|lean manufacturing|six sigma"),
    ("Field Service", r"field service|field engineer|on-?site service|customer site|commissioning|service engineer"),
    ("Maintenance & Repair", r"maintenance (technician|mechanic|engineer|team|work|schedule)|preventive maintenance|equipment repair|field technician|service technician|\bhvac\b|electrician|mechanic"),
    ("Quality Control", r"quality control|\bqc\b|inspection|\biso\s*9|quality management|\bcapa\b|nonconformance|product safety|safety engineer|\bul\b|\bce\b mark"),
    ("Mechanical Engineering", r"mechanical engineer|\bcad\b|solidworks|thermal|hvac design|mechanical design|\bfea\b"),
    ("Electrical Engineering", r"electrical engineer|circuit|\bpcb\b|power systems|embedded|firmware|\bfpga\b|signal"),
    ("Civil & Structural Engineering", r"civil engineer|structural|surveyor|surveying|geotechnical|construction engineer|autocad civil|site develop"),
    ("Chemical & Process Engineering", r"chemical engineer|process engineer|chemical process|refin|polymer"),
    ("Aerospace & Defense", r"aerospace|aircraft|avionics|spacecraft|satellite|defense (contractor|industry|sector)|\bdod\b"),
    ("Hardware & Semiconductors", r"semiconductor|silicon|\basic\b|chip design|\brtl\b|verilog|post[- ]silicon|validation engineer|hardware engineer"),
    ("Construction & Facilities", r"construction (project|manag|site|worker|engineer)|facilities (manag|maint|engineer)|general contractor|\bosha\b|jobsite|job site"),
    ("Healthcare & Nursing", r"\brn\b|nurse|nursing|patient care|clinical|hospital|\bemr\b|\behr\b|bedside"),
    ("Medical & Physicians", r"physician|\bmd\b|surgeon|medical doctor|residency|attending"),
    ("Pharmacy & Life Sciences", r"pharmac|biotech|laboratory|assay|clinical trial|\bgmp\b|\bfda\b|biolog|life science"),
    ("Education & Training", r"teacher|teaching|faculty|professor|instructor|curriculum|training (program|develop|specialist|manager)|tutor|lecturer|learning and development"),
    ("Research", r"research|\bphd\b|publication|experiment|hypothesis|principal investigator"),
    ("Consulting & Strategy", r"consultant|consulting|strategy (team|lead|manager|role)|corporate strategy|advisory|client engagement|business case"),
    ("Administration", r"administrative|executive assistant|office manag|scheduling|clerical|receptionist|data entry"),
    ("Agriculture & Food", r"agricultur|farm|crop|harvest|livestock|food process|food safety"),
    ("Hospitality & Retail", r"hospitality|hotel|restaurant|retail store|store manag|store associate|guest service|front desk|cashier"),
    ("Transportation & Driving", r"truck driver|delivery driver|\bcdl\b|trucking|fleet manag|dispatcher|transportation (manag|coordinator|planner)"),
    ("Energy & Utilities", r"energy (sector|industry|company|storage|systems|market)|utility company|utilities|solar|wind (farm|turbine|energy)|battery (storage|systems|cell)|power grid|oil and gas|renewable"),
    ("Insurance", r"insurance (carrier|claims|industry|product|polic)|claims adjust|actuar|policyholder|\bp&c\b|reinsurance"),
    ("Real Estate", r"real estate|property manag|leasing|tenant|broker"),
)
# Word-start guard only: many patterns are stems ("product manag", "manufactur") meant to match
# "Product Manager" and "manufacturing", so the match may run on into the rest of the word.
_COMPILED = tuple((tag, re.compile(r"(?<![a-z])(?:" + pat + r")", re.I)) for tag, pat in TAGS)

TITLE_WEIGHT = 5
MAX_TAGS = 9
# Functions whose words are everyday English in any ad ("operations", "research", "training"):
# a body-only hit needs three mentions. Specific ones (Data Engineering, Cybersecurity) need two.
COMMON = frozenset({"Operations", "Research", "Education & Training", "Consulting & Strategy",
                    "Customer Success & Support", "Marketing", "Sales", "Risk & Compliance",
                    "Human Resources", "Legal", "Administration", "Construction & Facilities",
                    "Energy & Utilities", "Project & Program Management", "Communications & PR"})


def job_tags(title: str, description: str = "", limit: int = MAX_TAGS) -> list[str]:
    """Ranked function tags for one job. A title hit alone qualifies; a body-only tag needs two
    mentions. Capped at `limit` (Migrate Mate shows about nine)."""
    title = title or ""
    body = description or ""
    scored = []
    for order, (tag, pat) in enumerate(_COMPILED):
        in_title = len(pat.findall(title))
        in_body = len(pat.findall(body))
        need = 3 if tag in COMMON else 2
        if not in_title and in_body < need:
            continue
        scored.append((-(in_title * TITLE_WEIGHT + min(in_body, 20)), order, tag))
    scored.sort()
    return [t for _, _, t in scored[:limit]]
