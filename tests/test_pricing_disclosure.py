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
    assert EN["upg.everyMonth"] == "every month" and EN["upg.every3"] == "every 3 months"
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
    for price in ("$29", "$69", "$49", "$119", "$9", "$24", "$39"):
        assert price in text, price
    assert "Student" in text and "Standard" in text
    assert "renew automatically until you cancel" in text
    assert "Cancel anytime" in text
    assert "Free" in text and "3 tailored applications a month" in text


def test_the_terms_page_describes_the_subscription_and_cancelling():
    assert "renews automatically" in TERMS and "Cancel renewal" in TERMS
    assert "24 months" in TERMS                                         # the student window


def test_no_never_renews_promise_is_left():
    places = {"app.js": APP_JS, "index.html": INDEX, "pricing.html": PRICING,
              "index site": (ROOT / "site/index.html").read_text(encoding="utf-8")}
    places.update({f"{c}.json": json.dumps(d, ensure_ascii=False) for c, d in LOCALES.items()})
    for name, text in places.items():
        assert not NO_RENEW.search(text), (name, NO_RENEW.search(text).group(0))
