"""The interview report card: pure, deterministic analysis of a finished practice interview.

Both rounds feed it. Round 1 (the recorded screen) hands over one transcript per question with
the seconds the person actually spoke; Round 2 (the live interview) hands over the dialogue paired
into question/answer items with the seconds taken from the transcript timestamps. From that this
module computes, per answer: words per minute, filler words and their rate, length in seconds, a
STAR check (Situation / Task / Action / Result) and a 0 to 3 "did it answer the question" rating.
The STAR check and the coverage rating come from the model through ONE strict JSON rubric
(``llm.check_star_coverage``); everything else is arithmetic, so the numbers never disagree with
themselves and the offline tests can pin them with fixed transcripts.

Nothing here touches Flask, disk, or the network. The caller stores the result additively next to
the existing overall score (``result["report_card"]``), so older stored results simply have no card.
"""

from __future__ import annotations

import re

REPORT_CARD_VERSION = 1

# The filler words the card counts (phrases first so "you know" is one filler, not two words).
FILLER_WORDS = ("you know", "sort of", "kind of", "um", "uh", "like", "basically", "actually")

# The spoken-pace guideline the delivery summary is judged against (words per minute).
PACE_LOW, PACE_HIGH = 140, 160
# Used to estimate seconds when no measured duration exists (a typed transcript).
_ESTIMATE_WPM = 130

STAR_PARTS = ("situation", "task", "action", "result")

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def count_fillers(text: str) -> dict:
    """How many filler words / phrases the transcript holds, in total and by filler. Case
    insensitive, whole words only ("likely" is not "like"), phrases counted once each."""
    low = (text or "").lower()
    by: dict[str, int] = {}
    for f in FILLER_WORDS:
        pat = r"\b" + r"\s+".join(re.escape(p) for p in f.split()) + r"\b"
        n = len(re.findall(pat, low))
        if n:
            by[f] = n
    return {"total": sum(by.values()), "by_word": by}


def words_per_minute(words: int, seconds: float | None) -> float:
    """Words per minute, 0 when the duration is unknown or zero."""
    if not seconds or seconds <= 0:
        return 0.0
    return round(words / (seconds / 60.0), 1)


def answer_metrics(transcript: str, seconds: float | None = None) -> dict:
    """The arithmetic part of one answer's row: words, seconds (measured when the caller knows
    them, else estimated from the word count and flagged as such), words per minute, fillers,
    and fillers per minute."""
    text = (transcript or "").strip()
    words = _word_count(text)
    measured = seconds is not None and float(seconds) > 0
    secs = round(float(seconds), 1) if measured else round(words / _ESTIMATE_WPM * 60, 1)
    fill = count_fillers(text)
    minutes = secs / 60.0 if secs else 0
    return {
        "words": words,
        "seconds": secs,
        "seconds_estimated": not measured,
        "wpm": words_per_minute(words, secs),
        "fillers": fill["total"],
        "fillers_by_word": fill["by_word"],
        "fillers_per_minute": round(fill["total"] / minutes, 1) if minutes else 0.0,
    }


def pace_label(wpm: float | None) -> str:
    """'slow', 'normal' or 'fast' against the 140 to 160 words-per-minute guideline; 'unknown'
    when there is no pace to judge."""
    if not wpm:
        return "unknown"
    if wpm < PACE_LOW:
        return "slow"
    if wpm > PACE_HIGH:
        return "fast"
    return "normal"


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    return str(v or "").strip().lower() in ("yes", "true", "present", "y", "1")


