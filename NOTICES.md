# Third-Party Notices & License Inventory

This project embeds a set of **local CV extraction + OCR** libraries, plus the
packages they require. Every package below is permissively licensed; this file is
the pinned inventory for commercial-use review.

**Scope.** Runtime base install of: `python-docx`, `pypdf`, `python-pptx`,
`pypdfium2`, `rapidocr-onnxruntime` (CV upload/extraction/OCR) and their
transitive closure. No optional extras. Enumerated statically from the resolved
dependency graph. 26 packages total.

MemPalace and ChromaDB were removed on 2026-10-30 (the applicant brain, Phase 2,
docs/applicant-brain.md section 12): the chat diary is now the app's own
standard-library code, so they and the 65 packages only they pulled into this
inventory (grpc, OpenTelemetry, the Kubernetes client, tokenizers, Hugging Face Hub
and others) are gone from it. A few of those (pydantic, anyio, click, idna, h11) are
still installed by Flask and the model SDKs, which this inventory does not cover.

## License summary

- **All 21 packages are permissively licensed**: MIT, BSD, Apache-2.0, PSF,
  HPND/MIT-CMU, or public-domain equivalents.
- **No strong copyleft** (no GPL / LGPL / AGPL) anywhere.
- **Weak copyleft (MPL-2.0, file-level only):** `tqdm`. It obliges sharing
  modifications *to that package's own files* only; **no obligation on this
  application's code**. Standard for commercial use; flagged for completeness.

## OCR engine and models (Apache-2.0, bundled, offline)

