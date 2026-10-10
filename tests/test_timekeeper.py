"""The live interview's timekeeper (2026-10-09), driven by a simulated clock in node.

Kofi's rule: the interviewer keeps time like a real one. It lets an answer run, moves on at the
next pause once the answer budget is spent, steps in politely only if no pause comes, warns
before the last question, asks for the candidate's question, and closes on time without ever
cutting the goodbye. These tests play scripted interviews at the real 15-minute timing."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
TK = Path("ui/static/timekeeper.js").resolve()
TIMING = {"total": 900, "answer_nudge": 150, "answer_step_in": 195, "pause_wait": 15,
          "last_question": 780, "candidate_questions": 840, "close_wait": 20, "hard_end": 930,
          "think_grace": 10, "answer_grace": 45}
# (These marks are the 15-minute set; the timekeeper only ever reads them from the app, so the
#  logic is the same at 20 minutes. The app's own 20-minute marks are tested in test_round2_cvi.)

HARNESS = r"""
require(process.argv[2]);
const t = JSON.parse(process.argv[3]), script = JSON.parse(process.argv[4]);
let clock = 0; const log = []; const timers = [];
const io = { now: () => clock, note: (x) => log.push([clock/1000, "note", x]),
             say: (x) => log.push([clock/1000, "say", x]), end: () => log.push([clock/1000, "end", ""]),
             later: (fn, ms) => timers.push([clock + ms, fn]) };
const tk = globalThis.SJTimekeeper.create(t, io);
const ev = (type, role) => tk.onEvent({ event_type: "conversation." + type, properties: { role } });
const at = {}; for (const [sec, type, role] of script) (at[sec] = at[sec] || []).push([type, role]);
for (let s = 0; s <= 1000; s++) {
  clock = s * 1000;
  for (const [type, role] of (at[s] || [])) ev(type, role);
  for (let i = timers.length - 1; i >= 0; i--) if (timers[i][0] <= clock) { const f = timers[i][1]; timers.splice(i, 1); f(); }
  tk.tick();
  if (log.some(l => l[1] === "end")) break;
}
console.log(JSON.stringify(log));
"""


def run(script):
    h = Path(__file__).with_name("_tk_harness.js")
    h.write_text(HARNESS)
    try:
        out = subprocess.run(["node", str(h), str(TK), json.dumps(TIMING), json.dumps(script)],
                             capture_output=True, text=True, timeout=60)
    finally:
        h.unlink(missing_ok=True)
    assert out.returncode == 0, out.stderr
    return [(int(s), kind, text) for s, kind, text in json.loads(out.stdout)]


def turn(start, end, role):
    return [[start, "started_speaking", role], [end, "stopped_speaking", role]]


def test_a_short_answer_gets_no_notes():
    log = run(turn(5, 10, "pal") + turn(12, 100, "user") + turn(102, 108, "pal"))
    assert all(s >= 780 for s, _, _ in log)     # nothing until the clock's own marks


def test_a_long_answer_is_nudged_then_moved_on_at_the_next_pause():
    # The answer starts at 12 s; the candidate first pauses at 214 s, past the step-in mark (207 s).
    log = run(turn(5, 10, "pal") + [[12, "started_speaking", "user"], [214, "stopped_speaking", "user"]])
    nudge = [s for s, k, x in log if k == "note" and "run long" in x]
    move = [s for s, k, x in log if k == "say" and "Move on now" in x]
    assert nudge == [162]                       # 150 s into the answer, a quiet note
    assert move == [214]                        # at the pause, never mid-sentence


def test_with_no_pause_the_interviewer_steps_in_politely_after_a_short_wait():
    log = run(turn(5, 10, "pal") + [[12, "started_speaking", "user"]])
    move = [(s, x) for s, k, x in log if k == "say" and "Move on now" in x]
    assert move and move[0][0] == 12 + 195 + 15     # waited 15 s for a pause that never came
    assert "Sorry to jump in" in move[0][1]


def test_the_ending_warns_asks_for_questions_and_closes_without_cutting_the_goodbye():
    script = (turn(5, 10, "pal") + turn(12, 150, "user")
              + turn(152, 158, "pal") + turn(160, 770, "user")        # talks across the 780 s mark
              + turn(772, 778, "pal") + turn(790, 850, "user")        # mid-answer at 840 s
              + turn(852, 858, "pal") + turn(860, 905, "user")        # their own question, past 900 s
              + turn(908, 916, "pal"))                                # the goodbye
    log = run(script)
    assert [s for s, k, x in log if "last main question" in x] == [780]
    assert [s for s, k, x in log if "one question for you" in x] == [850]   # at the pause after 840
    assert [s for s, k, x in log if "Close the interview" in x] == [905]   # at the pause after 900
    assert [s for s, k, _ in log if k == "end"] == [919]                   # 3 s after the goodbye


def test_the_backstop_never_cuts_a_goodbye_that_has_just_been_asked_for():
    # The candidate talks right through time-up and never pauses; the interviewer never speaks.
    log = run(turn(5, 10, "pal") + [[860, "started_speaking", "user"]])
    close = [s for s, k, x in log if "Close the interview" in x]
    end = [s for s, k, _ in log if k == "end"]
    assert close == [920]                       # 900 s + 20 s waiting for a pause
    assert end == [940]                         # at least 20 s after the close request


def test_a_question_the_candidate_is_still_thinking_about_is_not_skipped():
    """Kofi's test call, 2026-10-10: the interviewer asked question 3, he paused to think, and the
    "time is nearly up" note went out into the silence, so his answer was skipped. A few seconds
    of silence after a question is thinking: the wrap-up waits for the answer and goes at its
    pause."""
    script = (turn(5, 10, "pal") + turn(12, 700, "user")
              + turn(830, 836, "pal")                                  # a question just before 840
              + turn(842, 880, "user"))                                # 6 s thinking, then answers
    log = run(script)
    assert [s for s, k, x in log if "one question for you" in x] == [880]   # after the answer


def test_no_interviewer_waits_long_in_silence():
    """Who waits 45 seconds for an answer? (Kofi, 2026-10-10). Ten seconds of silence after a
    question, then the interview moves on."""
    log = run(turn(5, 10, "pal") + turn(12, 700, "user") + turn(830, 836, "pal"))
    ask = [s for s, k, x in log if "one question for you" in x]
    close = [s for s, k, x in log if "Close the interview" in x]
    assert ask == [850]                         # 840 + 10 s of silence
    assert close == [900]                       # nobody is speaking at 15:00: close right away
    assert [s for s, k, _ in log if k == "end"] == [930]   # the backstop at 15:30 (no goodbye was heard)


def test_an_answer_that_has_begun_is_heard_out_but_not_forever():
    log = run(turn(5, 10, "pal") + turn(12, 700, "user") + turn(830, 836, "pal")
              + [[845, "started_speaking", "user"]])           # starts at 845 and never pauses
    assert [s for s, k, x in log if "one question for you" in x] == [890]   # 45 s into the answer


def test_a_note_never_makes_the_interviewer_cut_itself_off():
    """Kofi's second test call: the close arrived while the interviewer was answering his question,
    and it stopped mid-word ("That") to start again. A request waits until it finishes speaking."""
    log = run(turn(5, 10, "pal") + turn(12, 700, "user") + turn(830, 836, "pal") + turn(838, 880, "user")
              + turn(895, 912, "pal"))                         # still speaking at 900
    assert [s for s, k, x in log if "Close the interview" in x] == [912]
