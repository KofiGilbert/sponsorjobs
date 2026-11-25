"""The on-device speech to text behind the recorded practice interview (interview/transcribe.py).

Every answer used to fail: faster-whisper 1.2.1's own decoder passes ``metadata_errors`` to
``av.open``, newer PyAV rejects it with a TypeError, and ``transcribe`` swallowed that into "". So
the app asked people to type their answers. Now we decode the recording ourselves, failures are
logged with their exception type, and nobody is ever asked to type.

No Whisper model is loaded here: the recordings are real WebM/Opus files PyAV writes in the test,
and the model is a stand-in where one is needed.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")


@pytest.fixture
def T(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("RESUME_AGENT_DISABLE_STT", raising=False)
    monkeypatch.delenv("RESUME_AGENT_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("RESUME_AGENT_MODELS_DIR", raising=False)
    import interview.transcribe as mod
    importlib.reload(mod)
    return mod


def _webm(path: Path, seconds: float = 2.0, video: bool = True, hz: float = 440.0) -> Path:
    """A WebM like Chromium's MediaRecorder writes: Opus audio at 48 kHz mono, plus a small VP8
    video track (the decoder must skip it)."""
    if "libopus" not in av.codecs_available:
        pytest.skip("this PyAV build has no Opus encoder")
    out = av.open(str(path), "w", format="webm")
    a = out.add_stream("libopus", rate=48000, layout="mono")
    v = None
    if video and "libvpx" in av.codecs_available:
        v = out.add_stream("libvpx", rate=10)
        v.width, v.height, v.pix_fmt = 64, 48, "yuv420p"
    sr = 48000
    t = np.arange(int(sr * seconds)) / sr
    x = (0.3 * np.sin(2 * np.pi * hz * t) * 32767).astype(np.int16)
    pts = 0
    for i in range(0, len(x), 960):
        fr = av.AudioFrame.from_ndarray(x[None, i:i + 960], format="s16", layout="mono")
        fr.sample_rate, fr.pts = sr, pts
        pts += fr.samples
        for p in a.encode(fr):
            out.mux(p)
    for p in a.encode(None):
        out.mux(p)
    if v is not None:
        for k in range(int(seconds * 10)):
            vf = av.VideoFrame.from_ndarray(np.zeros((48, 64, 3), np.uint8), format="rgb24")
            vf.pts = k
            for p in v.encode(vf):
                out.mux(p)
        for p in v.encode(None):
            out.mux(p)
    out.close()
    return path


# ---- the decoder ----------------------------------------------------------------------------------
def test_decodes_a_mediarecorder_style_webm_to_16k_mono_float(T, tmp_path):
    a = T.decode_audio(_webm(tmp_path / "answer_0.webm", seconds=2.0))
    assert a.dtype == np.float32 and a.ndim == 1
    assert abs(len(a) / 16000 - 2.0) < 0.1                      # 2 s of audio at 16 kHz
    assert 0.2 < float(np.abs(a).max()) < 0.4                     # the tone survived, not silence
    # the 440 Hz tone is still the loudest frequency after resampling
    spec = np.abs(np.fft.rfft(a[:16000]))
    assert abs(np.argmax(spec) * 16000 / 16000 - 440) < 5


def test_decodes_an_audio_only_recording_too(T, tmp_path):
    a = T.decode_audio(_webm(tmp_path / "a.webm", seconds=1.0, video=False))
    assert abs(len(a) / 16000 - 1.0) < 0.1


def test_the_decoder_never_passes_a_keyword_pyav_dropped(T, tmp_path, monkeypatch):
    """The root cause: faster-whisper passed metadata_errors, which PyAV no longer takes. Our
    decoder must work against an av.open that accepts only the file and the mode."""
    path = _webm(tmp_path / "a.webm", seconds=1.0)
    real = av.open

    def strict_open(file, mode="r", **kw):
        if kw:
            raise TypeError(f"open() got an unexpected keyword argument {next(iter(kw))!r}")
        return real(file, mode)

    monkeypatch.setattr(av, "open", strict_open)
    assert len(T.decode_audio(path)) > 0


def test_a_truncated_recording_keeps_what_decoded(T, tmp_path):
    full = _webm(tmp_path / "full.webm", seconds=3.0, video=False)
    cut = tmp_path / "cut.webm"
    data = full.read_bytes()
    cut.write_bytes(data[: int(len(data) * 0.6)])                # the take was cut off
    a = T.decode_audio(cut)
    assert 0.5 < len(a) / 16000 < 3.0


# ---- transcription, with a stand-in model ---------------------------------------------------------
class _Seg:
    def __init__(self, text):
        self.text = text


class _Model:
    def __init__(self, text=" Hello there. ", lang=("en", 0.99), fail=None):
        self.text, self.lang, self.fail, self.seen = text, lang, fail, []

    def detect_language(self, audio, vad_filter=False):
        return self.lang[0], self.lang[1], []

    def transcribe(self, audio, **kw):
        self.seen.append((type(audio).__name__, kw))
        if self.fail:
            raise self.fail
        return iter([_Seg(self.text)]), None


def _log(tmp_path) -> str:
    f = tmp_path / "data" / "logs" / "engine.log"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def test_transcribe_passes_our_decoded_audio_with_vad_and_a_small_beam(T, tmp_path, monkeypatch):
    m = _Model()
    monkeypatch.setattr(T, "available", lambda: True)
    monkeypatch.setattr(T, "_load", lambda: m)
    d = T.transcribe_detail(_webm(tmp_path / "answer_0.webm", seconds=1.0))
    assert d["status"] == "ok" and d["text"] == "Hello there." and d["language"] == "en"
    assert abs(d["seconds"] - 1.0) < 0.1
    kind, kw = m.seen[0]
    assert kind == "ndarray"                                     # our array, not the file path
    assert kw["vad_filter"] is True and kw["beam_size"] <= 2 and kw["language"] == "en"


def test_transcribe_surfaces_and_logs_a_failure_instead_of_swallowing_it(T, tmp_path, monkeypatch):
    m = _Model(fail=TypeError("open() got an unexpected keyword argument 'metadata_errors'"))
    monkeypatch.setattr(T, "available", lambda: True)
    monkeypatch.setattr(T, "_load", lambda: m)
    d = T.transcribe_detail(_webm(tmp_path / "answer_1.webm", seconds=1.0))
    assert d["status"] == "failed" and d["text"] == ""
    assert d["error"].startswith("TypeError") and "metadata_errors" in d["error"]
    log = _log(tmp_path)
    assert "TypeError" in log and "answer_1.webm" in log            # in the engine log, with its type
    assert T.transcribe(tmp_path / "answer_1.webm") == ""           # the plain wrapper stays safe


def test_a_decode_failure_falls_back_to_faster_whispers_own_path_and_is_logged(T, tmp_path, monkeypatch):
    m = _Model(text="From the fallback.")
    monkeypatch.setattr(T, "available", lambda: True)
    monkeypatch.setattr(T, "_load", lambda: m)
    monkeypatch.setattr(T, "decode_audio", lambda p: (_ for _ in ()).throw(ValueError("odd container")))
    f = tmp_path / "answer_2.webm"
    f.write_bytes(b"\x1aE\xdf\xa3 not much")
    d = T.transcribe_detail(f)
    assert d["status"] == "ok" and d["text"] == "From the fallback." and m.seen[0][0] == "str"
    assert "ValueError" in _log(tmp_path)


def test_a_recording_with_no_audio_is_silent_not_failed(T, tmp_path, monkeypatch):
    monkeypatch.setattr(T, "available", lambda: True)
    monkeypatch.setattr(T, "_load", lambda: _Model(text=""))
    d = T.transcribe_detail(_webm(tmp_path / "quiet.webm", seconds=1.0))
    assert d["status"] == "silent" and d["text"] == ""


def test_no_engine_or_model_is_unavailable_and_logged(T, tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DISABLE_STT", "1")
    f = _webm(tmp_path / "a.webm", seconds=1.0)
    assert T.available() is False and T.transcribe_detail(f)["status"] == "unavailable"
    monkeypatch.delenv("RESUME_AGENT_DISABLE_STT")
    monkeypatch.setattr(T, "available", lambda: True)
    monkeypatch.setattr(T, "_load", lambda: None)                   # the model could not be had
    assert T.transcribe_detail(f)["status"] == "unavailable"


@pytest.mark.parametrize("detected,expected", [
    (("en", 0.55), "en"), (("pt", 0.6), "en"), (("pt", 0.95), "pt"), (("ja", 0.99), "en")])
def test_english_unless_whisper_is_very_sure_of_another_app_language(T, detected, expected):
    assert T._language(_Model(lang=detected), np.zeros(16000, np.float32)) == expected


# ---- the one-time model download ------------------------------------------------------------------
def _fake_download(files=("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"), fail=None):
    calls = []

    def download_model(size_or_id, output_dir=None, local_files_only=False, revision=None, **kw):
        calls.append((size_or_id, output_dir, local_files_only, revision))
        if local_files_only:
            raise FileNotFoundError("not in the cache")
        if fail:
            raise fail
        for f in files:
            (Path(output_dir) / f).write_bytes(b"x" * 1000)
        return output_dir

    return download_model, calls


def test_the_model_downloads_once_into_the_data_dir_pinned(T, tmp_path, monkeypatch):
    fw_utils = pytest.importorskip("faster_whisper.utils")
    dl, calls = _fake_download()
    monkeypatch.setattr(fw_utils, "download_model", dl)
    assert T.model_status()["model"] == "missing"
    p = T.ensure_model()
    dest = tmp_path / "data" / "models" / "faster-whisper-base"
    assert p == str(dest) and (dest / "model.bin").is_file()
    assert not (tmp_path / "data" / "models" / "faster-whisper-base.part").exists()
    repo, _out, _local, rev = [c for c in calls if not c[2]][0]
    assert repo == "Systran/faster-whisper-base" and rev and len(rev) == 40   # a pinned revision
    assert T.model_status()["model"] == "ready"
    n = len(calls)
    assert T.ensure_model() == str(dest) and len(calls) == n          # never downloaded twice


def test_prepare_downloads_in_the_background_and_reports_progress(T, tmp_path, monkeypatch):
    fw_utils = pytest.importorskip("faster_whisper.utils")
    dl, _calls = _fake_download()
    monkeypatch.setattr(fw_utils, "download_model", dl)
    st = T.prepare()
    assert st["engine"] is True and st["model"] in ("downloading", "ready") and st["total_mb"] == 140
    t = T._DL["thread"]
    if t is not None:
        t.join(10)
    assert T.model_status()["model"] == "ready"


def test_a_failed_download_is_an_error_status_and_logged(T, tmp_path, monkeypatch):
    fw_utils = pytest.importorskip("faster_whisper.utils")
    dl, _calls = _fake_download(fail=ConnectionError("no route to host"))
    monkeypatch.setattr(fw_utils, "download_model", dl)
    assert T.ensure_model() is None
    st = T.model_status()
    assert st["model"] == "error" and "ConnectionError" in st["error"]
    assert "ConnectionError" in _log(tmp_path)
