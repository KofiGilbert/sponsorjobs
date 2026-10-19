#!/usr/bin/env python3
"""Stitch the shared nav and footer into every page of the static website (site/).

    python scripts/build_site.py            # rewrite the partial regions in site/*.html
    python scripts/build_site.py --check    # exit 1 if any page is out of date (CI / tests)

The site is plain static files with no framework, so the one piece of shared markup, the
nav and the footer, lives once in scripts/site_partials/ and is copied into each page
between two marker comments:

    <!-- partial:nav -->  ...  <!-- /partial:nav -->
    <!-- partial:footer -->  ...  <!-- /partial:footer -->

The committed pages are the OUTPUT: the Cloudflare Worker serves site/ as-is and nothing
runs at deploy time. Run this after editing a partial, then commit the pages too.
The nav link whose href is the page being built gets aria-current="page".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
PARTIALS = ROOT / "scripts" / "site_partials"
NAMES = ("nav", "footer")


def _marker(name: str) -> re.Pattern[str]:
    return re.compile(rf"(<!-- partial:{name} -->)(.*?)(<!-- /partial:{name} -->)", re.S)


def render(name: str, page: str) -> str:
    """The partial's markup for `page` (a file name such as "pricing.html")."""
    html = (PARTIALS / f"{name}.html").read_text(encoding="utf-8").strip("\n")
    if name == "nav":
        # Mark the current page. The home link is the wordmark, which is never "current".
        html = re.sub(rf'(<a )([^>]*href="{re.escape(page)}"[^>]*)(>)',
                      lambda m: m.group(1) + m.group(2) + ' aria-current="page"' + m.group(3)
                      if 'aria-current' not in m.group(2) else m.group(0), html)
    return "\n" + html + "\n"


def build_page(path: Path) -> tuple[str, str]:
    """(current text, rebuilt text) for one page."""
    src = path.read_text(encoding="utf-8")
    out = src
    for name in NAMES:
        pat = _marker(name)
        if not pat.search(out):
            continue
        out = pat.sub(lambda m, n=name: m.group(1) + render(n, path.name) + m.group(3), out, count=1)
    return src, out


def pages() -> list[Path]:
    return sorted(p for p in SITE.glob("*.html"))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
    stale = []
    for page in pages():
        src, out = build_page(page)
        if src != out:
            stale.append(page.name)
            if not check:
                page.write_text(out, encoding="utf-8")
    if check:
        if stale:
            print("site pages out of date (run scripts/build_site.py):", ", ".join(stale))
            return 1
        print("site pages up to date")
        return 0
    print("rebuilt:", ", ".join(stale) if stale else "nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
