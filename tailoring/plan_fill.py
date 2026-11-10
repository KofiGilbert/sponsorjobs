"""Plan, then write, the Experience section (Kofi, 2026-11-10).

Filling a page bullet by bullet (split, write, polish, refill) kept trading one rule for
another: a page at 95% with stub last lines, or full-width lines on a page at 88%. This plans
the whole section first, from the measured page, and then writes it in ONE model call:

1. PLAN (deterministic, no model). From the compiled page: the lines the Experience bullets
   must fill so the page reaches the fill goal and stays on one page, bullet counts in a
   strict staircase (oldest role at least ``OLDEST_FLOOR``, each newer role one more, the top
   about 5 to 6), and a target of 1 or 2 printed lines per bullet (2-line bullets go to the
   newest roles first) whose total is exactly those lines. Each bullet gets a character window
   from the measured line width.
2. WRITE. One call for all roles (``llm.write_experience``): per role its title, company,
   saved bullets and uploaded-resume text, the job, and the plan. Every bullet is checked: no
   number, employer, product, client, degree or certification the person's material does not
   state, no invented outcome claim, no filler, no padding (its own employer's name, a stock
   tail, a phrase another bullet repeats), and distinct from the role's other bullets.
3. VERIFY. Compile and measure. Every bullet that misses its line count, stops short or failed
   a check gets ONE retry, in a single call for all of them. If the page is then over one page
   or short of the goal, the plan moves by one line and that bullet is written once more. A
   page that ends worse than it started is never shipped: the section goes back as it was.
"""
from __future__ import annotations

import math
import re

# The page aims here (between the fill goal and the most a page may hold).
PLAN_TARGET = 0.96
# A bullet's last line reaches this share of the line, at most the full line.
WINDOW_LO, WINDOW_HI = 0.93, 1.0
# The top role's count stays at most here unless the lines truly need more.
TOP_MOST = 6
# The adjusting pass changes at most this many bullets' line targets.
ADJUST_MOST = 4

# An outcome a bullet asserts that the person's material must state to be said: "with none
# skipped or lost" (a real write, 2026-11-10), "zero errors", "100% accuracy", "never missed".
_ABSOLUTE = re.compile(
    r"\b(?:none|nothing|zero|no)\s+(?:\w+\s+){0,2}(?:skipped|lost|missed|dropped|errors?|"
    r"defects?|downtime|incidents?|failures?|complaints?)\b"
    r"|\bwith(?:out)?\s+(?:any|a\s+single)\b|\bevery\s+(?:single|one)\b|\b100\s*(?:%|percent)"
    r"|\bnever\s+(?:missed|failed|late|lost)\b|\bflawless(?:ly)?\b|\bguarantee[ds]?\b"
    r"|\brecord[- ](?:high|low|year|setting|breaking|number)\b"
    r"|\b(?:best[- ]ever|first[- ]ever|industry[- ]leading)\b", re.I)


def invented_claims(new: str, source: str) -> list[str]:
    """Absolute outcome claims in ``new`` that ``source`` (the person's own words) does not
    make."""
    src = str(source or "").lower()
    return [m.group(0) for m in _ABSOLUTE.finditer(str(new or ""))
            if m.group(0).lower() not in src]


def plan_counts(n_roles: int, lines: int, floor: int = 2) -> list[int]:
    """Bullet counts, newest role first, in a strict staircase that the given number of
    printed ``lines`` can carry with 1- and 2-line bullets: the base is the oldest at ``floor``
    and each newer role one more; the whole staircase steps up by one (top at most
    ``TOP_MOST``) while the lines would need most bullets at two lines."""
    if n_roles <= 0:
        return []
    counts = [floor + (n_roles - 1 - i) for i in range(n_roles)]
    if n_roles == 1:
        counts = [max(3, floor)]
    while sum(counts) * 1.75 < lines and counts[0] < TOP_MOST:
        counts = [c + 1 for c in counts]
    return counts


def plan_lines(counts: list[int], lines: int) -> list[list[int]]:
    """A 1 or 2 line target for every bullet, newest role first, so the targets add up to
    ``lines`` (as near as the counts allow): the 2-line bullets go to the newest roles first."""
    total = sum(counts)
    twos = max(0, min(total, lines - total))
    out = []
    for c in counts:
        row = []
        for _ in range(c):
            row.append(2 if twos > 0 else 1)
            twos -= 1 if twos > 0 else 0
        out.append(row)
    return out


