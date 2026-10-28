# Signed agent: Web Bot Auth for the apply agent

## Why

The apply agent (`docs/apply-agent.md`) fills and submits applications in the app's own browser
pane. The rule from `CLAUDE.md` 7 is that it never hides: no user-agent override, no proxies, no
captcha solving, no "human" delays. Web Bot Auth is the opposite move. Each document request the
agent makes to a job site carries an HTTP message signature (RFC 9421) in the shape Cloudflare
verifies for "signed agents", and the public key sits at a well-known URL on our domain. A site
behind Cloudflare can then see exactly who the bot is and allow it on purpose, by name, instead of
guessing from fingerprints. A site that does not check signatures simply ignores the headers.

The private key never ships in the installed app: it lives only in the broker Worker as a secret,
and the app asks the broker to sign each request for the signed-in account. A slow or missing
signer means an unsigned request, never a blocked one (`shell-electron/agent.js` `armSigning`).

## The key

One Ed25519 key pair. The secret `AGENT_SIGNING_KEY` holds the private key's 32-byte seed as
base64url (no padding). Make one with Node (the JWK `d` member IS the seed, already base64url):

```sh
node -e 'const k=require("crypto").generateKeyPairSync("ed25519"); console.log(k.privateKey.export({format:"jwk"}).d)'
```

or with OpenSSL:

```sh
openssl genpkey -algorithm ed25519 -outform DER | tail -c 32 | basenc --base64url -w0
```

Then `cd worker && npx wrangler secret put AGENT_SIGNING_KEY` and paste it. Keep a copy in the
password manager: it is the identity Cloudflare has on file, and rotating it means registering the
new key (below). The Worker derives the public key from the seed at start-up and publishes it as the
JWK `{kty: "OKP", crv: "Ed25519", x}`; the `kid` is that JWK's RFC 7638 thumbprint (the RFC 8037
A.3 member order, `{"crv":"Ed25519","kty":"OKP","x":"..."}`, SHA-256, base64url). Everything is
WebCrypto; nothing in the Worker can read the seed back out once imported.

## The two routes (`worker/src/botauth.ts`)

### `GET /.well-known/http-message-signatures-directory`

The public key set, served from **https://sponsorjobs.ai** (the registered `Signature-Agent`
origin, the one Cloudflare reads) and, for testing, from api.sponsorjobs.ai as well.

```
HTTP/1.1 200 OK
Content-Type: application/http-message-signatures-directory+json
Cache-Control: max-age=3600
Signature-Input: sig1=("@authority");alg="ed25519";keyid="<kid>";nonce="<base64>";created=<now>;expires=<now+3600>;tag="http-message-signatures-directory"
Signature: sig1=:<base64>:

{"keys":[{"kty":"OKP","crv":"Ed25519","kid":"<kid>","x":"<x>"}]}
```

The response is itself signed over `@authority`, the host it was requested from. Cloudflare's page
writes that component as `("@authority";req)` in its directory section and, in its limitations
section, says the `req` parameter is unsupported and fails verification; we emit the plain
`("@authority")`, so a verifier that does not understand `req` is never tripped. If Cloudflare's
`http-signature-directory` checker ever insists on `req`, that one string in `signDirectory` is the
change.

Why it is routed on the broker and not served by the site: the site at sponsorjobs.ai is the
separate `sponsorjobs-site` Worker (`site-worker/wrangler.jsonc`, static files on a custom domain).
Cloudflare runs a Worker on a route pattern before the custom-domain Worker, so the broker's
`wrangler.jsonc` adds one route, `sponsorjobs.ai/.well-known/http-message-signatures-directory`,
and only that path comes to the broker. The key and the signing stay in one Worker; the site Worker
needs no proxy code and never learns about keys. (A proxy from the site Worker would also have to
forward the original Host, since the directory signature covers it.)

### `POST /agent/sign`

Needs the account's bearer token (`401` without), answers `503 {"error":"not_configured"}` until
the secret is set. Body `{method, authority, path}`; `authority` must be a bare host, optionally
with a port (it is lowercased; anything else is `400 bad_authority`). `method` and `path` are
accepted for forward compatibility but are not covered by the signature: Cloudflare asks for
`@authority` and `signature-agent` and warns against covering components an intermediary may
rewrite. At most 120 signatures per account per minute (`429 rate_limited`), counted in D1
(`sign_limits`, the same guarded-upsert style as the sign-in limits) rather than in memory, because
an isolate can be recycled or run in several places at once. The Worker never logs the authority,
only counts.