- **OCR:** `rapidocr-onnxruntime` (Apache-2.0) running the **PP-OCR models (Apache-2.0)**
  that ship inside its wheel: no separate binary, no download. Runs on `onnxruntime`
  (MIT). Scanned PDFs are rasterized with **`pypdfium2`** (BSD-3-Clause / Apache-2.0;
  bundles Google's **PDFium**, BSD-3-Clause).
- **No embedding model** is used or downloaded.
- **No non-commercial licenses on any engine or model.**

## Full runtime closure

| Package | Version | License |
|---|---|---|
| certifi | 2026.7.22 | Mozilla Public License 2.0 (MPL 2.0) |
| charset-normalizer | 3.5.2 | MIT License |
| colorama | 0.4.6 | BSD License |
| flatbuffers | 25.12.19 | Apache Software License |
| idna | 3.20 | BSD-3-Clause |
| lxml | 6.1.1 | BSD-3-Clause |
| numpy | 2.5.1 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| onnxruntime | 1.27.0 | MIT License |
| opencv-python | 5.0.0.93 | Apache Software License |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause |
| pillow | 12.3.0 | HPND (MIT-CMU) |
| protobuf | 7.35.1 | BSD-3-Clause |
| pyclipper | 1.4.0 | MIT |
| pypdf | 6.14.2 | BSD-3-Clause |
| pypdfium2 | 5.11.0 | BSD-3-Clause / Apache-2.0 (PDFium BSD-3) |
| python-docx | 1.2.0 | MIT License |
| python-pptx | 1.0.2 | MIT License |
| PyYAML | 6.0.3 | MIT License |
| rapidocr-onnxruntime | 1.4.4 | Apache-2.0 |
| requests | 2.34.2 | Apache-2.0 |
| shapely | 2.1.2 | BSD License |
| six | 1.17.0 | MIT License |
| tqdm | 4.68.4 | MPL-2.0 AND MIT (weak, file-level) |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.8.0 | MIT License |
| xlsxwriter | 3.2.9 | BSD License |

## Excluded (intentionally not installed)

| Extra / package | Pulls | License | Why excluded |
|---|---|---|---|
| `mempalace`, `chromadb` (removed 2026-10-30) | grpc, OpenTelemetry, tokenizers, and 60+ more | permissive | the chat diary is our own code now |
| `reportlab` (test-only) | — | BSD | generates sample PDFs in tests; **not a runtime dependency** |

## Adapted open-source code (attribution)

Small pieces of this application's own code were adapted, and rewritten in our shape,
from two MIT-licensed projects. Both licenses permit this with attribution; the notices
below satisfy that. No stealth, anti-detection, proxy, or LinkedIn-automation code was
taken from either project (CLAUDE.md §7).

- **career-ops** — Copyright (c) 2026 Santiago Fernández de Valderrama Aparicio. MIT.
  Ideas and logic adapted: the numeric fact gate (`tailoring/fact_gate.py`, after
  `verify-cv-facts.mjs`), posting-URL and company/role dedup keys (`sourcing/dedup.py`,
  after `url-key.mjs` and the dedup helpers in `scan.mjs`), and the knock-out screening
  question list (`drafting/drafter.py`, after `modes/apply.md`).
- **AIHawk** v0.68.0 (post 2 September 2026 MIT relicense; nothing from earlier AGPL
  releases was used) — Copyright (c) 2024-2026 AIHawk contributors. MIT. Logic adapted:
  the visible-control predicate used by the assisted-apply filler
  (`extension/autofill_core.js`, after the snapshot visibility rule in `mcp/actions.py`)
  and the covered-button diagnosis in the desktop shell's submit step
  (`shell-electron/main.js`).

OpenCATS (MPL-2.0 / CATS Public License) and PeelJobs (MIT) were reviewed; no code was
copied from either. OpenCATS's candidate status vocabulary informed the inbox
classifier's wording list only.

AienTech/dossier (AGPL-3.0) was studied for product ideas only; no code, text or assets were
copied, and everything that came of it is our own Python and JavaScript. The ideas: a fact gate
that also checks names, tools and credentials (`tailoring/fact_gate.py`), a "What changed" summary
computed from the resume rather than written by a model (`tailoring/changes.py`), trims that can
be put back (`tailoring/assembler.py`), refine actions made from coverage gaps, and showing a
company's brand apart from its legal name (`sourcing/brands.py`).

## Job data sources (attribution)

- **remoteintech/remote-jobs** (https://github.com/remoteintech/remote-jobs), ISC License,
  Copyright (c) remoteintech contributors. Used 2026-10-30 as a list of company names and careers
  URLs to find public ATS boards; 42 verified boards were added to `seed/watchlist_seed.csv`
  (marked `source=remoteintech` in `seed/watchlist_evidence.csv`). Only those facts were used; no
  text from the project was copied.
- **Hacker News "Ask HN: Who is hiring?"** (https://news.ycombinator.com), read through the HN
  search API (https://hn.algolia.com/api). **Off by default**: the adapter
  (`sourcing/hackernews.py`) runs only with `HN_WHOISHIRING=1`, because Y Combinator's site terms
  (https://www.ycombinator.com/legal/) restrict commercial use of HN content. When switched on,
  rows carry only facts from each post's first line and link back to the post; the posts' text
  is not copied, and the rows are credited "via Hacker News" (docs/feed.md). While it is off, no
  HN data is fetched, stored, published or credited.

## Bundled brand icons (CC0)

- **Simple Icons** 16.33.0 (simpleicons.org, npm `simple-icons`) — **CC0 1.0 Universal**
  (public domain dedication). 67 technology logos (SVG path plus official brand hex) are vendored
  in `ui/static/vendor/techlogos.js` and shown on the Projects cards to name an idea's main
  technology. Nothing is fetched at runtime. The marks remain trademarks of their owners and are
  used only to identify the technology, which CC0 does not change.

## Bundled syntax highlighter (BSD-3-Clause)

- **highlight.js** 11.11.1 (highlightjs.org, cdnjs `highlight.js/11.11.1`), **BSD 3-Clause**,
  Copyright (c) 2006, Ivan Sagalaev. The common bundle (`ui/static/vendor/highlight-11.11.1.min.js`,
  which carries python, javascript, typescript, json, yaml, bash, go, java, sql, ini/toml, markdown,
  css and xml/html) plus the `dockerfile` grammar (`ui/static/vendor/highlight-dockerfile-11.11.1.min.js`)
  colour the code excerpts in Projects, Learn it and Defend it. No highlight.js theme CSS is shipped;
  the colours are our own tokens in `styles.css`. Nothing is fetched at runtime. Full license text:
  `ui/static/vendor/highlight-LICENSE.txt`.
