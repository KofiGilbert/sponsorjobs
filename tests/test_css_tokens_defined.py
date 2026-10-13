"""Every CSS custom property used without a fallback must be defined somewhere.

`var(--x)` with no fallback and no definition is invalid at computed-value time, so the
property silently resets (a border-radius drops to 0, a colour falls back to inherit).
That is how the Jobs search box and pager lost their rounded corners when `--r-md` was
used but never declared. Properties set from JS via style.setProperty count as defined.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "ui" / "static"


def _defined(css: str, js: str) -> set[str]:
    names = set(re.findall(r"(--[\w-]+)\s*:", css))
    names |= set(re.findall(r"""setProperty\(\s*['"](--[\w-]+)""", js))
    names |= set(re.findall(r"(--[\w-]+)\s*:", js))  # inline style strings
    return names


def test_every_fallbackless_var_is_defined():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    defined = _defined(css, js)
    undefined = {}
    for src_name, src in (("styles.css", css), ("app.js", js)):
        for m in re.finditer(r"var\(\s*(--[\w-]+)\s*\)", src):
            if m.group(1) not in defined:
                line = src.count("\n", 0, m.start()) + 1
                undefined.setdefault(m.group(1), []).append(f"{src_name}:{line}")
    assert not undefined, f"var() used with no definition and no fallback: {undefined}"


def test_radius_scale_has_r_md():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    root = css[css.index(":root {"):]
    root = root[: root.index("}")]
    assert re.search(r"--r-md:\s*\d+px", root)
