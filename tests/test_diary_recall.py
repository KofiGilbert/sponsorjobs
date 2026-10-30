"""The chat diary and its keyword recall (docs/applicant-brain.md, section 12; Phase 2).

MemPalace and ChromaDB are gone. The diary (intake/diary.py) keeps every turn verbatim and
recalls by plain word overlap: lines sharing more of the job's words first, then newest first.
Nothing imports mempalace or chromadb, the app works with neither installed, a diary turn that
produced a fact is that fact's source_ref, and Erase removes every diary file and the old store
folder (which nothing else ever deletes)."""
from __future__ import annotations

import ast
import importlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from intake.diary import Diary, erase_all, keywords

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- recall
def test_recall_ranks_by_shared_words_then_newest(tmp_path):
    d = Diary(tmp_path / "palace")
    d.remember_turn("user", "I built the payments API in Python at Acme.")             # 1: python, payment
    d.remember_turn("user", "I love hiking on weekends.")                               # 2: nothing
    d.remember_turn("agent", "Which Python payments projects should I use?")            # 3: app's own question
    d.remember_turn("note", "Led the Python migration of the payments ledger to Postgres.")  # 4: 3 words
    d.remember_turn("user", "Wrote Python scripts for reporting.")                      # 5: python only
    d.remember_turn("user", "Owned Python tooling for the data team.")                  # 6: python only, newer
    hits = d.recall("Senior Python Engineer, payments platform, Postgres", n=5)
    assert [h["text"] for h in hits] == [
        "Led the Python migration of the payments ledger to Postgres.",
        "I built the payments API in Python at Acme.",
        "Owned Python tooling for the data team.",                 # a tie goes to the newer line
        "Wrote Python scripts for reporting.",
    ]
    assert [h["ref"] for h in hits] == ["diary:default.jsonl:4", "diary:default.jsonl:1",
                                        "diary:default.jsonl:6", "diary:default.jsonl:5"]
    assert hits[0]["similarity"] == 0.75 and hits[1]["similarity"] == 0.5 and hits[2]["similarity"] == 0.25
    assert len(d.recall("Python", n=2)) == 2                       # n caps the result


def test_recall_skips_noise_and_repeats(tmp_path):
    d = Diary(tmp_path / "palace")
    assert d.recall("anything at all") == []                       # no diary yet
    d.remember_turn("user", "Shipped a Kubernetes operator.")
    d.remember_turn("user", "Shipped a Kubernetes operator.")       # said twice, recalled once
    assert [h["ref"] for h in d.recall("Kubernetes")] == ["diary:default.jsonl:2"]
    assert d.recall("") == [] and d.recall("the and of with") == []   # stop words only
    assert d.recall("experience years team skills") == []          # every job post says these
    with open(d.log_path, "a", encoding="utf-8") as fh:
        fh.write("not json\n\n")                                   # a damaged line is skipped
    d.remember_turn("user", "Mentored two Kubernetes interns.")
    assert d.recall("kubernetes")[0]["ref"] == "diary:default.jsonl:5"


def test_keywords_keep_tech_tokens_and_meet_plurals():
    assert {"c++", "c#", "node.js", "ci/cd"} <= keywords("C++, C#, Node.js and CI/CD.")
    assert keywords("dashboards") == keywords("dashboard")
    assert keywords("migrations") == keywords("migration")
    assert keywords("studies") == keywords("study")


# --------------------------------------------------------------------------- refs
def test_refs_count_lines_across_instances_and_threads(tmp_path):
    a = Diary(tmp_path / "palace", person="Ama")
    assert a.remember_turn("user", "one") == "diary:ama.jsonl:1"
    b = Diary(tmp_path / "palace", person="Ama")                   # a second instance (Telegram)
    assert b.remember_turn("user", "two") == "diary:ama.jsonl:2"
    with open(a.log_path, "a", encoding="utf-8") as fh:            # an outside write moves the count
        fh.write(json.dumps({"role": "user", "content": "three"}) + "\n")
    assert a.remember_turn("user", "four") == "diary:ama.jsonl:4" and a.last_ref.endswith(":4")

    refs: list[str] = []
    lock = threading.Lock()

    def write(i):
        r = Diary(tmp_path / "palace", person="Ama").remember_turn("user", f"turn {i}")
        with lock:
            refs.append(r)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(int(r.rsplit(":", 1)[1]) for r in refs) == list(range(5, 25))
    lines = a.log_path.read_text(encoding="utf-8").splitlines()
    for r in refs:                                                 # each ref points at its own line
        n = int(r.rsplit(":", 1)[1])
        assert json.loads(lines[n - 1])["content"].startswith("turn ")


