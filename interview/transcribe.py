"""Local, on-device speech-to-text for interview screens (P2). NEVER cloud.

Uses faster-whisper when it's installed, imported LAZILY so the whole app runs without it, exactly
like the OCR and MemPalace layers self-skip. When the engine is unavailable, ``transcribe()``
returns "" and the caller degrades to the person typing / pasting their own transcript. The model
is a heavy optional dependency, so it lives in the ``[interview]`` extra, not the base install:

    pip install -e ".[interview]"

Set ``RESUME_AGENT_WHISPER_MODEL`` to pick the model size (default ``base``), and
``RESUME_AGENT_DISABLE_STT=1`` to force the typed-transcript path (used by the offline tests).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_MODEL = None
_TRIED = False

# ~130 words per minute is a normal spoken pace; used to estimate speaking time from a transcript.
_WPM = 130
_FILLERS = ("um", "uh", "er", "like", "you know", "basically", "actually", "literally",
            "kind of", "sort of", "i mean", "just", "so yeah")
# Hedging phrases that undercut a confident answer, and the ownership pronouns. Interviewers hear
# hedging as uncertainty and "we" as not knowing what YOU actually did, so we surface both.
_HEDGES = ("i think", "i guess", "i suppose", "maybe", "probably", "hopefully", "i feel like",
           "kind of", "sort of", "i'm not sure", "perhaps")


def available() -> bool:
    """True when local transcription can actually run (engine importable and not disabled)."""
    if os.environ.get("RESUME_AGENT_DISABLE_STT") == "1":
        return False
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _load():
    global _MODEL, _TRIED
    if _MODEL is None and not _TRIED:
        _TRIED = True
        try:
            from faster_whisper import WhisperModel
            size = os.environ.get("RESUME_AGENT_WHISPER_MODEL", "base")
            _MODEL = WhisperModel(size, device="cpu", compute_type="int8")
        except Exception:
            _MODEL = None
    return _MODEL


def transcribe(path) -> str:
    """Transcribe a local audio/video file to text with a LOCAL engine. Returns "" if the engine
    isn't installed or transcription fails, so the caller falls back to a typed transcript. The
    file never leaves the machine."""
    if not available():
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    model = _load()
    if model is None:
        return ""
    try:
        segments, _info = model.transcribe(str(p))
        return " ".join(seg.text.strip() for seg in segments).strip()
    except Exception:
        return ""


def delivery_metrics(transcript: str, seconds: float | None = None) -> dict:
    """Delivery proxies from the transcript alone (no model): word count, estimated speaking time,
    filler-word count, and a plain length hint. If the real recording duration is known, use it for
    a truer words-per-minute. Deterministic, so the offline tests and the UI agree."""
    text = (transcript or "").strip()
    words = len(re.findall(r"\S+", text))
    speak_sec = int(round(seconds)) if seconds else int(round(words / _WPM * 60))
    low = text.lower()
    fillers = 0
    for f in _FILLERS:
        fillers += len(re.findall(r"\b" + f.replace(" ", r"\s+") + r"\b", low))
    wpm = round(words / (speak_sec / 60), 1) if speak_sec else 0
    hint = ("A bit short, add a specific example." if words < 40
            else "Long, aim for under 90 seconds." if speak_sec > 120 else "Good length.")
    hedges = 0
    for h in _HEDGES:
        hedges += len(re.findall(r"\b" + h.replace(" ", r"\s+").replace("'", "'?") + r"\b", low))
    i_count = len(re.findall(r"\b(i|my|me)\b", low))
    we_count = len(re.findall(r"\b(we|our|us)\b", low))
    # "we" heavier than "i" (and used a fair bit) reads as not owning your own contribution.
    ownership = "we" if (we_count > i_count and we_count >= 3) else "i"
    return {"words": words, "speak_sec": speak_sec, "wpm": wpm, "fillers": fillers, "hint": hint,
            "hedges": hedges, "i_count": i_count, "we_count": we_count, "ownership": ownership}