def parse_star_rubric(data) -> dict:
    """Normalise the model's strict-JSON rubric reply into the card's shape, whatever it sent:
    {"star": {situation, task, action, result} as booleans, "missing": [...], "coverage": 0..3 or
    None, "evidence": one sentence}. Tolerates "yes"/"no", 1/0, a missing block, or a JSON string.
    Anything unreadable yields unknown STAR (all None) and no rating, never an exception."""
    if isinstance(data, str):
        import json
        try:
            data = json.loads(data)
        except ValueError:
            start, end = data.find("{"), data.rfind("}")
            try:
                data = json.loads(data[start:end + 1]) if start != -1 and end != -1 else {}
            except ValueError:
                data = {}
    if not isinstance(data, dict):
        data = {}
    raw_star = data.get("star")
    if not isinstance(raw_star, dict):
        raw_star = {k: data.get(k) for k in STAR_PARTS if k in data}
    star: dict = {}
    for k in STAR_PARTS:
        star[k] = _truthy(raw_star[k]) if k in raw_star else None
    known = all(v is not None for v in star.values())
    missing = [k for k in STAR_PARTS if star[k] is False]
    cov = data.get("coverage", data.get("rating"))
    try:
        rating: int | None = max(0, min(3, int(round(float(cov)))))
    except (TypeError, ValueError):
        rating = None
    evidence = str(data.get("evidence") or "").strip()
    evidence = _SENTENCE_END.split(evidence)[0][:240] if evidence else ""
    return {"star": star, "star_known": known, "missing": missing, "coverage": rating,
            "evidence": evidence}


def _ask_rubric(llm, question: str, transcript: str) -> dict:
    """One strict-JSON rubric call per answer; a missing method or a model failure degrades to an
    unknown STAR and no rating rather than losing the whole card."""
    fn = getattr(llm, "check_star_coverage", None)
    if not callable(fn):
        return parse_star_rubric({})
    try:
        return parse_star_rubric(fn(question, transcript))
    except Exception:
        return parse_star_rubric({})


def weakness_score(row: dict) -> float:
    """How much one answer needs work, higher is weaker. Coverage weighs most (an answer that
    dodged the question is the worst moment whatever its polish), then missing STAR parts, the
    per-answer score, fillers and pace."""
    w = 0.0
    cov = row.get("coverage")
    if cov is not None:
        w += (3 - cov) * 3
    w += 1.5 * len(row.get("missing") or [])
    sc = row.get("score")
    if sc is not None:
        w += (100 - sc) / 20.0
    fpm = row.get("fillers_per_minute") or 0
    if fpm > 8:
        w += 2
    elif fpm > 4:
        w += 1
    wpm = row.get("wpm") or 0
    if wpm and (wpm < 120 or wpm > 180):
        w += 1
    if (row.get("words") or 0) < 40:
        w += 2
    return round(w, 2)


def drill_for(row: dict) -> str:
    """One line on what to practise for this answer, from its single biggest weakness."""
    cov = row.get("coverage")
    missing = row.get("missing") or []
    if (row.get("words") or 0) < 40:
        return "Give a full answer: 60 to 120 seconds with one concrete example."
    if cov is not None and cov <= 1:
        return "Answer the question asked in your first sentence, then add the story."
    if "result" in missing:
        return "End with the measurable result: what changed, and by how much."
    if "action" in missing:
        return "Spend most of the answer on what YOU did, step by step."
    if "situation" in missing or "task" in missing:
        return "Open with one sentence of context: where you were and what you were responsible for."
    fpm = row.get("fillers_per_minute") or 0
    if fpm > 4:
        return "Swap each filler for a silent pause. Record it again and count them."
    wpm = row.get("wpm") or 0
    if wpm > PACE_HIGH + 10:
        return "Slow down to about 150 words a minute. Pause at the end of each sentence."
    if wpm and wpm < PACE_LOW - 10:
        return "Pick up the pace to about 150 words a minute. Cut the preamble."
    if cov == 2:
        return "Cover every part of the question; name the part you skipped this time."
    return "Record it once more and lead with the result."


def strengths_for(rows: list[dict], delivery: dict) -> list[str]:
    """Up to four plain strengths the card can stand behind, from the numbers alone."""
    out: list[str] = []
    full = [r for r in rows if r.get("star_known") and not r.get("missing")]
    if full:
        out.append(f"Full STAR structure on {len(full)} of {len(rows)} answers.")
    on_point = [str(r["i"] + 1) for r in rows if r.get("coverage") == 3]
    if on_point:
        out.append("Answered exactly what was asked on question " + ", ".join(on_point) + ".")
    if delivery.get("pace") == "normal":
        out.append(f"Pace in the {PACE_LOW} to {PACE_HIGH} words-a-minute range.")
    fpm = delivery.get("fillers_per_minute")
    if fpm is not None and fpm < 2 and delivery.get("total_seconds"):
        out.append(f"Few fillers: {fpm} a minute.")
    long_enough = [r for r in rows if 40 <= (r.get("words") or 0)]
    if rows and len(long_enough) == len(rows):
        out.append("Every answer was a full answer, not a one-liner.")
    if not out and rows:
        out.append("Finished every question. The drills below are the fastest way up.")
    return out[:4]


