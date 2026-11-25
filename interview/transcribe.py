"""Local, on-device speech-to-text for interview screens. NEVER cloud.

faster-whisper (CTranslate2) does the recognition. It ships in the installers (release.yml installs
the ``[interview]`` extra and packaging/tailor.spec collects it); a source checkout gets it with

    pip install -e ".[interview]"

We decode the audio OURSELVES with PyAV and hand faster-whisper a 16 kHz mono float32 array.
faster-whisper 1.2.1's own decoder calls ``av.open(..., metadata_errors="ignore")``, which newer
PyAV releases no longer accept, so every answer failed with a TypeError that used to be swallowed
into an empty transcript. Our decoder only uses PyAV calls that are stable across versions, and
faster-whisper's own path stays as a fallback.

The model weights (``base``, about 140 MB) are not in the installer: they download once, on first
use, into ``<data dir>/models`` (pinned to a known revision), and the Interview intro shows the
progress. A dev machine that already has them in the Hugging Face cache uses that copy.

Every failure is written to ``<data dir>/logs/engine.log`` with the exception type, never swallowed
silently. ``RESUME_AGENT_WHISPER_MODEL`` picks another model size or a local model folder;
``RESUME_AGENT_DISABLE_STT=1`` turns the engine off (the offline tests use it so no model loads).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger("tailor.interview")

_ROOT = Path(__file__).resolve().parents[1]
_SAMPLE_RATE = 16000
# The Hugging Face repos faster-whisper uses, pinned to the revision we verified, so a changed
# upstream model can never silently change what gets installed.
_REPOS = {"base": ("Systran/faster-whisper-base", "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66")}
_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")
_MODEL_MB = {"tiny": 75, "base": 140, "small": 465}
# Languages the app speaks. Whisper's detection can mistake accented English for another
# language on a short answer, so we only switch away from English when it is very sure.
_APP_LANGS = ("en", "es", "fr", "de", "pt")
_SWITCH_LANG_P = 0.8
_BEAM = 2              # measured: about 4 s per minute of audio on an Apple Silicon Mac with base

_MODEL = None
_MODEL_LOCK = threading.Lock()   # one load at a time
_RUN_LOCK = threading.Lock()     # one transcription at a time (bounded CPU and memory)
_DL = {"state": "idle", "error": "", "thread": None}
_DL_LOCK = threading.Lock()

# ~130 words per minute is a normal spoken pace; used to estimate speaking time from a transcript.
_WPM = 130
_FILLERS = ("um", "uh", "er", "like", "you know", "basically", "actually", "literally",
            "kind of", "sort of", "i mean", "just", "so yeah")
# Hedging phrases that undercut a confident answer, and the ownership pronouns. Interviewers hear
# hedging as uncertainty and "we" as not knowing what YOU actually did, so we surface both.
_HEDGES = ("i think", "i guess", "i suppose", "maybe", "probably", "hopefully", "i feel like",
           "kind of", "sort of", "i'm not sure", "perhaps")


# ---- paths and the engine log -------------------------------------------------------------------
def _data_dir() -> Path:
    return Path(os.environ.get("RESUME_AGENT_DATA_DIR") or (_ROOT / "data"))


def models_dir() -> Path:
    return Path(os.environ.get("RESUME_AGENT_MODELS_DIR") or (_data_dir() / "models"))


def _size() -> str:
    return (os.environ.get("RESUME_AGENT_WHISPER_MODEL") or "base").strip() or "base"


def _note(msg: str, exc: BaseException | None = None) -> str:
    """Record a speech-to-text failure in the engine log (and the Python log), with the exception
    type, and return the one-line reason. Best effort: logging never takes transcription down."""
    reason = f"{type(exc).__name__}: {exc}" if exc is not None else ""
    line = msg + (f" ({reason})" if reason else "")
    log.warning("speech to text: %s", line)
    try:
        d = _data_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "engine.log"
        if f.exists() and f.stat().st_size > 512 * 1024:      # keep it small: start over
            f.replace(d / "engine.log.1")
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} speech to text: {line}\n")
    except Exception:
        pass
    return reason or msg


# ---- is the engine here, and the model -----------------------------------------------------------
def engine_installed() -> bool:
    try:
        import av  # noqa: F401
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def available() -> bool:
    """True when local transcription can run here (engine importable and not turned off). The model
    may still need its one-time download: ``prepare`` / ``model_status`` cover that."""
    if os.environ.get("RESUME_AGENT_DISABLE_STT") == "1":
        return False
    return engine_installed()


def _model_ready(d: Path) -> bool:
    return d.is_dir() and all((d / f).is_file() for f in _MODEL_FILES)


def _our_model_dir() -> Path:
    return models_dir() / f"faster-whisper-{_size()}"


def _part_dir() -> Path:
    d = _our_model_dir()
    return d.with_name(d.name + ".part")


def _local_model() -> str | None:
    """A model folder already on this computer: an explicit path, our downloaded copy, or the
    Hugging Face cache (a dev machine). None when it still has to be downloaded."""
    size = _size()
    if Path(size).is_dir():
        return size if _model_ready(Path(size)) else None
    if _model_ready(_our_model_dir()):
        return str(_our_model_dir())
    try:
        from faster_whisper.utils import download_model
        p = download_model(size, local_files_only=True)
        return p if _model_ready(Path(p)) else None
    except Exception:
        return None


def _dir_mb(d: Path) -> float:
    try:
        return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1e6
    except Exception:
        return 0.0


def _download() -> str | None:
    """Fetch the model once into <data dir>/models (a .part folder renamed when complete, so a cut
    connection never leaves a half model that looks ready). Returns the folder, or None."""
    size = _size()
    dest, part = _our_model_dir(), _part_dir()
    try:
        from faster_whisper.utils import download_model
        repo, rev = _REPOS.get(size, (size, None))
        part.mkdir(parents=True, exist_ok=True)
        download_model(repo, output_dir=str(part), revision=rev)
        if not _model_ready(part):
            raise RuntimeError("the download finished without the model files")
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        part.replace(dest)
        return str(dest)
    except Exception as e:
        with _DL_LOCK:
            _DL["error"] = _note("could not download the speech model", e)
        return None


def _download_worker() -> None:
    ok = _download()
    with _DL_LOCK:
        _DL["state"] = "ready" if ok else "error"
        _DL["thread"] = None


def prepare() -> dict:
    """Make sure the model is on this computer: start the one-time download in the background when
    it is missing (idempotent: a download in flight is never started twice). Returns the status."""
    if available() and _local_model() is None:
        with _DL_LOCK:
            if _DL["thread"] is None:
                _DL.update(state="downloading", error="")
                t = threading.Thread(target=_download_worker, name="stt-model-download", daemon=True)
                _DL["thread"] = t
                t.start()
    return model_status()


def model_status() -> dict:
    """What the Interview intro shows: is speech to text installed, and is the model ready,
    downloading (with megabytes so far), missing, or failed."""
    total = _MODEL_MB.get(_size(), 140)
    out = {"engine": available(), "model": "missing", "done_mb": 0, "total_mb": total, "error": ""}
    if not out["engine"]:
        out["model"] = "unavailable"
        return out
    with _DL_LOCK:
        state, err = _DL["state"], _DL["error"]
    if state == "downloading":
        out.update(model="downloading", done_mb=min(total, int(_dir_mb(_part_dir()))))
    elif _local_model() is not None:
        out.update(model="ready", done_mb=total)
    elif state == "error":
        out.update(model="error", error=err)
    return out


def ensure_model() -> str | None:
    """The model folder, downloading it now (blocking) when it is missing. Waits for a download
    already in flight instead of starting a second one."""
    p = _local_model()
    if p:
        return p
    with _DL_LOCK:
        t = _DL["thread"]
    if t is not None:
        t.join()
        return _local_model()
    with _DL_LOCK:
        _DL.update(state="downloading", error="")
    p = _download()
    with _DL_LOCK:
        _DL["state"] = "ready" if p else "error"
    return p


def _load():
    """The loaded model, or None (the reason is in the engine log). A failure is NOT remembered:
    the next answer, or Try again on the report, tries again."""
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            from faster_whisper import WhisperModel
        except Exception as e:
            _note("speech to text is not installed", e)
            return None
        path = ensure_model()
        if not path:
            return None
        try:
            threads = max(1, min(4, os.cpu_count() or 1))
            _MODEL = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=threads)
        except Exception as e:
            _note("could not load the speech model", e)
            _MODEL = None
        return _MODEL


# ---- decoding -----------------------------------------------------------------------------------
def decode_audio(path, sample_rate: int = _SAMPLE_RATE):
    """Decode the audio track of any recording PyAV can open (the WebM/Opus Chromium's
    MediaRecorder writes, with or without a video track, plus Ogg, MP4, MP3, WAV) to a mono float32
    numpy array at ``sample_rate``. Only the audio packets are decoded. Uses no keyword that a PyAV
    release has dropped. A corrupt packet is skipped and a truncated file keeps what decoded, so
    one bad cluster at the end of a take does not cost the whole answer."""
    import av
    import numpy as np
    from av.audio.resampler import AudioResampler

    chunks = []
    with av.open(str(path), mode="r") as container:
        if not container.streams.audio:
            return np.zeros(0, dtype=np.float32)
        stream = container.streams.audio[0]
        resampler = AudioResampler(format="flt", layout="mono", rate=sample_rate)

        def _take(frames):
            for out in frames or []:
                chunks.append(out.to_ndarray().reshape(-1))

        try:
            for packet in container.demux(stream):
                try:
                    frames = packet.decode()
                except av.error.FFmpegError:
                    continue                      # one bad packet, keep going
                for frame in frames:
                    frame.pts = None              # MediaRecorder timestamps can jump; let it resample
                    _take(resampler.resample(frame))
        except av.error.FFmpegError as e:         # truncated file: keep what we decoded
            _note(f"recording ended early, kept {sum(len(c) for c in chunks) / sample_rate:.1f}s", e)
        _take(resampler.resample(None))           # flush
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32, copy=False)


def _language(model, audio) -> str:
    """English unless Whisper is very sure the answer is in another language the app speaks."""
    try:
        lang, prob, _all = model.detect_language(audio, vad_filter=True)
        if lang in _APP_LANGS and (lang == "en" or prob >= _SWITCH_LANG_P):
            return lang
    except Exception:
        pass
    return "en"


def _run(model, audio, language: str) -> str:
    segments, _info = model.transcribe(audio, language=language, beam_size=_BEAM, vad_filter=True,
                                       condition_on_previous_text=False)
    return " ".join(seg.text.strip() for seg in segments).strip()


# ---- transcription ------------------------------------------------------------------------------
def transcribe_detail(path) -> dict:
    """Transcribe one recording on this computer. Returns {text, status, error, seconds, language}
    where status is "ok" (words heard), "silent" (it ran but heard no speech), "failed" (this file
    could not be transcribed; the reason is in ``error`` and in the engine log) or "unavailable"
    (no speech-to-text engine or model here). The file never leaves the machine."""
    out = {"text": "", "status": "unavailable", "error": "", "seconds": None, "language": ""}
    if not available():
        out["error"] = "Speech to text is not installed."
        return out
    p = Path(path)
    if not p.is_file():
        out.update(status="failed", error=_note(f"no recording at {p.name}"))
        return out
    model = _load()
    if model is None:
        out["error"] = "The speech model is not ready."
        return out
    with _RUN_LOCK:
        audio = None
        try:
            audio = decode_audio(p)
            out["seconds"] = round(len(audio) / _SAMPLE_RATE, 1)
        except Exception as e:
            _note(f"could not decode {p.name}, trying faster-whisper's own decoder", e)
        try:
            if audio is not None and not len(audio):
                out["status"] = "silent"
                return out
            lang = _language(model, audio) if audio is not None else "en"
            text = _run(model, audio if audio is not None else str(p), lang)
        except Exception as e:
            out.update(status="failed", error=_note(f"could not transcribe {p.name}", e))
            return out
    out.update(text=text, language=lang, status="ok" if text else "silent")
    return out


def transcribe(path) -> str:
    """The words of one recording, or "" when nothing could be heard or transcription failed (the
    reason is in the engine log). See ``transcribe_detail`` for the status."""
    return transcribe_detail(path)["text"]


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