Reply, for `{"authority": "boards.greenhouse.io"}`:

```json
{
  "signature_input": "sig2=(\"@authority\" \"signature-agent\");created=1793000000;keyid=\"<kid>\";alg=\"ed25519\";expires=1793000060;nonce=\"<base64>\";tag=\"web-bot-auth\"",
  "signature": "sig2=:<base64>:",
  "signature_agent": "\"https://sponsorjobs.ai\"",
  "expires": 1793000060
}
```

The shell puts them on the request as is:

```
Signature-Agent: "https://sponsorjobs.ai"
Signature-Input: sig2=("@authority" "signature-agent");created=1793000000;keyid="<kid>";alg="ed25519";expires=1793000060;nonce="<base64>";tag="web-bot-auth"
Signature: sig2=:<base64>:
```

A signature is good for 60 seconds; Cloudflare relies on that, not on the nonce, against replay. The
signature base the key signs (RFC 9421 section 2.5) is

```
"@authority": boards.greenhouse.io
"signature-agent": "https://sponsorjobs.ai"
"@signature-params": ("@authority" "signature-agent");created=1793000000;keyid="<kid>";alg="ed25519";expires=1793000060;nonce="<base64>";tag="web-bot-auth"
```

The app side: `ui/app.py` `POST /api/autoapply/agent/sign` proxies to this route with the account
token; `shell-electron/agent.js` `armSigning` calls it for every document or frame request to a host
in the run's allowlist and adds the three headers.

## Registering the bot with Cloudflare

Cloudflare's verified-bot form (dashboard: Manage Account, Configurations, Bot Submission Form;
tracked under BotBase, Submission history). What to paste:

- **Bot name:** SponsorJobs apply agent
- **Purpose / description:** A desktop job-application assistant. For a signed-in person it opens a
  job posting in the app's own browser, fills the application from the person's own saved
  profile, and submits only where the site permits automated applications (elsewhere the person
  clicks submit). One person, one application at a time, no crawling, no scraping, no bulk
  traffic; it identifies itself with Web Bot Auth on every request.
- **Verification method:** Request Signature
- **Validation instructions:** `https://sponsorjobs.ai/.well-known/http-message-signatures-directory`
- **Operation / category:** Intermediary (acting for a signed-in user)
- **User agent (optional):** `sponsorjobs-shell/<version>`; leave the match pattern loose. The
  signature, not the user agent, is what verifies.
- **Contact:** support@sponsorjobs.ai

## Deploy checklist (owner)

1. Make the seed (recipe above) and set it: `cd worker && npx wrangler secret put AGENT_SIGNING_KEY`.
2. Apply the migration: `npx wrangler d1 migrations apply DB --remote` (adds `sign_limits`).
3. Deploy: `npx wrangler deploy`. The new route needs the sponsorjobs.ai zone on this account
   (it is) and its DNS record proxied (the site's custom domain made one).
4. Check the directory on both hosts:
   `curl -si https://sponsorjobs.ai/.well-known/http-message-signatures-directory` and the same on
   `api.sponsorjobs.ai`. Expect `200`, the content type above, a `Signature-Input` with
   `tag="http-message-signatures-directory"`, and one key. The `kid` must be identical on both.
   Optionally run Cloudflare's checker, `http-signature-directory` (from their
   `web-bot-auth` repository), against the sponsorjobs.ai URL.
5. Submit the registration form (text above) and wait for Cloudflare to accept the key.
6. Prove it end to end from the shell: start an apply-agent run on `https://crawltest.com/` (any
   path; the allowlist must include it for the test, or sign one request by hand with the three
   headers from `/agent/sign` for authority `crawltest.com`) and request
   `https://crawltest.com/cdn-cgi/web-bot-auth`. `200` means the key is known and the signature
   verified; `401` means the signature parsed but the key is unknown (not registered yet) or did
   not verify; `400` means a malformed header. Until the registration is accepted, `401` is the
   expected answer.
7. Rotation: generate a new seed, set the secret, deploy, confirm the directory shows the new
   `kid`, and tell Cloudflare through the same form. The old signatures stop verifying at once,
   which is fine: each is good for 60 seconds.

## Tests

`worker/test/botauth.test.ts`: a fixed seed; the `kid` is recomputed in the test from the
published `x`; the directory's signature and `/agent/sign`'s signature are verified with WebCrypto
over a signature base rebuilt from the headers alone; the tag, the 60-second expiry, the lowercased
authority, `401`, `503`, `400` and the `429` after 120 calls. The crawltest.com check is manual
(step 6) and never part of the test run.
