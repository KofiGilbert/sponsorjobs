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


# --------------------------------------------------------------------- keys the UI asks for
# A key that app.js calls but no locale file defines is invisible: T() quietly returns the
# English fallback, in every language, and nothing fails. That is how the strings in #257 went
# untranslated for so long. This pins it: every key the UI asks for must exist in all five files.
#
# KNOWN_MISSING is a ratchet for the #257 sweep, which lands one screen group at a time. It may
# only ever SHRINK: each batch deletes its own keys from it, and the last batch deletes the set.
# Adding a key here is not an option, a new untranslated key fails the test instead.

import re

APP_JS = Path(__file__).resolve().parents[1] / "ui" / "static" / "app.js"
# Every translator form in app.js: the section aliases (T, MT, trkT, empT, agentT, ...) and the
# underlying I18N.t, each called as (key, English fallback).
_T_CALL = re.compile(r'(?:\b\w*T|I18N\.t)\(\s*(["\'])([A-Za-z0-9_.]+)\1\s*,')

KNOWN_MISSING = {
    # Auto-apply bar and its asks, the build message and the inbox strip: batch 3.
    "aa.act.continue",
    "aa.agent.answer",
    "aa.agent.back",
    "aa.agent.change",
    "aa.agent.changeA",
    "aa.agent.pageGone",
    "aa.agent.qOf",
    "aa.agent.sendAnswer",
    "aa.agent.thanks",
    "aa.agent.yourAnswers",
    "aa.applyAll",
    "aa.batch.see",
    "aa.batch.start",
    "aa.parked.busy",
    "aa.parked.gone",
    "aa.parked.queued",
    "aa.send.start",
    "aa.st.paused",
    "aa.st.readySend",
    "agent.connectFailed",
    "agent.signInFirst",
    "bm.took",
    "bm.working",
    "inbox.draft.body",
    "inbox.draft.gap1",
    "inbox.draft.gaps",
    "inbox.strip.autoMoved",
    "inbox.strip.done",
    "inbox.strip.draftReply",
    "inbox.strip.ok",
    "inbox.strip.when",
}


def _keys_used_in_app_js():
    return {m.group(2) for m in _T_CALL.finditer(APP_JS.read_text(encoding="utf-8"))}


def test_every_key_the_ui_asks_for_exists_in_every_language():
    used = _keys_used_in_app_js()
    assert used, "no translator calls found, the pattern moved"
    for path in sorted(LOCALES.glob("*.json")):
        have = set(_load(path.stem))
        missing = used - have - KNOWN_MISSING
        assert not missing, (
            f"{path.stem}.json never defines {len(missing)} key(s) app.js asks for, so they show "
            f"in English there: {sorted(missing)}"
        )


def test_the_known_missing_ratchet_only_shrinks():
    """Every entry must still be a real gap. A fixed key has to leave the set, not linger."""
    used = _keys_used_in_app_js()
    en = set(_load("en"))
    stale = {k for k in KNOWN_MISSING if k in en}
    assert not stale, f"these are translated now, delete them from KNOWN_MISSING: {sorted(stale)}"
    gone = {k for k in KNOWN_MISSING if k not in used}
    assert not gone, f"these keys are no longer used by app.js, delete them too: {sorted(gone)}"
