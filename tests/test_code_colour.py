"""Code excerpts in Learn it and Defend it are syntax-coloured like VS Code (2026-10-13).

highlight.js 11.11.1 (BSD-3-Clause) is vendored in ui/static/vendor and loaded from there only;
no theme CSS is shipped, the colours are our own Dark+ and Light+ tokens in styles.css. The
language comes from the excerpt's file extension (the block between `// <code-lang>` and
`// </code-lang>` in app.js, run here under node).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "ui" / "static"
HTML = (ROOT / "ui" / "templates" / "index.html").read_text()
APP = (STATIC / "app.js").read_text()
CSS = (STATIC / "styles.css").read_text()


def test_highlight_is_vendored_and_loaded_locally_before_app():
    core = STATIC / "vendor" / "highlight-11.11.1.min.js"
    docker = STATIC / "vendor" / "highlight-dockerfile-11.11.1.min.js"
    assert core.read_text().startswith("/*!\n  Highlight.js v11.11.1")
    assert 'registerLanguage("dockerfile"' in docker.read_text()
    assert "BSD 3-Clause" in (STATIC / "vendor" / "highlight-LICENSE.txt").read_text()
    assert "highlight.js" in (ROOT / "NOTICES.md").read_text()
    srcs = re.findall(r"<script[^>]*src=\"([^\"]*)\"", HTML)
    hl = [s for s in srcs if "highlight" in s]
    assert len(hl) == 2 and all("http" not in s and "vendor/highlight-" in s for s in hl)
    assert HTML.index("vendor/highlight-11.11.1.min.js") < HTML.index("vendor/highlight-dockerfile") \
        < HTML.index("filename='app.js'")
    # nothing anywhere loads highlight.js (or its themes) from the internet
    for text in (HTML, APP, CSS):
        assert not re.search(r"https?://[^\s\"']*highlight", text)
    assert not list((STATIC / "vendor").glob("*.css"))


def test_both_themes_define_the_code_tokens():
    dark = CSS[CSS.index(":root {"):CSS.index(":root[data-theme=\"light\"] {")]
    light = CSS[CSS.index(":root[data-theme=\"light\"] {"):]
    light = light[:light.index("}")]
    want_dark = {"kw": "#569CD6", "ctl": "#C586C0", "str": "#CE9178", "fn": "#DCDCAA", "type": "#4EC9B0",
                 "num": "#B5CEA8", "com": "#6A9955", "var": "#9CDCFE", "text": "#D4D4D4", "const": "#4FC1FF"}
    want_light = {"kw": "#0000FF", "ctl": "#AF00DB", "str": "#A31515", "fn": "#795E26", "type": "#267F99",
                  "num": "#098658", "com": "#008000", "var": "#001080"}
    for block, want in ((dark, want_dark), (light, want_light)):
        for k, v in want.items():
            assert f"--code-{k}: {v};" in block, (k, v)
    assert ".learn-code .hljs-comment, .learn-code .hljs-quote { color: var(--code-com); font-style: italic; }" in CSS


def test_excerpts_are_rebuilt_from_text_not_injected():
    body = APP[APP.index("function codeLines("):APP.index("function learnCode(")]
    assert "hl.highlight(src" in body and "esc(part)" in body and 'createElement("template")' in body
    assert "highlightElement" not in APP and "highlightAll" not in APP


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_language_comes_from_the_file_extension(tmp_path):
    block = APP[APP.index("// <code-lang>"):APP.index("// </code-lang>")]
    cases = {"tracker/core.py": "python", "web/app.js": "javascript", "src/x.tsx": "typescript", "a.ts": "typescript",
             "package.json": "json", ".github/workflows/ci.yml": "yaml", "k8s.yaml": "yaml", "run.sh": "bash",
             "main.go": "go", "App.java": "java", "schema.sql": "sql", "pyproject.toml": "ini", "setup.cfg": "ini",
             "README.md": "markdown", "styles.css": "css", "index.html": "xml", "Dockerfile": "dockerfile",
             "docker/Dockerfile.dev": "dockerfile", "Makefile": "makefile", "requirements.txt": None,
             ".gitignore": None, "LICENSE": None, "": None, "CORE.PY": "python"}
    js = tmp_path / "t.js"
    js.write_text(block + f"\nconsole.log(JSON.stringify({json.dumps(list(cases))}.map(codeLang)));\n")
    got = json.loads(subprocess.run(["node", str(js)], capture_output=True, text=True, check=True).stdout)
    assert dict(zip(cases, got)) == cases


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_every_mapped_language_is_in_the_vendored_bundle(tmp_path):
    block = APP[APP.index("// <code-lang>"):APP.index("// </code-lang>")]
    langs = sorted(set(re.findall(r':\s*"([a-z]+)"', block[:block.index("function codeLang")]))
                   | {"dockerfile", "makefile"})
    vendor = STATIC / "vendor"
    js = tmp_path / "t.js"
    js.write_text(
        "const vm = require('vm'), fs = require('fs'); const ctx = {}; vm.createContext(ctx);\n"
        f"vm.runInContext(fs.readFileSync({json.dumps(str(vendor / 'highlight-11.11.1.min.js'))}, 'utf8') + ';this.hljs=hljs;', ctx);\n"
        f"vm.runInContext(fs.readFileSync({json.dumps(str(vendor / 'highlight-dockerfile-11.11.1.min.js'))}, 'utf8'), ctx);\n"
        f"console.log(JSON.stringify({json.dumps(langs)}.filter(l => !ctx.hljs.getLanguage(l))));\n"
        "const out = ctx.hljs.highlight('x = \"<b>\"  # c', {language: 'python'}).value;\n"
        "console.log(JSON.stringify(out));\n")
    missing, out = subprocess.run(["node", str(js)], capture_output=True, text=True, check=True).stdout.splitlines()
    assert json.loads(missing) == []
    out = json.loads(out)
    assert "&lt;b&gt;" in out and "<b>" not in out and 'class="hljs-comment"' in out   # text comes back escaped