def score_trend(scores: list[int | float]) -> dict:
    """The trend of the overall score across past cards, oldest first: 'up', 'down' or 'flat',
    the change from first to latest, and best/latest."""
    vals = [int(round(float(s))) for s in (scores or []) if s is not None]
    if not vals:
        return {"direction": "none", "delta": 0, "first": None, "latest": None, "best": None,
                "count": 0}
    delta = vals[-1] - vals[0]
    direction = "up" if delta > 2 else "down" if delta < -2 else "flat"
    if len(vals) == 1:
        direction = "none"
    return {"direction": direction, "delta": delta, "first": vals[0], "latest": vals[-1],
            "best": max(vals), "count": len(vals)}


def build_report_card(answers: list[dict], llm=None, overall_score=None, round_no: int = 1) -> dict:
    """The structured card for one finished interview.

    ``answers``: [{q, transcript, competency?, seconds? (measured, or None), per_answer_feedback?
    ({score, feedback})}], in interview order. Answers with no transcript are skipped.
    ``llm``: anything with ``check_star_coverage(question, transcript)``; None means no STAR or
    coverage judgement (the arithmetic still runs).
    Returns {version, round, overall, answers: [rows], delivery, strengths, weakest: [3]}."""
    rows: list[dict] = []
    for i, a in enumerate(answers or []):
        transcript = str(a.get("transcript") or "").strip()
        if not transcript:
            continue
        m = answer_metrics(transcript, a.get("seconds"))
        rub = _ask_rubric(llm, str(a.get("q") or ""), transcript)
        fb = a.get("per_answer_feedback") or {}
        score = fb.get("score") if isinstance(fb, dict) else None
        try:
            score = int(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        rows.append({"i": i, "q": str(a.get("q") or ""),
                     "competency": str(a.get("competency") or ""), "score": score, **m, **rub})
    total_words = sum(r["words"] for r in rows)
    total_seconds = round(sum(r["seconds"] for r in rows), 1)
    total_fillers = sum(r["fillers"] for r in rows)
    avg_wpm = words_per_minute(total_words, total_seconds)
    pace = pace_label(avg_wpm)
    fpm = round(total_fillers / (total_seconds / 60.0), 1) if total_seconds else 0.0
    delivery = {
        "avg_wpm": avg_wpm, "pace": pace,
        "pace_note": {"slow": f"Slower than the {PACE_LOW} to {PACE_HIGH} words-a-minute guideline.",
                      "fast": f"Faster than the {PACE_LOW} to {PACE_HIGH} words-a-minute guideline.",
                      "normal": f"Inside the {PACE_LOW} to {PACE_HIGH} words-a-minute guideline.",
                      "unknown": "No pace to judge yet."}[pace],
        "total_words": total_words, "total_seconds": total_seconds,
        "total_fillers": total_fillers, "fillers_per_minute": fpm,
        "any_estimated": any(r["seconds_estimated"] for r in rows),
        "answers": len(rows),
    }
    for r in rows:
        r["weakness"] = weakness_score(r)
    weakest = sorted(rows, key=lambda r: (-r["weakness"], r["i"]))[:3]
    return {
        "version": REPORT_CARD_VERSION,
        "round": int(round_no),
        "overall": overall_score,
        "answers": rows,
        "delivery": delivery,
        "strengths": strengths_for(rows, delivery),
        "weakest": [{"i": r["i"], "q": r["q"], "competency": r.get("competency", ""),
                     "coverage": r.get("coverage"), "missing": r.get("missing") or [],
                     "weakness": r["weakness"], "drill": drill_for(r)} for r in weakest],
    }
