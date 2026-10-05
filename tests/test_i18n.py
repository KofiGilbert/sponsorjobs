"""i18n locale files stay in sync: every language defines exactly the same keys as English,
with non-empty values. Catches a translation that drifts, adds, or drops a key."""

import json
from pathlib import Path

LOCALES = Path(__file__).resolve().parents[1] / "ui" / "static" / "locales"


def _load(code):
    return json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))


def test_all_locales_match_english_keys():
    en = _load("en")
    assert en, "en.json is empty"
    for path in sorted(LOCALES.glob("*.json")):
        code = path.stem
        d = _load(code)
        missing = set(en) - set(d)
        extra = set(d) - set(en)
        assert not missing, f"{code}.json is missing keys: {sorted(missing)}"
        assert not extra, f"{code}.json has unknown keys: {sorted(extra)}"
        blank = [k for k, v in d.items() if not str(v).strip()]
        assert not blank, f"{code}.json has blank values: {blank}"


def test_expected_languages_present():
    have = {p.stem for p in LOCALES.glob("*.json")}
    assert {"en", "es", "fr", "de", "pt"} <= have, f"missing locale files, have {have}"
