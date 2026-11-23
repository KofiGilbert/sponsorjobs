"""A link or menu item whose click changed nothing gets its own click alone, once (2026-11-23,
Gainwell's SAP SuccessFactors careers "Apply Now": the menu closes on mousedown, so the full
pointer sequence clicked a closed menu). Checked live: the run then reached the SAP application."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
VIEW = (ROOT / "extension" / "agent_view.js").read_text(encoding="utf-8")


def test_plain_click_skips_the_pointer_events_but_not_the_guards():
    body = VIEW[VIEW.index("async function doClick(el, opts)"):VIEW.index("function pickResult(")]
    plain = body.index("if (opts && opts.plain)")
    assert body.index('note: "submit-guard"') < plain and body.index("policyLink(el)") < plain
    assert plain < body.index('fire(target, "pointerdown", init)')
    assert "plain: !!action.plain" in VIEW


def test_the_shell_tries_it_once_after_a_menu_item_changed_nothing():
    i = AGENT.index("const menuItem = ")
    block = AGENT[i:AGENT.index("if (real) return real;", i)]
    assert "realClick(wc, a.index)" in block and "plain: true" in block
    assert block.index("realClick(wc, a.index)") < block.index("!== urlBefore") < block.index("plain: true")
