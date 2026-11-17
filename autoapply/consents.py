"""Two kinds of form question the agent answers by a standing rule, never by asking each time.

* **Marketing and communication opt-ins** ("By selecting Yes, you agree to receive mobile
  messages from us in relation to this job application ... SMS Policy", "I agree to receive
  newsletters", "Text me updates", "Subscribe to job alerts"). The answer is the page's NO /
  decline choice, unless the person saved a standing yes (``@marketing_optin``, default no). An
  optional box stays unticked. A REQUIRED question whose only choice is yes is a forced consent:
  it stays the person's, unless they saved a standing yes to those (``@forced_consent``, Apply
  setup, default ask).
* **Acknowledgements of a notice or condition** ("If you are selected ... a background check
  and/or drug screening as a condition of employment", "would you be willing to ... complete any
  background check and/or drug screening required for this position", "GDPR Acknowledgement",
  a privacy notice acknowledgement, at-will employment, "I certify the information is true").
  The same standing consent that accepts the terms a site requires (``accept_required_terms``
  "always", the memory page's "What I may do for you") answers them with the page's affirmative
  choice (Yes, I acknowledge, I agree). Without that consent they are asked as before. A question
  that asks the person to DISCLOSE a fact (a criminal record, an existing non-compete, a
  termination) is never an acknowledgement: it is theirs.

Category rules on the question's words, never one site's markup (Phoenix Contact on Greenhouse,
2026-11-17, asked about both kinds after 15 fields were filled).
"""
from __future__ import annotations

import re

from autoapply import workauth

OPTIN_KEY = "@marketing_optin"
# (2026-11-17, Apply setup) A REQUIRED opt-in whose only choice is yes: asked (the default) unless
# the person saved a standing Yes under this key.
FORCED_KEY = "@forced_consent"

# ---------------------------------------------------------------------------- opt-ins
_CHANNEL = (r"(text|sms|mobile|messages?|newsletters?|marketing|promotional|promotions?|updates?|alerts?|news"
            r"|communications?|e-?mails?|offers?)")
_OPTIN = re.compile(
    r"\b(agree|consent|opt[- ]?in|sign me up|subscribe|like|want|wish|would you like|okay|ok)\b[^.?]{0,60}?"
    r"\b(receiv\w*|get|send( me)?|be sent|be contacted|contact me)\b[^.?]{0,40}?\b" + _CHANNEL + r"\b"
    r"|\bnewsletters?\b|\bmarketing (e-?mails?|messages?|communications?|texts?|materials?)\b|\bjob alerts?\b"
    r"|\btalent (community|network|pool)\b|\btext me\b|\bsms (policy|messages?|updates?|notifications?|alerts?)\b"
    r"|\bsubscribe\b|\bopt[- ]?in to (receive|text|sms|marketing|communications?|e-?mails?)\b"
    r"|\b(texts?|text messages?|sms) (about|regarding|for) (my |this |your )?(application|candidacy|interview)"
    r"|\b(can|may) we (text|sms) you\b|\b(can|may) we send you (texts?|text messages?|sms|marketing|newsletters?)\b", re.I)
# A question that only mentions a channel but asks something else ("Phone number (we may text you)",
# "Preferred method of contact").
_NOT_OPTIN = re.compile(r"\b(phone|mobile|cell) (number|no\.?)\b|preferred (method|way|mode) of contact"
                        r"|e-?mail address|^\W*(e-?mail|phone)\W*$", re.I)

_YES_OPT = re.compile(r"^\W*(yes\b|y\W*$|i agree|agree\b|i consent|consent\b|opt[- ]?in|subscribe|sign me up|i accept"
                      r"|accept\b|i would like|i'd like|please (text|send|contact|subscribe))", re.I)
_NO_OPT = re.compile(r"^\W*(no\b|n\W*$|i do not|i don'?t|do not|don'?t|not (now|at this time|interested)|opt[- ]?out"
                     r"|decline|i decline|unsubscribe|i disagree|disagree|no,? thanks?)", re.I)


def is_optin(label: str) -> bool:
    """True for a marketing or communication opt-in question."""
    lbl = " ".join(str(label or "").split())
    return bool(lbl) and bool(_OPTIN.search(lbl)) and not _NOT_OPTIN.search(lbl)


def _opt_polarity(option: str):
    o = str(option or "")
    if _NO_OPT.search(o):
        return False
    if _YES_OPT.search(o):
        return True
    return workauth.polarity(o)


def optin_choice(want: bool, options) -> str | None:
    """The option that says ``want`` (yes, opt in / no, decline); None when none or several do."""
    cands = [str(o) for o in (options or []) if str(o).strip() and _opt_polarity(o) is want]
    if len(cands) == 1:
        return cands[0]
    plain = [o for o in cands if re.fullmatch(r"\W*(yes|no)\W*", o, re.I)]
    return plain[0] if len(plain) == 1 else None


