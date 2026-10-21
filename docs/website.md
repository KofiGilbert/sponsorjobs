# The website and the release pipeline

The website lives in `site/` as plain HTML, CSS and one script: `index.html`, `pricing.html`,
`download.html`, `open-source.html`, `privacy.html`, `terms.html`, `404.html`, plus `robots.txt`,
`sitemap.xml`, the OG image, self-hosted fonts under `fonts/` (Inter and JetBrains Mono;
SIL OFL, licences alongside) and the app screenshots under `img/` (AVIF, WebP and PNG, content-hashed
names so `_headers` can cache them for a year). No framework, no analytics, no runtime font CDN.
It is served by the Cloudflare Worker in `site-worker/` (`wrangler deploy` from that directory).
Installers are built by `.github/workflows/release.yml` and attached to a GitHub release;
the download page reads version, size and date from the releases API and falls back to the
`releases/latest` link.

## Shared nav and footer

The nav and footer live once in `scripts/site_partials/` and are copied into every page between
`<!-- partial:nav -->` / `<!-- partial:footer -->` markers by `scripts/build_site.py`. After editing a
partial, run `python scripts/build_site.py` and commit the pages too; `tests/test_site_copy.py`
fails if a page is out of date. The same test enforces the copy rules (no em or en dashes, no
exclamation marks, "resume" never "CV", no filler words), that local links resolve, that
`site/sponsor_core.js` is a verbatim copy of `extension/sponsor_core.js`, and that the prices on the
pricing page match `worker/src/metering.ts`.

## Live numbers and the employer check

`script.js` replaces the numbers baked into the HTML (each labelled with its "as of" date) with live
ones from `https://feed.sponsorjobs.ai/feed/manifest.json` (roles, refresh time) and
`/feed/sponsors/manifest.json` (`stats.h1b`, employers with H-1B approvals on record). The hero's
"Check an employer" box downloads the one shard of the public sponsor index the typed name lives in
(`/feed/sponsors/<a-z,0-9,_>.json.gz`, raw gzip inflated with `DecompressionStream` when the edge does
not), and runs the extension's own `SponsorCore.lookup` in the browser, so no name leaves the page.
If a fetch fails the page keeps its baked numbers and the box says it could not reach the index.

**The R2 bucket behind `feed.sponsorjobs.ai` must allow the site's origins in its CORS policy**
(docs/feed.md, custom domain step 3, with `https://sponsorjobs.ai` and `https://www.sponsorjobs.ai`),
or every browser fetch above is blocked and the page silently stays on its baked numbers.
As of 2026-10-18 the bucket returned no `Access-Control-Allow-Origin` header.

## Things the site reads at runtime

- `GET https://feed.sponsorjobs.ai/feed/manifest.json` returns `{"generated_at": "<ISO 8601>", "count": <integer>, ...}`
  and `/feed/sponsors/manifest.json` returns `{"generated_at", "employers", "stats": {"h1b", "perm", "e_verify"}, "shards"}`
  (both written by `scripts/build_feed.py` / `scripts/build_sponsor_index.py`). CORS must allow the site's origin.
- `GET https://api.github.com/repos/KofiGilbert/sponsorjobs` and `.../releases/latest`: stars, last push,
  release assets. Unauthenticated, 60 requests an hour per visitor IP; on failure the page keeps its
  non-numeric fallback text ("On GitHub") and the `releases/latest` link.
- `site/_headers` `connect-src` lists exactly those two hosts. Add a host there before fetching from it.

## Deploy the site

The site is a static-assets Cloudflare Worker (`site-worker/wrangler.jsonc`, assets directory `../site`,
custom domains `sponsorjobs.ai` and `www.sponsorjobs.ai`, `404.html` served for unknown paths):

    cd site-worker && npx wrangler deploy

Nothing is built at deploy time; what is in `site/` is what is served, `site/_headers` included.
Check locally first with `python3 -m http.server -d site 8080`. From a localhost origin the feed fetches
are blocked by CORS (expected) and the page shows its baked numbers.

## Cut a release

1. Make sure `main` builds (the `tests` workflow is green) and `packaging/bundle_sponsor_db.py`
   works from a fresh checkout.
2. Tag and push:

       git tag v0.1.0
       git push origin v0.1.0

   Or, from the **Actions** tab, run **release** by hand with an existing tag.
3. The workflow builds on `windows-latest` and `macos-latest` (Apple Silicon), about 20 to
   40 minutes, then creates (or updates) the GitHub release for that tag with
   `SponsorJobs-Setup-<version>.exe` and `SponsorJobs-<version>-arm64.dmg` attached, with
   auto-generated release notes. The shell's `package.json` version is set from the tag at
   build time so the file names match.
4. `releases/latest`, which the site links to, resolves to the newest release that is not a
   draft or a pre-release, so the download buttons update on their own.

Both installers are unsigned (packaging/README.md, "Not done yet"). The site's install
section tells people what the SmartScreen and Gatekeeper prompts look like and how to get
through them. Code signing on both platforms is the thing to add before a wide launch.

## Check the page locally

    python3 -m http.server -d site 8080

Open `http://localhost:8080`. The manifest fetch will fail against the placeholder host and
the page will show the fallback sentence, which is the expected behaviour.
