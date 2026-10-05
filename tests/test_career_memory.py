"""Career-memory ingest: the lifelong-diary capture path (2026-08-03).

A person can add anything about their career to a durable, recallable memory, a note/event they
type now, or (best-effort, when the optional extractor is present) a document. This is the first
brick of the MemPalace "career diary" direction. Offline: the semantic index is disabled
(RESUME_AGENT_PALACE_INDEX=0) so these tests are model-free and never touch the network; the
verbatim transcript is the thing we assert on.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_PALACE_INDEX", "0")   # verbatim only, no embedder/network
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


def test_a_typed_note_is_remembered_in_the_career_diary(client):
    A, c = client
    r = c.post("/api/memory/ingest",
               json={"text": "At Acme I led the payments migration and cut latency in half."})
    assert r.status_code == 200 and r.get_json()["stored_chunks"] == 1
    # It landed in the durable transcript the recall + CV build read from.
    hist = A._palace().load_history()
    assert any("payments migration" in t["content"] for t in hist)


def test_empty_ingest_is_rejected(client):
    _, c = client
    assert c.post("/api/memory/ingest", json={"text": "   "}).status_code == 400


def test_a_long_note_is_split_into_searchable_chunks():
    """Each stored memory should be a coherent, searchable unit, not one giant blob."""
    import tempfile
    from intake.palace_memory import PalaceMemory
    pm = PalaceMemory(tempfile.mkdtemp())
    pm.index = False                                    # verbatim only, no embedder
    long_text = "\n\n".join(f"Paragraph {i} about a real project I shipped." * 6 for i in range(40))
    n = pm.ingest_note(long_text, source="note")
    assert n >= 2                                       # chunked, not one blob
    assert len(pm.load_history()) == n                  # every chunk persisted


def test_source_is_tagged_so_documents_are_attributable():
    import tempfile
    from intake.palace_memory import PalaceMemory
    pm = PalaceMemory(tempfile.mkdtemp())
    pm.index = False
    pm.ingest_note("Exceeded target by 30 percent this year.", source="2024-review.pdf")
    hist = pm.load_history()
    assert hist and "[2024-review.pdf]" in hist[0]["content"]   # attributable to its source


def test_uploaded_document_text_is_extracted_and_remembered(client):
    """Drop in a document -> its text is extracted LOCALLY (base deps: python-docx) and stored in
    the career diary. The file itself is not kept."""
    import io
    from docx import Document
    A, c = client
    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph("2024 review: exceeded target by 30 percent and mentored two juniors.")
    doc.save(buf); buf.seek(0)
    r = c.post("/api/memory/ingest",
               data={"file": (buf, "2024-review.docx",
                              "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
               content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["stored_chunks"] >= 1
    assert any("exceeded target by 30 percent" in t["content"] for t in A._palace().load_history())


def test_unreadable_file_degrades_gracefully(client):
    _, c = client
    import io
    r = c.post("/api/memory/ingest",
               data={"file": (io.BytesIO(b"\x00\x01\x02 not real text"), "junk.bin",
                              "application/octet-stream")},
               content_type="multipart/form-data")
    assert r.status_code == 422        # no text extracted -> clear message, not a crash


def test_cv_worthiness_nudge_flags_a_real_accomplishment(client):
    """On demand, the diary notices when a note is CV-worthy, the 'you shared something big
    without realising it' magic, without running the model on every message."""
    _, c = client
    good = c.post("/api/memory/assess",
                  json={"text": "I led the payments migration and cut checkout latency by 40 percent."}).get_json()
    assert good["assessment"]["worthy"] is True and good["assessment"]["headline"]
    vague = c.post("/api/memory/assess", json={"text": "did some stuff today"}).get_json()
    assert vague["assessment"]["worthy"] is False and vague["assessment"]["reason"]
    assert c.post("/api/memory/assess", json={"text": ""}).status_code == 400
