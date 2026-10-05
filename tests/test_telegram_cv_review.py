"""CV review and chat edits over Telegram (notify/cvdoc.py, notify/cvreview.py, notify/redact.py,
the broker's send_media / edit routes). Offline: FakeLLM, fake channels, fake Telegram transport;
the redaction pixel test compiles a real page when pdflatex is available.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from llm.base import FakeLLM
from notify import cvdoc
from notify.alerts import NotifyState
from notify.cvreview import (MSG_DISCARDED, MSG_EDIT_FOOTER, MSG_PDF_WARNING, CVReview,
                             fact_question)
from notify.hub import NotifyHub
from ui.records import CVRecords
from tests.conftest import requires_latex

ROOT = Path(__file__).resolve().parents[1]
JD = "Senior Product Manager at Acme. We need SQL, PnL ownership, ETL and Kafka."

BASE = {
    "identity": {"name": "Ama Mensah", "email": "ama.mensah@example.com",
                 "phone": "(312) 555-0187", "address": "Chicago, IL"},
    "summary": "Product manager with seven years in retail banking and payments.",
    "experience": [
        {"org": "Stanbic Bank", "location": "Accra", "roles": [
            {"title": "Senior Product Manager", "dates": "Jan 2020 to Present", "bullets": [
                "Led the mobile payments roadmap across three markets with SQL reporting.",
                "Ran profit reviews for the cards business with finance partners.",
                "Built data checks for the warehouse feeding risk dashboards."]}]},
        {"org": "MTN", "location": "Accra", "roles": [
            {"title": "Product Analyst", "dates": "Jun 2016 to Dec 2019", "bullets": [
                "Analysed churn for prepaid customers with SQL and Excel.",
                "Wrote weekly PnL notes for the mobile money team."]}]},
    ],
    "projects": [{"name": "Budget Bot", "bullets": ["Built a Python budgeting assistant used by 200 friends."]},
                 {"name": "Fare Map", "bullets": ["Mapped bus fares across Accra in a weekend."]}],
    "education": [{"school": "University of Ghana", "degree": "BSc Economics", "date": "2016",
                   "location": "Accra"}],
    "skills": {"Tools": "SQL, Python, Excel", "Methods": "PnL, A/B testing"},
}


def _page():
    p = json.loads(json.dumps(BASE))
    p["experience"][0]["roles"][0]["bullets"][1] = "Owned PnL reviews for the cards business with finance partners."
    p["experience"][0]["roles"][0]["bullets"][2] = "Built ETL checks for the warehouse feeding risk dashboards."
    p["summary"] = "Product manager with seven years in banking and payments, owning PnL."
    return p


SECTIONS = ["summary", "education", "skills", "experience", "projects"]


# ------------------------------------------------------------------ fakes
class FakeChannel:
    name = "fake"

    def __init__(self):
        self.sent, self.photos, self.docs, self.edits, self.acks = [], [], [], [], []
        self._mid = 100

    def _id(self):
        self._mid += 1
        return {"ok": True, "message_id": self._mid}

    def send(self, text, buttons=None):
        assert len(text) <= 4096
        self.sent.append({"text": text, "buttons": buttons})
        return self._id()

    def send_photo(self, path, caption="", buttons=None):
        assert len(caption) <= 1024
        self.photos.append({"path": path, "caption": caption, "buttons": buttons})
        return self._id()

    def send_document(self, path, caption="", buttons=None):
        self.docs.append({"path": path, "caption": caption})
        return self._id()

    def edit_text(self, message_id, text=None, buttons=None, caption=None):
        self.edits.append({"message_id": message_id, "buttons": buttons})
        return {"ok": True}

    def ack(self, ev, text=""):
        self.acks.append(text)

    def texts(self):
        return [m["text"] for m in self.sent]

    def all_button_ids(self):
        out = []
        for m in self.sent + self.photos:
            for row in m.get("buttons") or []:
                out += [b["id"] for b in row]
        return out


class Res:
    def __init__(self, ok=True, pages=1, overfull=0, fill=0.9, pdf=None):
        self.ok, self.pages, self.overfull_count, self.fill_ratio, self.pdf_path = ok, pages, overfull, fill, pdf


class FakeCompile:
    """Writes a stand-in PDF; overflows when any bullet in the source is longer than
    ``max_bullet`` characters, or when a fixed verdict is set."""

    def __init__(self, max_bullet=10_000, verdict=None):
        self.max_bullet, self.verdict, self.calls = max_bullet, verdict, 0

    def __call__(self, tex, workdir, jobname="cv"):
        self.calls += 1
        pdf = Path(workdir) / f"{jobname}.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake " + str(self.calls).encode())
        if self.verdict is not None:
            return self.verdict(pdf)
        longest = max((len(ln) for ln in tex.splitlines() if "\\item" in ln), default=0)
        if longest > self.max_bullet:
            return Res(pages=2, pdf=pdf)
        return Res(pdf=pdf)


def fake_preview(pdf, png, identity, redact=True):
    Path(png).write_bytes(b"png")
    return {"path": str(png), "redacted": redact, "band": (1, 2) if redact else None}


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "r.db"
    recs = CVRecords(db)
    rid = recs.add(role="Senior Product Manager", company="Acme", coverage=70, pdf_path="",
                   jd_label="Senior Product Manager",
                   data={"jd_text": JD, "page_profile": _page(), "base_profile": BASE,
                         "sections": SECTIONS, "template": "shetty",
                         "coverage": {"ratio": 70, "present": ["SQL", "PnL", "ETL"],
                                      "missing": ["Kafka"], "missing_supported": []},
                         "source_job": {"url": "https://jobs.example.com/1"}})
    recs.close()
    (tmp_path / f"cv-{rid}.pdf").write_bytes(b"%PDF-1.4 original")
    state = NotifyState(tmp_path / "notify_state.json")
    profile_updates = []

    def update_profile(change, doc):
        profile_updates.append(change)
        return True

    def make(llm=None, compile_fn=None, can_apply=False):
        return CVReview(state, lambda: CVRecords(db), lambda: llm or FakeLLM(), tmp_path,
                        lambda name: (ROOT / "config" / "resume_shetty.tex").read_text(),
                        compile_fn=compile_fn or FakeCompile(), preview_fn=fake_preview,
                        can_apply=lambda r: can_apply, apply=lambda r: f"Applied #{r}",
                        update_profile=update_profile)
    return {"rid": rid, "db": db, "state": state, "make": make, "dir": tmp_path,
            "profile_updates": profile_updates}


def _data(env):
    recs = CVRecords(env["db"])
    try:
        return json.loads(recs.get(env["rid"])["data"])
    finally:
        recs.close()


def _tap(rv, ch, act):
    """Tap the most recent button with action ``act``."""
    for m in reversed(ch.sent + ch.photos):
        for row in m.get("buttons") or []:
            for b in row:
                if b["id"].startswith(f"cv:{act}:"):
                    return rv.handle_button(ch, b["id"])
    raise AssertionError(f"no {act} button")


# ------------------------------------------------------------------ spans
def test_span_ids_are_stable_and_cover_fields():
    spans = cvdoc.span_map({"profile": _page(), "sections": SECTIONS})
    for sid in ("S", "E1.company", "E1.title", "E1.dates", "E1.location", "E1.1", "E1.3",
                "E2.2", "P1.name", "P1.1", "P2.1", "ED1.school", "ED1.degree", "ED1.date", "K1", "K2"):
        assert sid in spans, sid
    assert spans["E1.2"]["text"].startswith("Owned PnL")
    assert cvdoc.is_fact(spans["E1.title"]) and cvdoc.is_fact(spans["ED1.degree"])
    assert not cvdoc.is_fact(spans["E1.2"]) and not cvdoc.is_fact(spans["P1.name"])


def test_nested_roles_number_bullets_across_roles():
    p = {"experience": [{"org": "Bank", "roles": [
        {"title": "Lead", "dates": "2022", "bullets": ["a one", "a two"]},
        {"title": "Analyst", "dates": "2020", "bullets": ["b one"]}]}]}
    spans = cvdoc.span_map({"profile": p})
    assert spans["E1.3"]["text"] == "b one" and spans["E1.R2.title"]["text"] == "Analyst"


def test_caption_summary_contents():
    cap = cvdoc.change_summary("Senior Product Manager", "Acme", BASE, _page(),
                               {"present": ["SQL", "PnL", "ETL"], "missing": ["Kafka"]})
    assert cap == ("Tailored for Senior Product Manager at Acme: 2 bullets reworded, summary "
                   "updated. Added the job's terms: ETL. Missing: Kafka.")


def test_show_changes_chunks_under_4096_with_span_ids():
    page = _page()
    page["experience"][0]["roles"][0]["bullets"] += [f"Delivered result number {i} " + "x" * 300
                                                     for i in range(30)]
    base = json.loads(json.dumps(page))
    base["experience"][0]["roles"][0]["bullets"][1] = "Ran profit reviews."
    blocks = cvdoc.show_changes("PM", "Acme", base, {"profile": page, "sections": SECTIONS})
    msgs = cvdoc.chunk(blocks)
    assert len(msgs) > 1 and all(len(m) <= 4000 for m in msgs)
    joined = "\n".join(msgs)
    assert "E1.2\nBefore: Ran profit reviews.\nAfter: Owned PnL reviews" in joined
    assert "E1.1 (unchanged)" in joined and "E1.title: Senior Product Manager" in joined
    assert "P2.1 (unchanged)" in joined and "Section order: Summary, Education" in joined


# ------------------------------------------------------------------ preview
def test_preview_caption_buttons_and_short_callback_data(env):
    rv, ch = env["make"](), FakeChannel()
    rv.send_preview(ch, env["rid"])
    ph = ch.photos[-1]
    assert ph["caption"].startswith("Tailored for Senior Product Manager at Acme: 2 bullets reworded")
    labels = [b["label"] for row in ph["buttons"] for b in row]
    assert labels == ["Show changes", "Send PDF", "Edit", "Looks good"]
    for bid in ch.all_button_ids():
        assert len(bid.encode()) <= 64 and len(bid) <= 40
        assert "Stanbic" not in bid and "jobs.example" not in bid
    st = env["state"].load()
    assert all(v["rid"] == env["rid"] for v in st["cv_shorts"].values())


def test_preview_redacts_by_default_and_toggle_shows(env):
    seen = []

    def spy(pdf, png, identity, redact=True):
        seen.append(redact)
        return fake_preview(pdf, png, identity, redact)
    rv, ch = env["make"](), FakeChannel()
    rv.preview_fn = spy
    rv.send_preview(ch, env["rid"])
    env["state"].set_prefs({"show_contact": True})
    rv.send_preview(ch, env["rid"])
    assert seen == [True, False]


def test_send_pdf_warns_once_and_show_changes(env):
    rv, ch = env["make"](), FakeChannel()
    rv.send_preview(ch, env["rid"])
    _tap(rv, ch, "pdf")
    _tap(rv, ch, "pdf")
    assert ch.texts().count(MSG_PDF_WARNING) == 1
    assert len(ch.docs) == 2 and ch.docs[0]["path"].endswith(f"cv-{env['rid']}.pdf")
    assert "48 hours" in MSG_PDF_WARNING and "Telegram's servers" in MSG_PDF_WARNING
    _tap(rv, ch, "chg")
    assert any("E1.2\nBefore: Ran profit reviews" in t for t in ch.texts())


def test_looks_good_follows_submission_policy(env):
    ch = FakeChannel()
    rv = env["make"]()
    rv.send_preview(ch, env["rid"])
    _tap(rv, ch, "ok")
    assert ch.texts()[-1].endswith("Open SponsorJobs to submit.") and ch.sent[-1]["buttons"] is None
    rv2 = env["make"](can_apply=True)
    rv2.send_preview(ch, env["rid"])
    _tap(rv2, ch, "ok")
    assert "Apply for me" in [b["label"] for row in ch.sent[-1]["buttons"] for b in row]
    _tap(rv2, ch, "apply")
    assert ch.texts()[-1] == f"Applied #{env['rid']}"


# ------------------------------------------------------------------ edits
def test_rewrite_is_confined_to_the_target_and_accept_saves_a_version(env):
    rv, ch = env["make"](), FakeChannel()
    rv.send_preview(ch, env["rid"])
    before = cvdoc.span_texts({"profile": _page(), "sections": SECTIONS})
    out = rv.handle_text(ch, "rewrite E1.2 shorter")
    assert out and "E1.2\nBefore: Owned PnL reviews" in out and MSG_EDIT_FOOTER in out
    pend = _data(env)["tg_pending"]
    after = cvdoc.span_texts(pend["doc"])
    assert [k for k in after if after[k] != before.get(k)] == ["E1.2"]
    assert len(after["E1.2"]) < len(before["E1.2"])
    labels = [b["label"] for row in ch.photos[-1]["buttons"] for b in row]
    assert labels == ["Accept", "Undo"]
    assert _data(env)["page_profile"] == _page()           # nothing final before Accept
    _tap(rv, ch, "acc")
    d = _data(env)
    assert d["version"] == 2 and [v["v"] for v in d["versions"]] == [1, 2]
    assert d["page_profile"]["experience"][0]["roles"][0]["bullets"][1] == after["E1.2"]
    assert (env["dir"] / f"cv-{env['rid']}.pdf").read_bytes().startswith(b"%PDF-1.4 fake")
    assert ch.texts()[-1].startswith("Saved as version 2 of Senior Product Manager at Acme.")
    assert ch.texts()[-1].endswith("Open SponsorJobs to submit.")      # Accept never submits
    assert ch.edits and ch.edits[-1]["buttons"] == []                  # stale buttons removed
    assert "v2 (current): rewrite E1.2 shorter" in rv.versions_text(env["rid"])
    # Undo after Accept restores version 1 (the as-tailored PDF and page).
    _tap(rv, ch, "undo")
    d = _data(env)
    assert d["version"] == 1 and d["page_profile"] == _page()
    assert (env["dir"] / f"cv-{env['rid']}.pdf").read_bytes() == b"%PDF-1.4 original"
    assert "back to version 1" in ch.texts()[-1]


def test_undo_on_a_pending_edit_discards_it(env):
    rv, ch = env["make"](), FakeChannel()
    rv.run_edit(ch, env["rid"], "rewrite E1.2 shorter")
    _tap(rv, ch, "undo")
    assert ch.texts()[-1] == MSG_DISCARDED and _data(env)["tg_pending"] == {}
    assert _data(env)["page_profile"] == _page()


class OverEditLLM(FakeLLM):
    def edit_cv_spans(self, instruction, targets, context, jd_text="", budgets=None):
        out = {k: v + " Faster." for k, v in targets.items()}
        out["E1.3"] = "Quietly rewrote a bullet nobody asked about."
        return out


def test_over_editing_model_output_is_rejected(env):
    rv, ch = env["make"](llm=OverEditLLM()), FakeChannel()
    out = rv.run_edit(ch, env["rid"], "rewrite E1.2 with more punch")
    assert "also touched E1.3" in out and "left your CV as it was" in out
    assert not _data(env).get("tg_pending")


def test_document_guard_rejects_changes_outside_targets():
    doc = {"profile": _page(), "sections": SECTIONS}
    with pytest.raises(cvdoc.EditRejected):
        cvdoc.apply_text_edits(doc, {"E1.2": "x", "E1.3": "y"}, {"E1.2"})
    out = cvdoc.apply_text_edits(doc, {"E1.2": "x"}, {"E1.2"})
    assert cvdoc.changed_spans(doc, out) == ["E1.2"]


class KafkaLLM(FakeLLM):
    def edit_cv_spans(self, instruction, targets, context, jd_text="", budgets=None):
        return {k: "Ran PnL reviews on Kafka streams for the cards business." for k in targets}


def test_fabrication_gate_rejects_an_introduced_skill_but_allows_a_typed_one(env):
    rv, ch = env["make"](llm=KafkaLLM()), FakeChannel()
    out = rv.run_edit(ch, env["rid"], "rewrite E1.2 to sound more technical")
    assert "added Kafka" in out and "Stanbic Bank doesn't show" in out
    assert not _data(env).get("tg_pending")
    out = rv.run_edit(ch, env["rid"], "use the word Kafka in E1.2")
    assert "Check: Kafka in E1.2 is from your message" in out
    assert "Kafka" in _data(env)["tg_pending"]["doc"]["profile"]["experience"][0]["roles"][0]["bullets"][1]


class FigureLLM(FakeLLM):
    def edit_cv_spans(self, instruction, targets, context, jd_text="", budgets=None):
        return {k: v.rstrip(".") + ", lifting volume 40%." for k, v in targets.items()}


def test_new_figure_from_the_model_is_flagged_but_a_typed_one_is_not(env):
    rv, ch = env["make"](llm=FigureLLM()), FakeChannel()
    out = rv.run_edit(ch, env["rid"], "rewrite E1.1 with more impact")
    assert "Check: the figure 40 in E1.1 isn't in your material." in out
    rv2 = env["make"]()
    out = rv2.run_edit(ch, env["rid"], "change E1.1 to: Led the payments roadmap across 9 markets.")
    assert "figure" not in out and "across 9 markets" in out


def test_factual_field_asks_first_then_updates_profile_on_accept(env):
    rv, ch = env["make"](), FakeChannel()
    rv.send_preview(ch, env["rid"])
    rv.handle_text(ch, "change my title at Stanbic to Product Lead")
    q = ch.sent[-1]
    assert q["text"] == ("This changes a fact on your CV: Stanbic Bank title from 'Senior Product "
                         "Manager' to 'Product Lead'. Apply to this application only, or also "
                         "update your profile?")
    assert [b["label"] for row in q["buttons"] for b in row] == [
        "This CV only", "Also update my profile", "Cancel"]
    assert not _data(env).get("tg_pending")                 # nothing applied before the confirm
    _tap(rv, ch, "fp")
    pend = _data(env)["tg_pending"]
    assert pend["doc"]["profile"]["experience"][0]["roles"][0]["title"] == "Product Lead"
    assert env["profile_updates"] == []                     # still nothing final
    _tap(rv, ch, "acc")
    assert env["profile_updates"] and env["profile_updates"][0]["new"] == "Product Lead"
    assert "Your saved profile is updated too." in ch.texts()[-1]


def test_factual_cancel_and_this_cv_only(env):
    rv, ch = env["make"](), FakeChannel()
    rv.run_edit(ch, env["rid"], "change E1 dates to Jan 2020 to Mar 2023")
    assert "Stanbic Bank dates from 'Jan 2020 to Present' to 'Jan 2020 to Mar 2023'" in ch.texts()[-1]
    _tap(rv, ch, "fx")
    assert ch.texts()[-1] == "Cancelled. Nothing changed." and not _data(env).get("tg_pending")
    rv.run_edit(ch, env["rid"], "change E1 dates to Jan 2020 to Mar 2023")
    _tap(rv, ch, "f1")
    _tap(rv, ch, "acc")
    assert env["profile_updates"] == []
    assert _data(env)["page_profile"]["experience"][0]["roles"][0]["dates"] == "Jan 2020 to Mar 2023"


def test_apply_fact_to_saved_profile_matches_by_name_not_position():
    saved = {"experience": [{"org": "Old Co", "roles": [{"title": "Intern", "dates": "2015"}]},
                            {"org": "Stanbic Bank", "roles": [
                                {"title": "Senior Product Manager", "dates": "Jan 2020 to Present"}]}]}
    doc = {"profile": _page(), "sections": SECTIONS}
    ch = cvdoc.fact_changes(cvdoc.normalize_plan(
        {"edits": [{"op": "set", "target": "E1.title", "value": "Product Lead"}]}, doc), doc)[0]
    assert cvdoc.apply_fact_to_profile(saved, ch, doc)
    assert saved["experience"][1]["roles"][0]["title"] == "Product Lead"
    assert saved["experience"][0]["roles"][0]["title"] == "Intern"


def test_structural_reorder_drop_and_add(env):
    rv, ch = env["make"](), FakeChannel()
    rv.run_edit(ch, env["rid"], "move Projects above Experience")
    assert _data(env)["tg_pending"]["doc"]["sections"] == [
        "summary", "education", "skills", "projects", "experience"]
    assert "Moved Projects above Experience." in ch.photos[-1]["caption"]
    _tap(rv, ch, "acc")
    assert _data(env)["sections"][3] == "projects"
    rv.run_edit(ch, env["rid"], "drop P2")
    assert len(_data(env)["tg_pending"]["doc"]["profile"]["projects"]) == 1
    _tap(rv, ch, "undo")
    rv.run_edit(ch, env["rid"], "drop E1.1")
    _tap(rv, ch, "acc")
    shown = "\n".join(rv.show_changes(ch, env["rid"]))
    assert "E1.1\nBefore: Ran profit reviews" in shown     # before/after stay paired after a drop
    rv.run_edit(ch, env["rid"], "add a bullet to E2: Launched a savings feature for prepaid users.")
    bl = _data(env)["tg_pending"]["doc"]["profile"]["experience"][1]["roles"][0]["bullets"]
    assert bl[-1] == "Launched a savings feature for prepaid users."


def test_unknown_id_asks_did_you_mean_and_yes_applies(env):
    rv, ch = env["make"](), FakeChannel()
    rv.run_edit(ch, env["rid"], "rewrite E1.9 shorter")
    assert ch.texts()[-1] == "Did you mean E1.3?"
    _tap(rv, ch, "yes")
    after = cvdoc.span_texts(_data(env)["tg_pending"]["doc"])
    assert after["E1.3"] != _page()["experience"][0]["roles"][0]["bullets"][2]


def test_overflow_self_heals_only_the_edited_span_or_rejects(env):
    longest = max(len(b) for e in _page()["experience"] for r in e["roles"] for b in r["bullets"])
    comp = FakeCompile(max_bullet=longest + len("\\item ") + 2)
    rv, ch = env["make"](compile_fn=comp), FakeChannel()
    long = "Led the mobile payments roadmap across three markets with SQL reporting " + "and more " * 8
    rv.run_edit(ch, env["rid"], f"change E1.1 to: {long.strip()}.")
    assert "past one page" in ch.texts()[-1]                # a typed value is never reworded
    first = comp.calls
    rv.run_edit(ch, env["rid"], "use the word stakeholders in E1.2")
    pend = _data(env).get("tg_pending")
    assert pend, ch.texts()[-1]
    after = cvdoc.span_texts(pend["doc"])
    before = cvdoc.span_texts({"profile": _page(), "sections": SECTIONS})
    assert [k for k in after if after[k] != before.get(k)] == ["E1.2"]
    assert after["E1.2"].startswith("stakeholders: ")       # the edit survived, shortened
    assert comp.calls - first >= 2                          # it overflowed, then healed


def test_layout_break_rolls_back(env):
    comp = FakeCompile(verdict=lambda pdf: Res(ok=False, pages=None, pdf=None))
    rv, ch = env["make"](compile_fn=comp), FakeChannel()
    out = rv.run_edit(ch, env["rid"], "rewrite E1.2 shorter")
    assert "broke the layout" in out and not _data(env).get("tg_pending")


def test_new_overfull_line_is_rejected_but_baseline_is_tolerated(env):
    comp = FakeCompile(verdict=lambda pdf: Res(overfull=1, pdf=pdf))
    rv, ch = env["make"](compile_fn=comp), FakeChannel()
    rv.run_edit(ch, env["rid"], "move Projects above Experience")
    assert _data(env).get("tg_pending")                      # the original had 1 overfull too
    comp2 = FakeCompile()
    calls = {"n": 0}

    def verdict(pdf):
        calls["n"] += 1
        return Res(overfull=2 if "tgedit" in pdf.name else 0, pdf=pdf)
    comp2.verdict = verdict
    rv2, ch2 = env["make"](compile_fn=comp2), FakeChannel()
    rv2.state.update(lambda d: None)
    recs = CVRecords(env["db"])
    recs.merge_data(env["rid"], {"tg_baseline_overfull": 0})
    recs.close()
    out = rv2.run_edit(ch2, env["rid"], "move Projects above Experience")
    assert "right margin" in out


def test_fact_question_wording():
    assert fact_question([{"field": "company", "label": "Stanbic Bank", "old": "Stanbic Bank",
                           "new": "Stanbic IBTC"}]) == (
        "This changes a fact on your CV: employer name from 'Stanbic Bank' to 'Stanbic IBTC'. "
        "Apply to this application only, or also update your profile?")


# ------------------------------------------------------------------ hub routing
class Cmds:
    def __init__(self):
        self.got = []

    def handle_command(self, text):
        self.got.append(text)
        return "chat reply"


class ReadyActions:
    def __init__(self, rid):
        self._rid = rid

    def take_ready(self):
        rid, self._rid = self._rid, None
        return rid


class Engine:
    def handle_button(self, data, actions):
        return "Ready: Senior Product Manager at Acme is in your review queue (#1, 70% JD match)."


def test_hub_sends_preview_after_a_telegram_tailoring_and_routes_edits(env):
    rv, ch = env["make"](), FakeChannel()
    cmds = Cmds()
    hub = NotifyHub(env["state"], Engine(), cmds, job_actions=ReadyActions(env["rid"]), review=rv)
    hub.handle_event(ch, {"type": "button", "data": "job:queue:abc"})
    assert ch.texts()[-1].startswith("Ready:") and ch.photos[-1]["caption"].startswith("Tailored for")
    hub.handle_event(ch, {"type": "text", "text": "what jobs should I look at?"})
    assert cmds.got == ["what jobs should I look at?"]       # a chat question is not an edit
    hub.handle_event(ch, {"type": "text", "text": "reword the summary shorter"})
    assert _data(env)["tg_pending"]["report"][0].startswith("S\nBefore:")
    btn = [b["id"] for row in ch.photos[-1]["buttons"] for b in row if b["id"].startswith("cv:acc:")][0]
    hub.handle_event(ch, {"type": "button", "data": btn})
    assert ch.acks[-1] == "Saved" and _data(env)["version"] == 2
    hub.handle_event(ch, {"type": "text", "text": f"/versions {env['rid']}"})
    assert ch.texts()[-1].startswith("Versions of Senior Product Manager at Acme")


def test_edit_button_then_next_message_is_the_instruction(env):
    rv, ch = env["make"](), FakeChannel()
    rv.send_preview(ch, env["rid"])
    _tap(rv, ch, "edit")
    assert ch.texts()[-1].startswith("Tell me what to change")
    rv.handle_text(ch, "drop P2")
    assert len(_data(env)["tg_pending"]["doc"]["profile"]["projects"]) == 1


def test_show_me_resends_the_preview(env):
    rv, ch = env["make"](), FakeChannel()
    rv.send_preview(ch, env["rid"])
    n = len(ch.photos)
    assert rv.handle_text(ch, "show me") == "preview" and len(ch.photos) == n + 1


# ------------------------------------------------------------------ channels: protect_content
def test_own_bot_media_is_protected_and_edits_buttons():
    from notify.channel import OwnBotChannel
    from notify.telegram import TelegramBot
    calls = []

    def http(url, payload=None):
        calls.append((url.rsplit("/", 1)[-1], dict(payload or {})))
        return {"ok": True, "result": {"message_id": 9}}
    ch = OwnBotChannel(TelegramBot("t", "42", http=http), lambda: 0, lambda o: None)
    ch.send_photo("/x.png", "cap", [[{"id": "cv:acc:abc", "label": "Accept"}]])
    ch.send_document("/x.pdf", "cap")
    ch.edit_text(9, buttons=[])
    (m1, p1), (m2, p2), (m3, p3) = calls
    assert (m1, m2, m3) == ("sendPhoto", "sendDocument", "editMessageReplyMarkup")
    assert p1["protect_content"] is True and p2["protect_content"] is True
    assert p3["reply_markup"] == {"inline_keyboard": []}


def test_official_channel_posts_media_and_waits_out_a_short_429():
    from notify.channel import OfficialBotChannel
    posted, slept = [], []
    answers = [(429, {"error": "rate_limited", "retry_after": 2}), (200, {"ok": True, "message_id": 5})]

    def post_media(path, fields, file_path, ctype):
        posted.append((path, fields, ctype))
        return answers.pop(0)
    ch = OfficialBotChannel(lambda p: (200, {}), lambda p, b: (200, {"ok": True}), lambda: "",
                            lambda c: None, post_media=post_media, sleep=slept.append)
    out = ch.send_photo("/x.png", "cap", [[{"id": "cv:ok:abc", "label": "Looks good"}]])
    assert out["message_id"] == 5 and slept == [2.0]
    assert posted[0][0] == "/notify/telegram/send_media" and posted[0][1]["kind"] == "photo"
    assert json.loads(posted[0][1]["buttons"]) == [[{"id": "cv:ok:abc", "label": "Looks good"}]]


# ------------------------------------------------------------------ broker relay: send_media / edit
SECRET = "s3cret-webhook"
WH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


class FakeTG:
    def __init__(self):
        self.calls, self.media = [], []

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        return {"message_id": 7} if method == "sendMessage" else {}

    def upload(self, method, fields, field, filename, data, ctype):
        self.media.append({"method": method, "fields": fields, "field": field,
                           "size": len(data), "ctype": ctype})
        return {"message_id": 77}


def _relay_app(media=True, clock=None):
    from backend.accounts import InMemoryAccountStore
    from backend.broker import create_app
    from backend.metering import InMemoryStore, Meter
    from backend.telegram_relay import InMemoryTelegramStore, TelegramRelay
    tg = FakeTG()
    store = InMemoryTelegramStore()
    t = {"now": 1_800_000_000.0}
    relay = TelegramRelay(store, tg, bot_username="SponsorJobsBot", webhook_secret=SECRET,
                          now=lambda: t["now"], media_transport=tg.upload if media else None)
    app = create_app(meter=Meter(InMemoryStore()), period_fn=lambda: "2026-10",
                     accounts=InMemoryAccountStore(), telegram=relay)
    app.config.update(TESTING=True)
    c = app.test_client()
    tok = c.post("/account/register").get_json()["token"]
    H = {"Authorization": f"Bearer {tok}"}
    code = c.post("/notify/telegram/link", headers=H).get_json()["code"]
    c.post("/telegram/webhook", headers=WH, json={"update_id": 1, "message": {
        "message_id": 1, "text": f"/start {code}", "chat": {"id": 555, "type": "private"},
        "from": {"id": 555, "username": "ama"}}})
    return c, H, tg, store, t


def _media(c, H, kind="photo", size=1000, caption="Tailored for PM at Acme.", buttons=None):
    data = {"kind": kind, "caption": caption,
            "file": (io.BytesIO(b"\x89PNG" + b"0" * size), "cv.png")}
    if buttons is not None:
        data["buttons"] = json.dumps(buttons)
    return c.post("/notify/telegram/send_media", headers=H, data=data,
                  content_type="multipart/form-data")


def test_relay_send_media_passes_through_protected_and_stores_nothing():
    c, H, tg, store, t = _relay_app()
    events_before = list(store._events)
    r = _media(c, H, buttons=[[{"id": "cv:acc:abcd1234", "label": "Accept"}]])
    assert r.status_code == 200 and r.get_json() == {"ok": True, "message_id": 77}
    m = tg.media[-1]
    assert m["method"] == "sendPhoto" and m["field"] == "photo"
    assert m["fields"]["protect_content"] == "true" and m["fields"]["chat_id"] == "555"
    assert json.loads(m["fields"]["reply_markup"])["inline_keyboard"][0][0]["callback_data"] == "cv:acc:abcd1234"
    assert store._events == events_before                    # nothing about the file is kept
    assert not any(isinstance(v, (bytes, bytearray)) for v in vars(store).values())
    t["now"] += 5
    r = _media(c, H, kind="document")
    assert r.status_code == 200 and tg.media[-1]["method"] == "sendDocument"
    assert tg.media[-1]["fields"]["protect_content"] == "true"


def test_relay_send_media_limits():
    from backend.telegram_relay import MAX_PHOTO_BYTES
    c, H, tg, store, t = _relay_app()
    assert _media(c, H, kind="video").status_code == 400
    assert _media(c, H, caption="x" * 1025).status_code == 400
    assert _media(c, H, buttons=[[{"id": "bad id!", "label": "x"}]]).status_code == 400
    assert _media(c, H, size=MAX_PHOTO_BYTES + 1).status_code == 413
    assert tg.media == []                                   # bad requests use no quota
    assert _media(c, H).status_code == 200
    r = _media(c, H)                                        # within 3 s of the last send
    assert r.status_code == 429 and r.get_json()["error"] == "rate_limited"
    from backend.telegram_relay import SEND_PER_DAY
    for _ in range(SEND_PER_DAY + 10):                      # media counts toward the daily cap
        t["now"] += 4
        last = _media(c, H)
    assert last.status_code == 429
    assert len(tg.media) == SEND_PER_DAY
    assert c.post("/notify/telegram/send_media", data={"kind": "photo"},
                  content_type="multipart/form-data").status_code == 401


def test_relay_media_unconfigured_is_503_and_edit_route():
    c, H, tg, store, t = _relay_app(media=False)
    assert _media(c, H).status_code == 503
    r = c.post("/notify/telegram/edit", headers=H, json={"message_id": 7, "buttons": []})
    assert r.status_code == 200 and tg.calls[-1][0] == "editMessageReplyMarkup"
    assert tg.calls[-1][1]["reply_markup"] == {"inline_keyboard": []}
    t["now"] += 2
    r = c.post("/notify/telegram/edit", headers=H, json={"message_id": 7, "text": "Saved."})
    assert r.status_code == 200 and tg.calls[-1][0] == "editMessageText"
    t["now"] += 2
    r = c.post("/notify/telegram/edit", headers=H, json={"message_id": 7, "caption": "c"})
    assert tg.calls[-1][0] == "editMessageCaption"
    assert c.post("/notify/telegram/edit", headers=H, json={"message_id": 7}).status_code == 400
    assert c.post("/notify/telegram/edit", headers=H, json={"text": "x"}).status_code == 400


def test_relay_send_accepts_a_long_show_changes_chunk():
    c, H, tg, store, t = _relay_app()
    assert c.post("/notify/telegram/send", headers=H, json={"text": "x" * 4000}).status_code == 200
    t["now"] += 5
    assert c.post("/notify/telegram/send", headers=H, json={"text": "x" * 4097}).status_code == 400


# ------------------------------------------------------------------ real page: the redaction bar
@requires_latex
def test_redaction_bar_covers_the_contact_band(tmp_path):
    from PIL import Image

    from notify.redact import render_preview
    from tailoring.assembler import assemble_cv
    tex = (ROOT / "config" / "resume_shetty.tex").read_text()
    r = assemble_cv(tex, BASE, JD, FakeLLM(), tmp_path, jobname="red", tailor=False)
    assert r.ok
    out = render_preview(r.pdf_path, tmp_path / "p.png", BASE["identity"], redact=True)
    assert out["redacted"] and out["band"]
    y0, y1 = out["band"]
    img = Image.open(out["path"]).convert("L")
    mid = (y0 + y1) // 2
    row = [img.getpixel((x, mid)) for x in range(0, img.width, 7)]
    assert max(row) < 40                                     # a solid dark bar across the page
    assert y0 > 20                                           # the name above stays visible
    name_rows = [img.getpixel((x, y)) for y in range(5, y0 - 2) for x in range(0, img.width, 5)]
    assert min(name_rows) < 80 and max(name_rows) > 200     # ink of the name on white paper
    plain = render_preview(r.pdf_path, tmp_path / "q.png", BASE["identity"], redact=False)
    img2 = Image.open(plain["path"]).convert("L")
    assert max(img2.getpixel((x, mid)) for x in range(0, img2.width, 7)) > 200


# ------------------------------------------------------------------ app wiring
def test_app_contact_toggle_and_review_wiring(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path)
    c = app.app.test_client()
    assert c.get("/api/notify/settings").get_json()["prefs"]["show_contact"] is False
    assert c.post("/api/notify/settings", json={"show_contact": True}).get_json()["prefs"]["show_contact"] is True
    rv = app._cv_review()
    assert rv.workdir == tmp_path and rv.state.prefs()["show_contact"] is True
    acts = app._JobAlertActions()
    acts._ready = 12
    assert acts.take_ready() == 12 and acts.take_ready() is None
    hub = app._notify_hub()
    assert hub.review is not None
