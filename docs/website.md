# The website and the release pipeline

The landing page lives in `site/` as plain HTML, CSS and one script. No build step, no
framework, no analytics. It is deployed by Cloudflare Pages straight from the repository.
Installers are built by `.github/workflows/release.yml` and attached to a GitHub release;
the page's download buttons point at `releases/latest`.

## Placeholders to replace before launch

| Where | What | Replace with |
| --- | --- | --- |
| `site/script.js`, `FEED_BASE` | `https://tailor.example` | the broker's real host (the one that serves `/feed/manifest.json`) |
| `site/_headers`, `connect-src` | `https://tailor.example` | the same host, or the browser will block the fetch |
| `site/script.js`, `REPO_URL` | `https://github.com/KofiGilbert/sponsorjobs` | the public repository URL |
| `site/index.html`, every `href` to `github.com/KofiGilbert/sponsorjobs` | same | the same URL. These are the no-JavaScript fallback; the script rewrites them from `REPO_URL` at load. |
| `site/index.html`, the `demo-placeholder` block | dashed box | a `<video controls>` or a link to the recording |
| `docs/launch/*.md` | `[[TODO]]` markers | real links and numbers |

The live-numbers line expects `GET <FEED_BASE>/feed/manifest.json` to return
`{"generated_at": "<ISO 8601>", "count": <integer>}` with CORS allowing the site's origin.
The manifest is served by the R2 bucket (see docs/feed.md, custom domain step 3 for the CORS rule). If the request fails or the
shape is wrong the page keeps its static sentence, which makes no numeric claim.

## Deploy the page on Cloudflare Pages

1. Cloudflare dashboard, **Workers & Pages**, **Create**, **Pages** tab, **Connect to Git**.
2. Pick the GitHub account and the repository, then **Begin setup**.
3. Build settings:
   - Production branch: `main`
   - Framework preset: **None**
   - Build command: leave **empty**
   - Build output directory: `site`
   - Root directory (under "Path"): `site`
4. **Save and Deploy**. Cloudflare serves `site/index.html` and applies `site/_headers`.
   Every push to `main` redeploys; every pull request gets a preview URL.

### Custom domain

1. Add the domain to Cloudflare (**Websites**, **Add a domain**) and move its nameservers
   to the two Cloudflare gives you, if it is not there already.
2. In the Pages project, **Custom domains**, **Set up a custom domain**, enter the domain
   (and again for `www.`). Cloudflare creates the CNAME records itself when the zone is on
   Cloudflare.
3. Wait for the certificate (minutes). Then update the two `tailor.example` placeholders
   above and the R2 bucket's CORS rule (docs/feed.md) to the new domain.

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
