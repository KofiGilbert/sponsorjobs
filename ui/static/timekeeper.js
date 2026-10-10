// The live interview's timekeeper (Kofi, 2026-10-09).
//
// A real interviewer with a day of candidates keeps time. They let an answer run its course,
// thank the person and move on, and close on time. The AI interviewer cannot see a clock, so the
// app is its watch: it hears who is speaking (Tavus events) and passes TIME NOTES to it. Nobody
// is cut off by a timer. At the answer budget the interviewer moves on at the candidate's next
// pause; only if no pause comes does it step in politely, the way a person would. At time-up it
// closes at the next pause, and the call ends a moment after its goodbye.
//
// Pure: everything outside (sending notes, ending the call, the clock, timers) is passed in, so
// tests/timekeeper.test.mjs drives it with a simulated clock.
(function (root) {
  function create(t, io) {
    // awaitingAnswer: the interviewer has just spoken (usually a question) and the candidate has
    // not started answering. A few seconds of silence then is thinking, not "finished", so the
    // "any questions?" turn waits up to think_grace (10 s, what a person would allow) for the
    // answer to begin; once it begins, it is heard out for up to answer_grace (45 s).
    const thinkGrace = t.think_grace || 10;
    const answerGrace = t.answer_grace || 45;
    const tk = { t0: io.now(), userTalking: false, palTalking: false, afterPal: null, awaitingAnswer: false, answerStart: 0, nudged: false, steppedIn: false,
                 waitingPause: null, lastQ: false, candQ: false, closing: false, closeSentAt: 0,
                 ended: false };
    const elapsed = () => (io.now() - tk.t0) / 1000;
    const answerElapsed = () => (tk.answerStart ? (io.now() - tk.answerStart) / 1000 : 0);
    const finish = () => { if (!tk.ended) { tk.ended = true; io.end(); } };
    // Ask at the candidate's next pause, or after `wait` seconds if no pause comes. A newer request
    // replaces an older one still waiting (closing outranks "any questions?") and keeps the
    // earlier deadline, so nothing waits longer than asked.
    // `patient`: also wait for an answer the candidate is still thinking about (the "any
    // questions?" turn). The close is never patient that way: it only waits for a pause in speech.
    const atNextPause = (fn, wait, patient) => {
      const thinking = patient && tk.awaitingAnswer;
      // The interviewer is mid-sentence: a note now makes it cut itself off and restart ("That"
      // then the close, 2026-10-10). Wait for it to finish; its stopped_speaking runs this.
      if (tk.palTalking) { tk.afterPal = fn; return; }
      if (!tk.userTalking && !thinking) { tk.waitingPause = null; fn(); return; }
      const until = io.now() + (tk.userTalking ? wait : thinkGrace) * 1000;
      tk.waitingPause = { fn, thinking: !tk.userTalking,
                          until: tk.waitingPause ? Math.min(tk.waitingPause.until, until) : until };
    };
    const runPending = () => { const p = tk.waitingPause; tk.waitingPause = null; if (p) p.fn(); };
    const isPal = (r) => r === "pal" || r === "replica";

    function onEvent(ev) {
      const type = ev && ev.event_type, role = ev && ev.properties && ev.properties.role;
      if (type === "conversation.started_speaking" && role === "user") {
        tk.userTalking = true;
        tk.awaitingAnswer = false;
        // They began answering a question we were waiting on: hear the answer out, then run the
        // request at their pause, or answer_grace seconds in at the latest.
        if (tk.waitingPause && tk.waitingPause.thinking) {
          tk.waitingPause.thinking = false;
          tk.waitingPause.until = io.now() + answerGrace * 1000;
        }
        if (!tk.answerStart) tk.answerStart = io.now();
      } else if (type === "conversation.stopped_speaking" && role === "user") {
        tk.userTalking = false;
        runPending();
      } else if (type === "conversation.started_speaking" && isPal(role)) {
        // The interviewer took a turn: the candidate's next words start a new answer.
        tk.palTalking = true;
        tk.answerStart = 0; tk.nudged = false; tk.steppedIn = false;
        tk.awaitingAnswer = !tk.closeSentAt;
      } else if (type === "conversation.stopped_speaking" && isPal(role)) {
        tk.palTalking = false;
        if (tk.afterPal) { const f = tk.afterPal; tk.afterPal = null; f(); }
        // After the goodbye: leave a short moment, then end the call.
        if (tk.closeSentAt && io.now() > tk.closeSentAt) io.later(finish, 3000);
      }
    }

    function tick() {
      const now = elapsed(), a = answerElapsed();
      if (tk.waitingPause && io.now() >= tk.waitingPause.until) runPending();
      if (!tk.closing && a >= t.answer_nudge && !tk.nudged) {
        tk.nudged = true;
        io.note("This answer has run long. When the candidate finishes this thought, thank them briefly and ask your next question.");
      }
      if (!tk.closing && a >= t.answer_step_in && !tk.steppedIn) {
        tk.steppedIn = true;
        atNextPause(() => io.say("Move on now. Thank the candidate for the answer and ask your next question. If they were still speaking, say: Sorry to jump in, that's really helpful, and I want to make sure we cover everything."), t.pause_wait);
      }
      if (!tk.lastQ && now >= t.last_question) {
        tk.lastQ = true;
        io.note("About two minutes remain. Make your next question the last main question, and say so.");
      }
      if (!tk.candQ && now >= t.candidate_questions) {
        tk.candQ = true;
        atNextPause(() => io.say("Time is nearly up. Thank them for the answer and ask whether they have one question for you."), t.pause_wait, true);
      }
      if (!tk.closing && now >= t.total) {
        tk.closing = true;
        atNextPause(() => {
          tk.closeSentAt = io.now();
          io.say("The time is up. Close the interview now: thank the candidate warmly, tell them their feedback report is ready, and say goodbye.");
        }, t.close_wait);
      }
      // Backstop only. Normally the call ends just after the goodbye (above). Never sooner than
      // 20 seconds after the close was asked for, so the goodbye itself is never cut.
      const closeAge = tk.closeSentAt ? (io.now() - tk.closeSentAt) / 1000 : 0;
      if (!tk.ended && now >= t.hard_end && (closeAge >= 20 || now >= t.hard_end + 25)) finish();
      return now;
    }
    return { onEvent, tick, state: tk };
  }
  root.SJTimekeeper = { create };
})(typeof window !== "undefined" ? window : globalThis);
