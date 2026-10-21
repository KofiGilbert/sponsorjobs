"""The "Cap-exempt (likely)" rule, on a table of real employer names (2026-10-20).

H-1B cap exemption (8 CFR 214.2(h)(19)(iii)) covers institutions of higher education (20 U.S.C.
1001(a): public or nonprofit), nonprofits related to or affiliated with one, nonprofit research
organizations, and governmental research organizations. A K-12 school district is not one: the
old rule matched "school district" and put a cap-exempt chip on every K-12 district, and a seed
flag set from NAICS 61 (all of education) did the same for "Public Schools".

The Python rule (sourcing/sponsors.py) and its browser-extension port (extension/sponsor_core.js,
copied verbatim to site/) must give the same answer on every name, checked here through node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sourcing.growth import cap_exempt_likely
from sourcing.sponsors import cap_exempt_verdict, looks_cap_exempt, looks_k12

CORE = Path("extension/sponsor_core.js").resolve()

# (posting / employer name, cap-exempt by name?)
NAMES = [
    # Higher education: public and nonprofit universities and colleges, community colleges.
    ("University of Michigan", True),
    ("Regents of the University of California", True),
    ("Board of Trustees of the Leland Stanford Junior University", True),
    ("President and Fellows of Harvard College", True),
    ("Massachusetts Institute of Technology", True),
    ("Massachusetts Inst Of Technology", True),
    ("Rensselaer Polytechnic Institute", True),
    ("Williams College", True),
    ("Miami Dade College", True),
    ("Houston Community College", True),
    ("Los Angeles Community College District", True),
    ("The Trustees of Columbia University in the City of New York", True),
    ("Texas A&M University System", True),
    ("UT Health Science Center", True),
    ("Stanford School of Medicine", True),
    ("Princeton Theological Seminary", True),
    # Affiliated hospitals and medical centers.
    ("Johns Hopkins Hospital, Inc.", True),
    ("Boston Children's Hospital", True),
    ("Cedars-Sinai Medical Center", True),
    ("University of Texas MD Anderson Cancer Center", True),
    ("Mayo Clinic", True),
    ("Cleveland Clinic Foundation", True),
    # Nonprofit and government research organizations.
    ("Argonne National Laboratory", True),
    ("Lawrence Berkeley National Lab", True),
    ("Scripps Research Institute", True),
    ("Broad Institute", True),
    ("Cold Spring Harbor Laboratory", True),
    ("Research Foundation of SUNY", True),
    ("Fred Hutchinson Cancer Center", True),
    ("Howard Hughes Medical Institute", True),
    ("Battelle Memorial Institute", True),
    # K-12 school districts and schools: NOT cap-exempt.
    ("Houston Independent School District", False),
    ("Aldine ISD", False),
    ("Los Angeles Unified School District", False),
    ("Baltimore City Public Schools", False),
    ("Anne Arundel County Public Schools", False),
    ("Chicago Board of Education", False),
    ("New York City Department of Education", False),
    ("Savannah Chatham County Public Sch", False),
    ("Richland County School Dist One", False),
    ("Success Academy Charter Schools", False),
    ("KIPP Texas Public Schools", False),
    ("Harmony Public Schools", False),
    ("Prince George's County Public Schools", False),
    ("Milwaukee Board of School Directors", False),
    # Look-alikes: not higher education, not nonprofit research.
    ("University Federal Credit Union", False),
    ("College Pharmacy", False),
    ("College Hunks Hauling Junk", False),
    ("Universal Studios", False),
    ("Universal Health Services", False),
    ("Collegiate Prep Tutors Co", False),
    ("College Park Industries", False),
    ("University Park Mall", False),
    ("The College Board", False),
    ("University of Phoenix", False),
    ("DeVry University", False),
    ("Acme Research Inc", False),
    ("Gartner Research Corp", False),
    ("IQVIA Research Laboratories LLC", False),
    ("HCA Houston Healthcare Hospital LLC", False),
    ("Hospitality Partners LLC", False),
    ("Medical Devices Inc", False),
    ("Abbott Laboratories", False),
    ("Labcorp", False),
    ("Google", False),
    ("Microsoft Corporation", False),
    ("", False),
]


def test_the_table_is_big_enough_and_has_both_answers():
    assert len(NAMES) >= 40
    assert {want for _, want in NAMES} == {True, False}


@pytest.mark.parametrize("name,want", NAMES)
def test_python_name_rule(name, want):
    assert looks_cap_exempt(name) is want, name


@pytest.mark.parametrize("name", ["Houston Independent School District", "Aldine ISD",
                                  "Baltimore City Public Schools", "Chicago Board of Education",
                                  "Success Academy Charter Schools"])
def test_k12_beats_a_stored_flag_and_naics_61(name):
    """The seed flags every NAICS 61 filer; a K-12 name clears it for the chip and the ranking."""
    assert looks_k12(name) is True
    assert cap_exempt_verdict(name, name, "61", True) is False
    assert cap_exempt_likely({"display_name": name, "naics": "61", "cap_exempt": 1}) is False


def test_stored_flag_is_believed_only_without_a_for_profit_form():
    # Microsoft and Meta carry the seed flag (a research entity among their filing names).
    assert cap_exempt_verdict("Microsoft", "Microsoft Corporation", "51", True) is False
    assert cap_exempt_verdict("Meta", "Meta Platforms, Inc.", "51", True) is False
    assert cap_exempt_verdict("Chegg", "Chegg Inc", "61", True) is False
    # A nonprofit whose name carries no university word keeps it.
    assert cap_exempt_verdict("Weill Cornell Medicine", "Weill Cornell Medicine", "61", True) is True
    assert cap_exempt_verdict("Arup Laboratories", "Arup Laboratories", "62", True) is True
    # No flag and no cap-exempt name: no.
    assert cap_exempt_verdict("Weill Cornell Medicine", "Weill Cornell Medicine", "61", False) is False
    # The posting's own name can still say so.
    assert cap_exempt_verdict("University of Michigan", "University Of Michigan", "61", False) is True


def test_ranking_boost_uses_the_same_rule():
    for name, want in NAMES:
        if not name:
            continue
        assert cap_exempt_likely({"display_name": name, "naics": "54", "cap_exempt": 0}) is want, name


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_python_and_js_agree_on_every_name(tmp_path):
    cases = [{"name": n, "display": d, "naics": naics, "flag": flag}
             for n, _ in NAMES
             for d, naics, flag in ((n, "61", True), (n, "54", False), ("Microsoft Corporation", "51", True))]
    harness = tmp_path / "h.js"
    harness.write_text(
        "const core = require(process.argv[2]);\n"
        "const cases = JSON.parse(require('fs').readFileSync(process.argv[3], 'utf8'));\n"
        "process.stdout.write(JSON.stringify(cases.map((c) => [core.looksCapExempt(c.name), "
        "core.looksK12(c.name), core.capExemptVerdict(c.name, c.display, c.naics, c.flag)])));\n")
    cases_f = tmp_path / "cases.json"
    cases_f.write_text(json.dumps(cases))
    r = subprocess.run(["node", str(harness), str(CORE), str(cases_f)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)
    py = [[looks_cap_exempt(c["name"]), looks_k12(c["name"]),
           cap_exempt_verdict(c["name"], c["display"], c["naics"], c["flag"])] for c in cases]
    diffs = [(c["name"], c["display"], p, j) for c, p, j in zip(cases, py, js) if p != j]
    assert not diffs, diffs
