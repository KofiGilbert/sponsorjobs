"""Embed autoapply/recipes.json into extension/autofill_core.js as the offline fallback copy.

The app serves the live table at GET /api/autofill/recipes (and inside /api/profile/autofill), so
the extension and the desktop shell normally get the freshest copy. The engine still carries one
embedded copy so a fill works when the table did not arrive. One source of truth: edit
autoapply/recipes.json, run this script, commit both. tests/test_fill_recipes.py fails when the
two drift.

    python scripts/embed_recipes.py          # rewrite the embedded copy
    python scripts/embed_recipes.py --check  # exit 1 if it is out of date
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPES = ROOT / "autoapply" / "recipes.json"
CORE = ROOT / "extension" / "autofill_core.js"
MARK = re.compile(r"/\*RECIPES_JSON_START\*/(.*?)/\*RECIPES_JSON_END\*/", re.S)


def embedded_text(core_src: str) -> str:
    m = MARK.search(core_src)
    if not m:
        raise SystemExit("autofill_core.js has no RECIPES_JSON_START/END markers")
    return m.group(1)


def compact(recipes: dict) -> str:
    # '</script' can never appear in the JSON, so the JS stays safe inside a <script> too.
    return json.dumps(recipes, separators=(",", ":"), ensure_ascii=True).replace("</", "<\\/")


def main(argv: list[str]) -> int:
    recipes = json.loads(RECIPES.read_text(encoding="utf-8"))
    src = CORE.read_text(encoding="utf-8")
    want = compact(recipes)
    have = embedded_text(src)
    if "--check" in argv:
        if have == want:
            print("embedded recipes are up to date")
            return 0
        print("embedded recipes are STALE: run python scripts/embed_recipes.py")
        return 1
    new = MARK.sub(lambda _m: "/*RECIPES_JSON_START*/" + want + "/*RECIPES_JSON_END*/", src, count=1)
    CORE.write_text(new, encoding="utf-8")
    print(f"embedded {len(recipes.get('systems', []))} systems ({len(want)} bytes) into {CORE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
