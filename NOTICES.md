# Third-Party Notices & License Inventory

This project embeds **MemPalace** (durable memory) and a set of **local CV
extraction + OCR** libraries, plus the packages they require. Every package below
is permissively licensed; this file is the pinned inventory for commercial-use
review.

**Scope.** Runtime base install of: `mempalace` (memory), `python-docx`, `pypdf`,
`python-pptx`, `pypdfium2`, `rapidocr-onnxruntime` (CV upload/extraction/OCR) — and
their transitive closure. No optional extras. Enumerated statically from the
resolved dependency graph. 88 packages total.

## License summary

- **All 88 packages are permissively licensed** — MIT, BSD, Apache-2.0, ISC, PSF,
  HPND/MIT-CMU, or public-domain equivalents.
- **No strong copyleft** (no GPL / LGPL / AGPL) anywhere.
- **Weak copyleft (MPL-2.0, file-level only):** `certifi`, `orjson`, `tqdm` — obliges
  sharing modifications *to those packages' own files* only; **no obligation on this
  application's code**. Standard for commercial use; flagged for completeness.

## OCR & embedding — engines and models (all Apache-2.0, bundled, offline)

- **OCR:** `rapidocr-onnxruntime` (Apache-2.0) running the **PP-OCR models (Apache-2.0)**
  that ship inside its wheel — no separate binary, no download. Runs on the
  `onnxruntime` (MIT) already shipped for MemPalace. Scanned PDFs are rasterized with
  **`pypdfium2`** (BSD-3-Clause / Apache-2.0; bundles Google's **PDFium**, BSD-3-Clause).
- **Embedding (MemPalace):** `minilm` / all-MiniLM-L6-v2 ONNX — **Apache-2.0**. Google
  Gemma (use-restricted) is **not** used.
- **No non-commercial licenses on any engine or model.**

## MemPalace copyright (MIT)

```
MIT License

Copyright (c) 2026 MemPalace Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Full runtime closure

| Package | Version | License |
|---|---|---|
| PyPika | 0.51.1 | Apache Software License |
| PyYAML | 6.0.3 | MIT License |
| Pygments | 2.20.0 | BSD-2-Clause |
| aiohappyeyeballs | 2.7.1 | Python Software Foundation License |
| aiohttp | 3.14.1 | Apache-2.0 AND MIT |
| aiosignal | 1.4.0 | Apache Software License |
| annotated-doc | 0.0.4 | MIT |
| annotated-types | 0.7.0 | MIT License |
| anyio | 4.14.1 | MIT |
| attrs | 26.1.0 | MIT |
| bcrypt | 5.0.0 | Apache Software License |
| build | 1.5.1 | MIT |
| certifi | 2026.6.17 | MPL-2.0 (weak, file-level) |
| charset-normalizer | 3.4.9 | MIT |
| chromadb | 1.5.9 | Apache Software License |
| click | 8.4.2 | BSD-3-Clause |
| colorama | 0.4.6 | BSD License |
| durationpy | 0.10 | MIT |
| filelock | 3.29.7 | MIT (Unlicense) |
| flatbuffers | 25.12.19 | Apache Software License |
| frozenlist | 1.8.0 | Apache-2.0 |
| fsspec | 2026.6.0 | BSD-3-Clause |
| googleapis-common-protos | 1.75.0 | Apache Software License |
| grpcio | 1.82.1 | Apache-2.0 |
| h11 | 0.16.0 | MIT License |
| hf-xet | 1.5.1 | Apache-2.0 |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD License |
| huggingface_hub | 1.23.0 | Apache Software License |
| idna | 3.18 | BSD-3-Clause |
| importlib_resources | 7.1.0 | Apache-2.0 |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| kubernetes | 36.0.2 | Apache Software License |
| lxml | 6.1.1 | BSD-3-Clause |
| markdown-it-py | 4.2.0 | MIT License |
| mdurl | 0.1.2 | MIT License |
| mempalace | 3.5.0 | MIT |
| mmh3 | 5.2.1 | MIT License |
| multidict | 6.7.1 | Apache License 2.0 |
| numpy | 2.5.1 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| oauthlib | 3.3.1 | BSD-3-Clause |
| onnxruntime | 1.27.0 | MIT License |
| opencv-python | 5.0.0.93 | Apache Software License |
| opentelemetry-api | 1.43.0 | Apache-2.0 |
| opentelemetry-exporter-otlp-proto-common | 1.43.0 | Apache-2.0 |
| opentelemetry-exporter-otlp-proto-grpc | 1.43.0 | Apache-2.0 |
| opentelemetry-proto | 1.43.0 | Apache-2.0 |
| opentelemetry-sdk | 1.43.0 | Apache-2.0 |
| opentelemetry-semantic-conventions | 0.64b0 | Apache-2.0 |
| orjson | 3.11.9 | MPL-2.0 AND (Apache-2.0 OR MIT) (weak, file-level) |
| overrides | 7.7.0 | Apache License, Version 2.0 |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause |
| pillow | 12.3.0 | HPND (MIT-CMU) |
| propcache | 0.5.2 | Apache Software License |
| protobuf | 7.35.1 | BSD-3-Clause |
| pybase64 | 1.4.3 | BSD License |
| pyclipper | 1.4.0 | MIT |
| pydantic | 2.13.4 | MIT |
| pydantic-settings | 2.14.2 | MIT |
| pydantic_core | 2.46.4 | MIT |
| pypdf | 6.14.2 | BSD-3-Clause |
| pypdfium2 | 5.11.0 | BSD-3-Clause / Apache-2.0 (PDFium BSD-3) |
| pyproject_hooks | 1.2.0 | MIT License |
| python-dateutil | 2.9.0.post0 | Apache-2.0 / BSD (dual) |
| python-docx | 1.2.0 | MIT License |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| python-pptx | 1.0.2 | MIT License |
| rapidocr-onnxruntime | 1.4.4 | Apache-2.0 |
| referencing | 0.37.0 | MIT |
| requests | 2.34.2 | Apache Software License |
| requests-oauthlib | 2.0.0 | BSD License |
| rich | 15.0.0 | MIT License |
| rpds-py | 2026.6.3 | MIT |
| shapely | 2.1.2 | BSD License |
| shellingham | 1.5.4 | ISC License (ISCL) |
| six | 1.17.0 | MIT License |
| tenacity | 9.1.4 | Apache Software License |
| tokenizers | 0.23.1 | Apache Software License |
| tqdm | 4.68.4 | MPL-2.0 AND MIT (weak, file-level) |
| typer | 0.26.8 | MIT |
| typing-inspection | 0.4.2 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.7.0 | MIT |
| uvicorn | 0.51.0 | BSD-3-Clause |
| websocket-client | 1.9.0 | Apache Software License |
| xlsxwriter | 3.2.9 | BSD License |
| yarl | 1.24.2 | Apache-2.0 |

## Excluded (intentionally not installed)

| Extra / package | Pulls | License | Why excluded |
|---|---|---|---|
| MemPalace `[pgvector]` | psycopg | **LGPL-3.0** (copyleft) | default Chroma backend is used |
| MemPalace `[extract]` / `[gpu]` / `[dev]` | markitdown / onnxruntime-gpu / tooling | mixed / permissive | not needed |
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
