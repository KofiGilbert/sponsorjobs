"""A click the page reports as covered (2026-11-23, Gainwell's SAP careers "Apply Now" menu item):
the shell presses it with the real pointer; when nothing happened, it presses the element itself
once (``force``), and the page view's Submit and policy-link guards still apply to that click."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
VIEW = (ROOT / "extension" / "agent_view.js").read_text(encoding="utf-8")


def test_the_shell_falls_back_to_the_elements_own_click_only_when_nothing_changed():
    block = AGENT[AGENT.index('if (!ctx && a.op === "click" && out && /covered/.test(out.note || ""))'):]
    block = block[:block.index("return out;")]
    real, changed, forced = block.index("realClick(wc, a.index)"), block.index("!== urlBefore"), block.index("force: true")
    assert real < changed < forced


def test_force_only_skips_the_cover_check_never_the_guards():
    body = VIEW[VIEW.index("async function doClick(el, opts)"):VIEW.index("function pickResult(")]
    assert body.index('note: "submit-guard"') < body.index("opts && opts.force ? null : covered(target)")
    assert body.index("policyLink(el)") < body.index("opts && opts.force")
    assert 'if (op === "click") return await doClick(el, { force: !!action.force, plain: !!action.plain });' in VIEW
