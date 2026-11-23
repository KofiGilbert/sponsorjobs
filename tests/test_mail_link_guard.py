"""The apply agent and the person's mail: a link is followed only on the employer's own site.

* ``detect.link_on_site``: an emailed link counts only on the site the run is applying on (its
  registrable domain; the very host on shared hosting) or on the same hiring system;
* ``detect.sender_on_site``: a message is the site's own by its sender ADDRESS's domain, never
  by a display name that looks like an address;
* ``_await_match`` (ui/app.py): the company name in the subject or From name is enough to read
  a one-time code, never to hand the agent a link;
* the run's navigation fence never widens to a whole shared-hosting domain or a registry
  suffix (submit/policy.py);
* the shell's own check, under node (tests/js/test_mail_link_guard.js).

Everyone and every site here is made up.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

from inbox import detect
from submit import policy

ROOT = Path(__file__).resolve().parent.parent
SITE = "careers.brightwater.example"
WD = "brightwater.wd5.myworkdayjobs.com"


# --------------------------------------------------------------------------- link_on_site
@pytest.mark.parametrize("link,host", [
    ("https://careers.brightwater.example/activate?t=1", SITE),
    ("https://login.brightwater.example/verify?t=1", SITE),
    ("https://www.brightwater.example/verify", SITE),
    ("https://id.brightwater.co.uk/verify", "careers.brightwater.co.uk"),
    ("https://brightwater.wd5.myworkdayjobs.com/en-US/careers/activate/abc", WD),
    ("https://wd5.myworkday.com/brightwater/activate/abc", WD),
    ("https://career41.sapsf.com/verify?x=1", "brightwater.successfactors.com"),
    ("https://my.greenhouse.io/users/confirmation?t=1", "job-boards.greenhouse.io"),
    ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/verify", "eeho.fa.us2.oraclecloud.com"),
    ("https://brightwater.herokuapp.com/verify", "brightwater.herokuapp.com"),
])
def test_a_link_on_the_site_or_its_hiring_system_passes(link, host):
    assert detect.link_on_site(link, host) is True


@pytest.mark.parametrize("link,host", [
    ("https://brightwater-careers.example-other.com/verify", SITE),            # lookalike name
    ("https://careers.brightwater.example.other-site.net/verify", SITE),       # site as a subdomain
    ("https://myworkdayjobs.com.other-site.net/activate", WD),                  # ATS as a subdomain
    ("https://workday.com.other-site.net/activate", WD),
    ("https://careers.brightwater.example@other-site.net/verify", SITE),       # userinfo
    ("https://user:pw@careers.brightwater.example/verify", SITE),
    ("https://other-site.net\\@careers.brightwater.example/verify", SITE),    # parsers disagree
    ("https://other-site.net/verify?next=https://careers.brightwater.example", SITE),
    ("javascript:alert(1)", SITE),
    ("ftp://careers.brightwater.example/verify", SITE),
    ("", SITE),
    ("https://careers.brightwater.example/verify", ""),
    ("https://jobs.lever.co/other/verify", WD),                                 # another hiring system
    ("https://objectstorage.us-ashburn-1.oraclecloud.com/n/x", "eeho.fa.us2.oraclecloud.com"),
    ("https://other.herokuapp.com/verify", "brightwater.herokuapp.com"),        # shared hosting sibling
    ("https://other.co.uk/verify", "careers.brightwater.co.uk"),
])
def test_lookalike_and_unrelated_links_are_refused(link, host):
    assert detect.link_on_site(link, host) is False


# --------------------------------------------------------------------------- the sender
def test_the_sender_is_its_address_never_a_display_name():
    spoof = {"from": '"hr@brightwater.example" <notice@other-site.net>'}
    assert detect.sender_domain(spoof) == "other-site.net"
    assert detect.sender_on_site(spoof, SITE) is False
    assert detect.sender_domain({"from": "Brightwater <jobs@mail.brightwater.example>"}) == "mail.brightwater.example"
    assert detect.sender_domain({"from": "jobs@brightwater.example"}) == "brightwater.example"


def test_sender_on_site_by_domain_or_hiring_system():
    assert detect.sender_on_site({"from": "Jobs <noreply@mail.brightwater.example>"}, SITE)
    assert detect.sender_on_site({"from": "Brightwater <brightwater@myworkday.com>"}, WD)
    assert not detect.sender_on_site({"from": "Brightwater <jobs@other-site.net>"}, SITE)
    assert not detect.sender_on_site({"from": "Brightwater <brightwater@myworkday.com>"}, SITE)


def test_the_company_name_alone_still_matches_for_display():
    msg = {"from": "Notices <no-reply@notify.other-site.net>", "subject": "Your Brightwater application"}
    assert detect.about_site(msg, SITE, company="Brightwater") is True
    assert detect.sender_on_site(msg, SITE) is False


# --------------------------------------------------------------------------- what the run is handed
@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    import ui.app as mod
    importlib.reload(mod)
    return mod


def _verify(frm, link, subject="Please verify your email"):
    return {"from": frm, "subject": subject, "body": f"Confirm your account: {link}", "id": "m1"}


def test_a_link_needs_the_senders_domain_not_the_company_name(A):
    msg = _verify("Brightwater Careers <no-reply@other-site.net>",
                  "https://other-site.net/verify?t=1", subject="Brightwater: verify your email")
    assert A._await_match([msg], "any", SITE, "Brightwater") is None
    # even a link that looks like the site's own: the sender must be the site's
    msg2 = _verify("Brightwater Careers <no-reply@other-site.net>",
                   "https://careers.brightwater.example/verify?t=1", subject="Brightwater: verify your email")
    assert A._await_match([msg2], "any", SITE, "Brightwater") is None


def test_a_link_from_the_site_to_elsewhere_is_never_handed_over(A):
    msg = _verify("Brightwater <no-reply@brightwater.example>", "https://other-site.net/verify?t=1")
    assert A._await_match([msg], "any", SITE, "Brightwater") is None
    reset = {"from": "Brightwater <no-reply@brightwater.example>", "subject": "Reset your password",
             "body": "Reset it here: https://other-site.net/reset?token=1", "id": "m2"}
    assert A._await_match([reset], "any", SITE, "Brightwater") is None


def test_the_sites_own_link_is_handed_over(A):
    good = _verify("Brightwater <no-reply@brightwater.example>", "https://careers.brightwater.example/verify?t=1")
    bad = _verify("Brightwater <no-reply@other-site.net>", "https://other-site.net/verify?t=2",
                  subject="Brightwater: verify")
    hit = A._await_match([bad, good], "any", SITE, "Brightwater")
    assert hit and hit["kind"] == "verify" and hit["link"] == "https://careers.brightwater.example/verify?t=1"
    wd = _verify("Brightwater <brightwater@myworkday.com>", "https://brightwater.wd5.myworkdayjobs.com/en-US/c/activate/x")
    assert A._await_match([wd], "any", WD, "Brightwater")["link"].endswith("/activate/x")


def test_a_code_may_still_match_by_the_company_name(A):
    code = {"from": "Notices <no-reply@notify.other-site.net>", "subject": "Your Brightwater verification code",
            "body": "Enter this code to verify your email: 482913", "id": "m3"}
    hit = A._await_match([code], "any", SITE, "Brightwater")
    assert hit and hit["kind"] == "code" and hit["code"] == "482913"


# --------------------------------------------------------------------------- the fence
def test_the_fence_never_widens_to_a_shared_domain_or_a_registry_suffix():
    hosts = policy.run_allowed_hosts("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1")
    assert "eeho.fa.us2.oraclecloud.com" in hosts and "oraclecloud.com" not in hosts
    assert "herokuapp.com" not in policy.run_allowed_hosts("https://brightwater.herokuapp.com/jobs/1")
    assert policy.registrable_domain("careers.brightwater.com.pl") == "brightwater.com.pl"
    assert "com.pl" not in policy.run_allowed_hosts("https://careers.brightwater.com.pl/job/1")
    assert "brightwater.example" in policy.run_allowed_hosts("https://careers.brightwater.example/job/1")


def test_the_shell_mirrors_the_lists():
    js = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    for h in policy.SHARED_HOSTING:
        assert f'"{h}"' in js, h
    for group in detect._ATS_LINK_GROUPS:
        for h in group:
            assert f'"{h}"' in js, h


def test_the_shell_under_node():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_mail_link_guard.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
