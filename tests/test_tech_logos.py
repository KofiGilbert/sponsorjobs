"""The Projects cards' technology logos (2026-10-13).

The idea and project tiles show the real logo of the main technology, from Simple Icons (CC0)
vendored in ui/static/vendor/techlogos.js. This pins that the vendored file is local only (no
URL the page could load), well formed, and that the pure mapping in app.js (the block between
`// <tech-logo>` and `// </tech-logo>`, run here under node) picks frameworks before languages,
honours the aliases, and returns null for a stack it has no logo for.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOGOS = ROOT / "ui" / "static" / "vendor" / "techlogos.js"
APP = ROOT / "ui" / "static" / "app.js"


def _logos() -> dict:
    src = LOGOS.read_text()
    m = re.search(r"const LOGOS = (\{.*?\});\n", src, re.S)
    assert m, "techlogos.js should define LOGOS as one JSON object"
    return json.loads(m.group(1))


def test_vendored_logos_are_local_and_well_formed():
    src = LOGOS.read_text()
    assert "http" not in src.lower(), "the vendored logos must not reference any URL"
    assert "Simple Icons" in src and "CC0" in src
    logos = _logos()
    assert 50 <= len(logos) <= 80
    for slug, lg in logos.items():
        assert re.fullmatch(r"[0-9A-F]{6}", lg["hex"]), slug
        assert re.fullmatch(r"[MmLlHhVvCcSsQqTtAaZz0-9.,\s\-eE]+", lg["path"]), slug
        assert lg["title"], slug
    assert "Simple Icons" in (ROOT / "NOTICES.md").read_text()


def test_index_loads_logos_from_static_before_app():
    html = (ROOT / "ui" / "templates" / "index.html").read_text()
    assert html.index("vendor/techlogos.js") < html.index("filename='app.js'")


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_mapping_picks_main_tech(tmp_path):
    app = APP.read_text()
    block = app[app.index("// <tech-logo>"):app.index("// </tech-logo>")]
    cases = [
        {"tech": "Python, Apache Airflow, PostgreSQL", "skills": ["Python", "Airflow", "SQL"]},
        {"tech": "JavaScript, React, Node.js", "skills": []},
        {"tech": "Python 3 + FastAPI + Postgres (Docker)", "skills": []},
        {"tech": "", "skills": ["sklearn", "Python"]},
        {"tech": "Bash, AWS, k8s", "skills": []},
        {"tech": "Python", "skills": ["GitHub Actions"]},
        {"tech": "PostgreSQL database", "skills": []},
        {"tech": "COBOL, Excel", "skills": ["Communication"]},
        {"tech": "", "skills": []},
        {"tech": "Next.js, TypeScript", "skills": []},
    ]
    script = tmp_path / "run.js"
    script.write_text(
        "const esc = s => String(s == null ? '' : s);\n"
        f"const LOGOS = require({json.dumps(str(LOGOS))});\n"
        + block
        + f"\nconst cases = {json.dumps(cases)};\n"
        "console.log(JSON.stringify({picks: cases.map(c => (mainTech(c, LOGOS) || {}).slug || null),"
        " fills: [logoFill('000000'), logoFill('FFFFFF'), logoFill('3776AB')],"
        " noLogoTile: techTile({tech: 'COBOL'}, '<svg/>', 'x')}));\n"
    )
    out = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout)
    assert out["picks"] == ["apacheairflow", "react", "fastapi", "scikitlearn", "kubernetes",
                            "githubactions", "postgresql", None, None, "nextdotjs"]
    assert out["fills"] == ["#000000", "#3a3a3c", "#3776AB"]
    assert 'class="tech-tile x"' in out["noLogoTile"] and "<svg/>" in out["noLogoTile"]