def test_a_fact_from_chat_names_its_diary_line(tmp_path):
    from intake.memory import ConversationMemory
    from llm.base import FakeLLM
    from ui.records import CVRecords
    from ui.session import WebIntake
    db = str(tmp_path / "m.db")
    s = WebIntake("Data Analyst at a fintech. SQL, dashboards.", "x", FakeLLM(), ConversationMemory(db),
                  CVRecords(":memory:"), tmp_path / "cv", palace_dir=tmp_path / "palace")
    s.start()
    s.submit("Please don't mention the 2021 gap on my resume. I'm targeting fintech.")
    facts = {f["key"]: f for f in ConversationMemory(db).list_facts("default", status=None)}
    prov = facts["avoid_2021_gap"]["provenance"]
    ref = prov.rsplit(" · ", 1)[1]
    assert ref.startswith("diary:default.jsonl:")
    line = (tmp_path / "palace" / "transcripts" / "default.jsonl").read_text(encoding="utf-8") \
        .splitlines()[int(ref.rsplit(":", 1)[1]) - 1]
    assert "2021 gap" in json.loads(line)["content"] and json.loads(line)["role"] == "user"


def test_the_brain_takes_the_diary_line_as_source_ref(tmp_path, monkeypatch):
    from applicant import migrate as M
    from brain_fixtures import write_legacy
    from intake.memory import ConversationMemory
    monkeypatch.delenv(M.ENV, raising=False)
    data = write_legacy(tmp_path / "data")
    mem = ConversationMemory(str(data / "resume_agent.db"))
    mem.upsert_fact("default", "preference", "work_style", "Remote first",
                    provenance="turn 3 · 2026-10-30 · diary:default.jsonl:2")
    mem.close()
    try:
        assert M.apply(data)["ok"]
        st = M.open_store(M.brain_path(data))
        refs = {r["subject"]: r["source_ref"] for r in st.q("SELECT subject, source_ref FROM fact")}
        assert refs["work_style"] == "diary:default.jsonl:2"
        assert refs["tone"] == "resume_agent.db:memory_facts:preference:tone"   # no diary line: as before
    finally:
        M.forget_process_state(data)


# --------------------------------------------------------------------------- erase
def test_erase_all_removes_every_diary_and_the_old_store(tmp_path):
    root = tmp_path / "palace"
    Diary(root).remember_turn("user", "mine")
    Diary(root, person="_autoapply_ab12").remember_turn("user", "scratch")
    (root / "store" / "chroma").mkdir(parents=True)
    (root / "store" / "chroma" / "chroma.sqlite3").write_bytes(b"x")
    assert sorted(erase_all(root)) == ["autoapply_ab12.jsonl", "default.jsonl", "store"]
    assert not list((root / "transcripts").glob("*.jsonl")) and not (root / "store").exists()
    d = Diary(root)
    assert d.load_history() == [] and d.remember_turn("user", "again") == "diary:default.jsonl:1"


def test_the_store_folder_is_left_alone_until_erase(tmp_path):
    root = tmp_path / "palace"
    (root / "store").mkdir(parents=True)
    (root / "store" / "keep.bin").write_bytes(b"x")
    d = Diary(root)
    d.remember_turn("user", "Built dashboards in Tableau.")
    d.recall("Tableau dashboards")
    d.load_history()
    assert (root / "store" / "keep.bin").exists()


def test_memory_erase_route_removes_the_diary_and_store(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._palace().remember_turn("user", "I led the Postgres migration.")
    (A.PALACE_DIR / "store").mkdir(parents=True)
    r = A.app.test_client().post("/api/memory/erase")
    assert r.status_code == 200
    assert A._palace().load_history() == [] and not (A.PALACE_DIR / "store").exists()


# --------------------------------------------------------------------------- the library is gone
def _imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out


def test_nothing_imports_mempalace_or_chromadb():
    skip = {".venv", "venv", "node_modules", ".git", ".claude", "build", "dist", "__pycache__"}
    bad, seen = [], 0
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if x not in skip]
        for name in files:
            if name.endswith((".py", ".spec")):
                seen += 1
                f = Path(folder) / name
                if _imports(f) & {"mempalace", "chromadb"}:
                    bad.append(str(f.relative_to(ROOT)))
    assert seen > 100 and bad == []
    import tomllib
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = list(proj["dependencies"]) + [d for ds in proj.get("optional-dependencies", {}).values() for d in ds]
    for req in ROOT.glob("requirements*.txt"):
        deps += [ln.split("#")[0] for ln in req.read_text(encoding="utf-8").splitlines()]
    assert not [d for d in deps if "mempalace" in d.lower() or "chromadb" in d.lower()]


@pytest.fixture
def no_memory_libs(monkeypatch):
    """Make mempalace and chromadb unimportable, as on a machine that never installed them."""
    for name in list(sys.modules):
        if name.split(".")[0] in ("mempalace", "chromadb"):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "mempalace", None)
    monkeypatch.setitem(sys.modules, "chromadb", None)
    with pytest.raises(ImportError):
        importlib.import_module("chromadb")


def test_the_app_remembers_and_recalls_without_either_library(no_memory_libs, tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    c = A.app.test_client()
    assert c.post("/api/memory/ingest", json={"text": "Built a Tableau dashboard that halved reporting time."}
                  ).get_json()["stored_chunks"] == 1
    hi = c.post("/api/memory/highlights", json={"role": "Data Analyst", "jd": "dashboards and reporting"}
                ).get_json()["highlights"]
    assert [h["text"] for h in hi] == ["Built a Tableau dashboard that halved reporting time."]
    assert c.get("/api/memory").get_json()["turns"] == 1