def windows(targets: list[list[int]], cpl: float) -> list[list[tuple[int, int, int]]]:
    """(lo, hi, lines) characters per bullet: one line is 93 to 100% of the line; two lines is
    the full first line and 93 to 100% of the second."""
    return [[(int(cpl * (n - 1 + WINDOW_LO)), int(cpl * (n - 1 + WINDOW_HI)), n) for n in row]
            for row in targets]


def line_share(result, LF) -> tuple[float, float]:
    """(one printed line's share of the text block, the extra space between two bullets as a
    share of it), measured on the page: the line pitch and the bullet gap over the height the
    page's text spans, scaled by the measured fill. ``linefit.LINE_SHARE`` and no gap when the
    page gives no reading."""
    try:
        lines = LF.pdf_lines(result.pdf_path)
    except Exception:                                        # noqa: BLE001
        lines = []
    if len(lines) < 10 or not result.fill_ratio:
        return LF.LINE_SHARE, 0.0
    lines = sorted(lines, key=lambda ln: ln.y)
    inside, between = [], []
    for prev, ln in zip(lines, lines[1:]):
        dy = abs(ln.y - prev.y)
        if dy <= 1:
            continue
        starts = ln.text.lstrip().startswith(("\u2022", "•"))
        prev_bullet = prev.text.lstrip().startswith(("\u2022", "•")) or abs(prev.x0 - ln.x0) > 3
        if not starts and abs(ln.x0 - prev.text_left) < 3:
            inside.append(dy)                                # a bullet's own next line
        elif starts and prev_bullet:
            between.append(dy)                               # the next bullet
    inside.sort()
    between.sort()
    pitch = inside[len(inside) // 2] if inside else 0.0
    if not pitch:
        return LF.LINE_SHARE, 0.0
    span = abs(lines[-1].y - lines[0].y) + pitch
    unit = result.fill_ratio / span
    share = pitch * unit
    gap = max(0.0, (between[len(between) // 2] - pitch) * unit) if between else 0.0
    if not 0.008 <= share <= 0.03:
        return LF.LINE_SHARE, 0.0
    return share, gap


def lines_of(it, text: str, cpl: float) -> int:
    """The printed lines of a bullet: as measured, unless the measure matched lines that are
    not its own (more lines than its text can fill), then from its length."""
    est = max(1, math.ceil(len(text) / cpl - 0.1)) if cpl else 1
    if it is None or not getattr(it, "found", False):
        return est
    return it.lines if it.lines <= est + 1 else est


def chars_per_line(rep) -> float:
    """Characters one full bullet line holds, from the measured bullets (the median)."""
    est = sorted(len(i.text) / i.total for i in rep.bullets if i.found and i.total >= 0.7)
    return est[len(est) // 2] if est else 0.0


def _log(**rec):
    from .assembler import _fill_log
    _fill_log(**rec)


def run_plan(preamble: str, used: dict, result, *, llm, jd_text: str, source: dict,
             material=(), gate_material=(), sections=None, headings=None, workdir=None,
             jobname: str = "cv", origins: dict | None = None, fill_goal: float = 0.95,
             max_fill: float = 0.97):
    """Plan the Experience bullets from the measured page, write them in one call, verify,
    retry once, adjust once. Returns (result, added): the page's compile result and one record
    per bullet written. The section is left as it was when the plan cannot make a clean page."""
    import copy

    from . import assembler as A
    from . import linefit as LF
    from .compiler import compile_tex
    from .fact_gate import figures, known_material, new_claims, source_figures
    from .filler import fact_stems, filler_added, hollow, role_padding, same_fact, tacked_on_terms

    origins = origins if origins is not None else {}
    roles = [(k, e, r) for k, e, r in sorted(A.ranked_roles(used), key=lambda x: x[0])
             if not (r.get("_lead") or e.get("_lead"))]
    if not roles or not (result and result.ok and result.pages == 1 and result.fill_ratio):
        return result, []
    base_overfull = result.overfull_count
    rep = LF.measure(getattr(result, "pdf_path", None), used)
    cpl = chars_per_line(rep)
    if not cpl:
        return result, []
    found = {i.text: i for i in rep.bullets if i.found}
    now_lines = 0
    unfound = 0
    for _k, _e, r in roles:
        for b in r.get("bullets") or []:
            it = found.get(str(b))
            unfound += it is None
            now_lines += lines_of(it, str(b), cpl)
    _log(step="plan-measure", label="experience", n=unfound, fill=result.fill_ratio,
         why=[(str(b)[:30], lines_of(found.get(str(b)), str(b), cpl))
              for _k, _e, r in roles for b in r.get("bullets") or []])
    share, gap = line_share(result, LF)
    now_n = sum(len(r.get("bullets") or []) for _k, _e, r in roles)
    room = PLAN_TARGET - result.fill_ratio + now_lines * share + now_n * gap
    lines = round(room / share)
    counts = plan_counts(len(roles), lines, A.OLDEST_FLOOR)
    # Each bullet also takes the space between bullets: the lines are what is left.
    lines = round((room - sum(counts) * gap) / share)
    counts = plan_counts(len(roles), lines, A.OLDEST_FLOOR)
    targets = plan_lines(counts, lines)
    _log(step="plan", label="experience", n=counts, why=f"lines {lines} (now {now_lines}), "
         f"line share {share:.4f}, gap {gap:.4f}, {cpl:.0f} characters a line, targets {targets}",
         fill=result.fill_ratio)

    src_roles = {A._role_key(e, r): r for e, r in A._experience_roles(source)}
    known = known_material(*gate_material, jd_text)
    figs = source_figures(*gate_material)
    info = []
    for (_k, e, r), row in zip(roles, targets):
        org = str(e.get("org") or e.get("company") or "")
        saved = [str(x) for x in (src_roles.get(A._role_key(e, r)) or {}).get("bullets") or []]
        saved = saved or [str(x) for x in r.get("bullets") or []]
        notes = "\n".join(A._role_notes_for(material, org))
        info.append({"label": A.entry_label(e, r), "title": str(r.get("title") or ""),
                     "company": org, "dates": str(r.get("dates") or e.get("dates") or ""),
                     "tense": A._role_tense(r, e, saved), "saved": saved, "notes": notes,
                     "targets": list(row)})

    def problems(i: int, text: str, siblings) -> list[str]:
        ro = info[i]
        own = "\n".join(ro["saved"] + [ro["notes"]])
        bad = sorted(figures(text) - figs)
        bad += new_claims(text, known=known)
        bad += invented_claims(text, own)
        if hollow(text) or filler_added("", text):
            bad.append("filler")
        bad += tacked_on_terms(own, text, count=False)
        bad += role_padding(text, siblings, ro["company"], "",
                            context=" ".join([ro["title"], ro["company"]]), allowed=own)
        if any(same_fact(text, s) for s in siblings):
            bad.append("repeats another bullet")
        return bad

    def ask(current=None, only=None, feedback=None):
        for ro in info:
            ro["windows"] = windows([ro["targets"]], cpl)[0]
        try:
            out = llm.write_experience(info, jd_text=jd_text, current=current, only=only,
                                       feedback=feedback)
        except Exception as exc:                             # noqa: BLE001
            _log(step="plan-error", label="experience", why=str(exc)[:200])
            return None
        if not isinstance(out, list) or len(out) != len(info):
            _log(step="plan-error", label="experience", why="wrong number of roles")
            return None
        return out

    def place(out, only=None):
        """Put the written bullets on the page; returns {role: [(bullet, reasons)]} refused."""
        refused: dict = {}
        for i, (_k, e, r) in enumerate(roles):
            want = info[i]["targets"]
            cur = list(r["bullets"]) if only is not None else [None] * len(want)
            cur = (cur + [None] * len(want))[:len(want)]
            got = [str(x or "").strip() for x in (out[i] or [])]
            idxs = (only or {}).get(i, []) if only is not None else range(len(want))
            for k in idxs:
                text = got[k] if k < len(got) else ""
                sibs = [str(x) for j, x in enumerate(cur) if j != k and x]
                bad = problems(i, text, sibs) if text else ["empty"]
                _log(step="plan-write", label=info[i]["label"], n=k, bullet=text, why=bad)
                if not bad:
                    cur[k] = text
                else:
                    refused.setdefault(i, []).append(k)
            # A slot still empty takes the person's own bullet (theirs, never refused).
            for k in range(len(cur)):
                if not cur[k]:
                    spare = [s for s in info[i]["saved"] if s not in cur
                             and not any(c and same_fact(s, c) for c in cur)]
                    cur[k] = spare[0] if spare else None
            r["bullets"][:] = [c for c in cur if c]
        return refused

    def compile_now(tag):
        return compile_tex(A.render_cv(preamble, used, sections, probe=True, headings=headings),
                           workdir, jobname=f"{jobname}-{tag}")

    def misses(res):
        rp = LF.measure(getattr(res, "pdf_path", None), used)
        fits = {i.text: i for i in rp.bullets}
        out: dict = {}
        notes: dict = {}
        for i, (_k, _e, r) in enumerate(roles):
            for k, b in enumerate(r["bullets"]):
                n = info[i]["targets"][k] if k < len(info[i]["targets"]) else 1
                it = fits.get(b)
                if it is None or not it.found:
                    notes[(i, k, "unfound")] = b[:40]
                    continue
                if lines_of(it, b, cpl) != n or not it.ok():
                    out.setdefault(i, []).append(k)
                    notes[(i, k)] = (f"was {len(b)} characters and printed on "
                                     f"{lines_of(it, b, cpl)} line(s), "
                                     f"the last {it.last_fill:.0%} full; it must print on {n} "
                                     f"line(s), the last one full")
        return out, notes

    before = copy.deepcopy([list(r["bullets"]) for _k, _e, r in roles])
    before_result = result
    out = ask()
    if out is None:
        return result, []
    refused = place(out)
    res = compile_now("plan")
    # VERIFY: one retry, in one call, for every bullet that missed its lines or a check.
    if res.ok and res.pages == 1:
        miss, why = misses(res)
    else:
        miss, why = {}, {}
    _log(step="plan-verify", label="experience", fill=res.fill_ratio, n=res.pages,
         why=[f"{i}:{k} {why.get((i, k), '')}" for i, ks in miss.items() for k in ks])
    for i, ks in refused.items():
        miss.setdefault(i, [])
        miss[i] = sorted(set(miss[i]) | set(ks))
        for k in ks:
            why.setdefault((i, k), "failed a check: no new numbers, names, credentials or "
                                   "claims, no employer name, no stock tail, no repeated phrase")
    if miss:
        cur = [list(r["bullets"]) for _k, _e, r in roles]
        fb = {f"{i}:{k}": v for (i, k), v in why.items()}
        out2 = ask(current=cur, only=miss, feedback=fb)
        if out2 is not None:
            place(out2, only=miss)
            res = compile_now("plan2")
            _log(step="plan-retry", label="experience", fill=res.fill_ratio, n=res.pages,
                 why=str(misses(res)[1])[:600] if res.ok and res.pages == 1 else "")
    # ADJUST once: over a page, or short of the goal, by the lines the page is off (1-line
    # bullets become 2-line in the newest roles, or 2-line become 1-line in the oldest).
    _log(step="plan-check", label="experience", fill=res.fill_ratio,
         why=f"ok={res.ok} pages={res.pages} overfull={res.overfull_count}/{base_overfull}")
    if res.ok and (res.pages > 1 or (res.fill_ratio or 0) > max_fill
                   or (res.fill_ratio or 0) < fill_goal):
        over = res.pages > 1 or (res.fill_ratio or 0) > max_fill
        off = 2 if res.pages > 1 else abs(round((PLAN_TARGET - (res.fill_ratio or 0)) / share))
        off = max(1, min(off, ADJUST_MOST))
        picks: dict = {}
        order = range(len(info) - 1, -1, -1) if over else range(len(info))
        for i in order:
            row = info[i]["targets"]
            for k in (range(len(row) - 1, -1, -1) if over else range(len(row))):
                if off and row[k] == (2 if over else 1):
                    row[k] = 1 if over else 2
                    picks.setdefault(i, []).append(k)
                    off -= 1
        if picks:
            cur = [list(r["bullets"]) for _k, _e, r in roles]
            fb = {f"{i}:{k}": f"the page needs this bullet on {info[i]['targets'][k]} line(s), "
                              "the last one full" for i, ks in picks.items() for k in ks}
            out3 = ask(current=cur, only=picks, feedback=fb)
            if out3 is not None:
                place(out3, only=picks)
                res = compile_now("plan3")
            _log(step="plan-adjust", label="experience", fill=getattr(res, "fill_ratio", None),
                 why=str(picks))
    if not (res.ok and res.pages == 1 and res.overfull_count <= base_overfull
            and (res.fill_ratio or 0) <= max_fill + 0.005):
        for (_k, _e, r), bl in zip(roles, before):
            r["bullets"][:] = bl
        _log(step="plan-undone", label="experience", fill=getattr(res, "fill_ratio", None))
        return before_result, []
    added = []
    for i, (_k, e, r) in enumerate(roles):
        for b in r["bullets"]:
            best = max(info[i]["saved"], key=lambda s: len(set(fact_stems(s)) & set(fact_stems(b))),
                       default="")
            share = (len(set(fact_stems(best)) & set(fact_stems(b))) / max(1, len(set(fact_stems(b))))
                     if best else 0)
            origins[b] = best if share >= 0.5 else b
            if b not in info[i]["saved"]:
                added.append({"section": "experience", "label": info[i]["label"], "bullet": b,
                              "kind": "written", "source": ""})
    _log(step="plan-done", label="experience", fill=res.fill_ratio,
         n=[len(r["bullets"]) for _k, _e, r in roles])
    return res, added