def _saved_yes(library, key: str) -> bool:
    if library is None:
        return False
    try:
        row = library.best(key) if hasattr(library, "best") else None
    except Exception:          # noqa: BLE001 - nothing saved then
        row = None
    return workauth.polarity(str((row or {}).get("answer") or "")) is True


def saved_optin(library) -> bool:
    """The person's standing choice on marketing messages: True only when they saved a yes
    (``@marketing_optin``); no saved answer is a no."""
    return _saved_yes(library, OPTIN_KEY)


def saved_forced(library) -> bool:
    """The person's standing choice on a required consent with no way to decline: True only when
    they saved a yes (``@forced_consent``); no saved answer asks them each time."""
    return _saved_yes(library, FORCED_KEY)


def saved_forced_decline(library) -> bool:
    """(2026-11-17) The person said no once to a required consent with no way to decline
    (``@forced_consent`` saved as No): such a question is left EMPTY and flagged from then on,
    never asked again. No saved answer still asks; a saved yes accepts (saved_forced)."""
    if library is None:
        return False
    try:
        row = library.best(FORCED_KEY) if hasattr(library, "best") else None
    except Exception:          # noqa: BLE001 - nothing saved then
        row = None
    return workauth.polarity(str((row or {}).get("answer") or "")) is False


def is_forced(label: str, options, field_type: str = "select") -> bool:
    """A REQUIRED opt-in with no way to decline (its only choice is yes): the person's."""
    lbl = " ".join(str(label or "").split())
    return bool(lbl) and is_optin(lbl) and not is_acknowledgement(lbl) \
        and optin_answer(lbl, options, field_type, True, opt_in=False) is PERSON


# The verdicts of optin_answer: a value to pick or tick, nothing to do (an optional box left
# unticked), or the person's (a forced consent).
LEAVE, PERSON = "", None


def optin_answer(label: str, options, field_type: str = "select", required: bool = False,
                 *, opt_in: bool = False):
    """For an opt-in question: the choice to make ("No", "I do not agree"... or the yes when
    ``opt_in``), LEAVE ("" - nothing to do: an optional box stays unticked), or PERSON (None - a
    required question with no way to decline). ``label`` may be a checkbox group's "Question:
    Option" line; then the answer is "Yes" to tick THIS box, or LEAVE."""
    t = str(field_type or "select").lower()
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if t == "checkbox":
        q, sep, opt = str(label or "").partition(": ")
        if sep and opt.strip() and is_optin(q):
            # one box of a group ("Can we text you?: No"): tick the box that says the choice
            return "Yes" if _opt_polarity(opt) is opt_in else LEAVE
        if opt_in:
            return "Yes"
        return PERSON if required else LEAVE
    if opts or t in ("select", "radio"):
        v = optin_choice(opt_in, opts)
        if v:
            return v
        if opt_in:
            return PERSON if required else LEAVE
        return PERSON if required else LEAVE
    return PERSON if required else LEAVE                 # a text box: nothing to type


# ---------------------------------------------------------------------------- acknowledgements
_SCREEN = r"(background (check|screen\w*|investigation)|drug (screen\w*|test\w*)|pre-?employment screen\w*)"
_ACK = re.compile(
    # a condition of employment the company gives notice of, or asks willingness to complete
    _SCREEN + r"[^?]{0,200}?\b(condition of (employment|hire|the offer)|contingent|required for (this|the) (position|role|job))"
    r"|\b(if|once|when) (you are|you're) (selected|hired|offered)\b[^?]{0,200}?" + _SCREEN
    + r"|\b(if|once|when) an offer is (extended|made|accepted)\b[^?]{0,200}?" + _SCREEN
    + r"|\b(willing|agree|consent) to (participate in|undergo|complete|submit to|take|pass)\b[^?]{0,80}?" + _SCREEN
    + r"|\bconsent to (a |the |any )?" + _SCREEN
    + r"|" + _SCREEN + r"[^?]{0,120}?\b(acknowledg\w*|understand|i agree|advance notice)"
    # a notice the person is asked to acknowledge
    r"|\bgdpr\b|\b(privacy|data protection|candidate privacy|applicant privacy) (notice|policy|statement)\b[^?]{0,120}?"
    r"\b(acknowledg\w*|read|understand|reviewed|agree)"
    r"|\b(acknowledg\w*|read|understand|reviewed)\b[^?]{0,120}?\b(privacy|data protection) (notice|policy|statement)"
    r"|\bat[- ]will\b[^?]{0,160}?\b(acknowledg\w*|understand|agree)|\b(acknowledg\w*|understand|agree)\b[^?]{0,160}?\bat[- ]will\b"
    r"|\be-?verify\b[^?]{0,120}?\b(acknowledg\w*|understand|notice)"
    # an attestation that the application is true
    r"|\b(certify|attest|affirm|declare)\b[^?]{0,160}?\b(true|accurate|complete|correct|truthful)\b"
    r"|\binformation (i have |i've )?(provided|given|submitted)[^?]{0,80}?\b(true|accurate|complete|correct)\b"
    r"|^\W*(\w+ )?acknowledge?ment\W*$|\backnowledge?ment of\b", re.I)
