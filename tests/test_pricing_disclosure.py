"""Plans renew automatically (2026-11-20), so every place that sells one must say so.

Auto-renewal laws (California's, among others) want the price, how often it renews, that it renews
until cancelled and how to cancel shown next to the buy button, with an affirmative consent. This
pins that the app's plans panel, its strings and the website say it, that the button stays off
until the box is ticked, and that the old "nothing renews" promise is gone everywhere."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "ui/static/app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "ui/templates/index.html").read_text(encoding="utf-8")
EN = json.loads((ROOT / "ui/static/locales/en.json").read_text(encoding="utf-8"))
LOCALES = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "ui/static/locales").glob("*.json")}
PRICING = (ROOT / "site/pricing.html").read_text(encoding="utf-8")
TERMS = (ROOT / "site/terms.html").read_text(encoding="utf-8")

NO_RENEW = re.compile(r"nothing (auto-)?renews|no auto-renew|never renews|paid once|pay once|nothing to cancel|"
                      r"does not renew\.\"", re.I)


def _buy_html() -> str:
    """The body of buyHtml(), the function that draws the buy area of a plan card."""
    start = APP_JS.index("function buyHtml(")
    return APP_JS[start:APP_JS.index("function studentHtml(", start)]


def test_the_terms_say_price_interval_renewal_and_how_to_cancel():
    terms = EN["upg.terms"]
    assert "{price}" in terms and "{every}" in terms
    assert "Renews automatically until you cancel" in terms
    assert "Cancel anytime" in terms and "Your plan" in terms          # how: account menu, Your plan
    assert EN["upg.everyMonth"] == "every month"
    for code, d in LOCALES.items():                                     # every language carries both
        assert "{price}" in d["upg.terms"] and "{every}" in d["upg.terms"], code


def test_the_terms_and_a_consent_box_sit_next_to_the_button_which_starts_off():
    body = _buy_html()
    terms_at = body.index("renewalTerms(")
    box_at = body.index("data-consent")
    button_at = body.index("data-subscribe disabled")
    assert terms_at < box_at < button_at                                # terms, box, then the button
    assert 'type="checkbox"' in body
    assert "upg.agree" in body and EN["upg.agree"].startswith("I agree to automatic renewal")


def test_the_button_follows_the_box_and_only_a_ticked_box_buys():
    assert "go.disabled = !consent.checked" in APP_JS
    sub = APP_JS[APP_JS.index("async function subscribe(btn, consent)"):]
    sub = sub[:sub.index("\n  }\n")]
    assert "if (!consent || !consent.checked) return;" in sub
    assert '{ interval: UPG_INTERVAL, consent: true }' in sub
    # Only the interval goes to the server: never a tier, a plan or a price.
    assert "tier:" not in sub and "price" not in sub.split("api(")[1].split(")")[0]


def test_the_account_area_shows_the_renewal_date_and_a_cancel():
    assert 'id="acctMenuPlanRenew"' in INDEX and 'id="acctMenuCancel"' in INDEX
    assert 'id="upgCancel"' in INDEX and 'id="upgResume"' in INDEX
    assert EN["acct.renewsOn"] == "Renews on {date}, cancel anytime"
    assert "/api/billing/cancel" in APP_JS and "/api/billing/resume" in APP_JS


def test_the_website_shows_both_tiers_and_says_it_renews():
    text = re.sub(r"<[^>]+>", " ", PRICING)
    for price in ("$29", "$49", "$12", "$33"):
        assert price in text, price
    assert "Student" in text and "Standard" in text
    assert "renew automatically until you cancel" in text
    assert "Cancel anytime" in text
    assert "Free" in text and "3 tailored resumes" in text


def _row(label: str) -> list[str]:
    """The cells of one comparison-table row on the pricing page, by its row heading."""
    m = re.search(r'<tr><th scope="row">%s</th>(.*?)</tr>' % re.escape(label), PRICING)
    assert m, label
    return [re.sub(r"<[^>]+>", "", c) for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(1))]


def test_the_website_sells_only_the_monthly_plans_and_states_the_free_limits():
    """Owner decision 2026-11-25: Free / Student $29 a month / Standard $49 a month. No every-3-months
    price, no 5-pack, no old pack prices, on the pricing page, the home page or the terms."""
    home = (ROOT / "site/index.html").read_text(encoding="utf-8")
    for name, page in (("pricing.html", PRICING), ("index.html", home), ("terms.html", TERMS)):
        text = re.sub(r"<[^>]+>", " ", page)
        assert not re.search(r"every 3 months|quarter", text, re.I), name
        for gone in ("$69", "$119", "$39", "$24", "$35"):
            assert gone not in page, (name, gone)
        assert not re.search(r"\$9(?!\d)", page), name
    assert _row("Price") == ["$0", "$29 a month", "$49 a month"]
    assert _row("Tailored one-page resumes") == ["3 a month", "40 a month", "60 a month"]
    assert _row("Recorded practice interviews, scored") == ["1 a month", "Unlimited", "Unlimited"]
    assert _row("Live AI interviews, 20 minutes each") == ["No", "3 a month", "3 a month"]
    assert _row("Application tracker") == ["Yes", "Yes", "Yes"]
    text = re.sub(r"<[^>]+>", " ", PRICING)
    assert "1 for $12, 3 for $33" in text
    # The structured data lists exactly the three offers.
    for page in (PRICING, home):
        assert re.findall(r'"@type": "Offer", "name": "([^"]+)", "price": "(\d+)"', page) == [
            ("Free", "0"), ("Student, monthly", "29"), ("Standard, monthly", "49")]


def test_the_plans_panel_sells_monthly_only_and_shows_the_switch_only_when_the_broker_does():
    assert 'id="upgSeg" hidden' in INDEX                               # hidden until an offer needs it
    paint = APP_JS[APP_JS.index("function paintPlans()"):APP_JS.index("function paintMine()")]
    assert 'p.interval === "quarterly"' in paint and "seg.hidden = !quarterlySold" in paint
    assert EN["upg.feat3"] == "3 live interviews of 20 minutes a month"
    assert EN["upg.featApps"] == "{n} tailored applications a month"
    # The number is the plan's own (Student 40, Standard 60), read from the broker's offers.
    assert '.replace("{n}", planPackages(id))' in paint and "p.packages" in APP_JS
    assert "student_monthly: 40" in APP_JS and "standard_monthly: 60" in APP_JS
    assert "upg.feat60" not in APP_JS and "upg.feat60" not in EN
    assert EN["upg.featRecorded"] == "Unlimited recorded practice interviews"
    assert EN["upg.free"] == ("Free keeps every job with sponsorship marks, the tracker, autofill, "
                              "3 tailored resumes a month and 1 recorded practice interview a month.")
    assert EN["upg.free"] in INDEX


def test_the_upgrade_lines_are_the_decided_words_and_carry_no_price():
    """Every upgrade moment opens the ONE plans panel, so no price is repeated in any of them."""
    lines = {
        "upg.freeResumesUsed": "You've used your 3 free tailored resumes this month. Plans include 40 or 60 a month.",
        "upg.recordedUsed": "You've used this month's practice interview. Plans include unlimited recorded practice.",
        "upg.practiseLive": "Practise live with an interviewer",
        "outage.upgrade.btn": "See plans",
    }
    for key, text in lines.items():
        assert EN[key] == text, key
        for code, d in LOCALES.items():
            assert d[key].strip() and "$" not in d[key], (code, key)
    moments = APP_JS[APP_JS.index("// ---- Upgrade moments"):APP_JS.index("// toast(\"Moved\"")]
    assert moments.count("openUpgrade") >= 2 and "$" not in re.sub(r"\$\(|\$\{", "", moments)
    assert "window.__upgradeMoment" in moments and "window.__recordedLimit" in moments
    assert "window.__r1FreeUpsell" in moments and 'ACCT_PLAN !== "free"' in moments
    # The engine uses the same two lines.
    from ui import app as A
    assert A.FREE_RESUMES_USED_LINE == lines["upg.freeResumesUsed"]
    assert A.FREE_RECORDED_USED_LINE == lines["upg.recordedUsed"]


def test_the_terms_page_describes_the_subscription_and_cancelling():
    assert "renews automatically" in TERMS and "Cancel renewal" in TERMS
    assert "24 months" in TERMS                                         # the student window


def test_no_never_renews_promise_is_left():
    places = {"app.js": APP_JS, "index.html": INDEX, "pricing.html": PRICING,
              "index site": (ROOT / "site/index.html").read_text(encoding="utf-8")}
    places.update({f"{c}.json": json.dumps(d, ensure_ascii=False) for c, d in LOCALES.items()})
    for name, text in places.items():
        assert not NO_RENEW.search(text), (name, NO_RENEW.search(text).group(0))