# A question that asks the person to DISCLOSE something: a fact, never an acknowledgement.
_DISCLOSE = re.compile(
    r"convict|criminal|felon|misdemeanor|arrest|pending charge|plead|guilty|offen[cs]e"
    r"|non-?compet|restrictive covenant|non-?solicit|confidentiality agreement"
    r"|have you (ever )?been\b|have you ever\b|do you (currently )?have (any|a|an)\b|are you (currently )?(bound|subject|party)"
    r"|terminat|dismiss|fired|discharg|resign(ed)? in lieu|disciplin"
    r"|licen[cs]e|sponsor|visa|citizen|authori[sz]ed to work|relocat|salary|compensation|clearance"
    r"|failed (a |any )?(drug|background)|tested positive|refused (a |any )?(drug|background)", re.I)

_AFFIRM = re.compile(r"^\W*(yes\b|y\W*$|i (acknowledge|agree|understand|accept|certify|attest|consent|confirm|have read|will)\b"
                     r"|acknowledged?\b|agreed?\b|accept(ed)?\b|understood\b|confirm(ed)?\b|true\W*$)", re.I)
_DENY = re.compile(r"\b(no|not|don'?t|do not|decline|disagree|refuse|won'?t|will not|unwilling)\b", re.I)


def is_acknowledgement(label: str) -> bool:
    """True for a question that only asks the person to acknowledge a notice or a condition of
    employment, or to attest the application is true (never one that asks them to disclose)."""
    lbl = " ".join(str(label or "").split())
    if not lbl or _DISCLOSE.search(lbl) or workauth.classify(lbl):
        return False
    return bool(_ACK.search(lbl))


def affirm_option(options) -> str | None:
    """The page's affirmative acknowledge choice ("Yes", "I acknowledge", "Yes, I agree"), or None."""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    cands = [o for o in opts if _AFFIRM.search(o) and not _DENY.search(re.sub(r"^\W*yes\b", "", o, flags=re.I))]
    if len(cands) == 1:
        return cands[0]
    plain = [o for o in cands if re.fullmatch(r"\W*(yes|i acknowledge|i agree|acknowledged?)\W*", o, re.I)]
    return plain[0] if plain else (cands[0] if cands else None)


def acknowledge_answer(label: str, options, field_type: str = "select") -> str | None:
    """The affirmative choice for an acknowledgement (the caller checks the person's consent),
    "Yes" to tick a lone box, else None (theirs: a text box, or no choice that says yes)."""
    t = str(field_type or "select").lower()
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if t == "checkbox":
        q, sep, opt = str(label or "").partition(": ")
        if sep and opt.strip():
            return "Yes" if affirm_option([opt]) else None
        return "Yes"
    if opts or t in ("select", "radio"):
        return affirm_option(opts)
    return None


# ---------------------------------------------------------------------------- the rule
PICK, NOTHING, THEIRS = "pick", "leave", "person"


def standing_answer(label: str, options, field_type: str = "select", required: bool = False, *,
                    library=None, accept_notices: bool = False):
    """What the standing rules say for one field, or None when neither rule applies (or an
    acknowledgement without the person's consent: asked as before):

    * (PICK, value, origin): pick or tick ``value``; origin ``optin`` (declined marketing),
      ``library`` (the person's saved yes) or ``consent`` (an acknowledgement under their
      standing consent to required notices);
    * (NOTHING, "", ""): an optional opt-in left as it is (unticked, never typed in);
    * (THEIRS, "", ""): a forced consent (required, no way to decline): the person's, unless
      they saved a standing yes to those (``@forced_consent``): then (PICK, the yes, "consent")."""
    lbl = " ".join(str(label or "").split())
    t = str(field_type or "select").lower()
    q = lbl.partition(": ")[0] if t == "checkbox" and ": " in lbl else lbl
    if is_optin(q) and is_acknowledgement(q):
        return None                                    # one box for both: the person's own read
    if is_optin(q):
        yes = saved_optin(library)
        v = optin_answer(lbl, options, t, required, opt_in=yes)
        if v is PERSON and saved_forced(library):
            # a forced consent, and the person said once to accept those: the page's yes
            forced = optin_answer(lbl, options, t, required, opt_in=True)
            if forced:
                return PICK, forced, "consent"
        if v is PERSON:
            return THEIRS, "", ""
        if v == LEAVE:
            return NOTHING, "", ""
        return PICK, v, ("library" if yes else "optin")
    if accept_notices and is_acknowledgement(q):
        v = acknowledge_answer(lbl, options, t)
        if v:
            return PICK, v, "consent"
        if t == "checkbox" and lbl != q:
            return NOTHING, "", ""                     # another box of the group (its "No")
    return None
