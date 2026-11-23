// Web Bot Auth: the apply agent identifies itself to job sites with signed requests.
//
// WHY: the desktop app's apply agent (docs/apply-agent.md) fills and submits applications in the
// app's own browser pane. It does not hide (no UA override, no proxies, no captcha solving; CLAUDE.md
// 7). The opposite: every document request it makes to a job site carries an HTTP message
// signature (RFC 9421) in the shape Cloudflare's "signed agents" verify, so a site behind Cloudflare
// can see exactly who the bot is and let it through on purpose. The public key is published at
// https://sponsorjobs.ai/.well-known/http-message-signatures-directory; the private key lives ONLY
// here, as the AGENT_SIGNING_KEY secret, never in the installed app. The app asks this Worker to
// sign (POST /agent/sign, with the account's bearer token; ui/app.py proxies it as
// /api/autoapply/agent/sign and shell-electron/agent.js armSigning adds the headers).
//
// What is signed. A request signature covers the components "@authority" (the site's host) and
// "signature-agent" (our origin, where the key directory is), with the parameters Cloudflare lists
// (docs/signed-agent.md):
//
//   Signature-Agent: "https://sponsorjobs.ai"
//   Signature-Input: sig2=("@authority" "signature-agent");created=<unix>;keyid="<kid>";
//                    alg="ed25519";expires=<unix+60>;nonce="<base64>";tag="web-bot-auth"
//   Signature: sig2=:<base64 of the Ed25519 signature over the signature base>:
//
// The method and path are accepted in the body for forward compatibility but are not covered:
// Cloudflare asks for @authority (and signature-agent) and warns against components an intermediary
// may rewrite. A signature is good for 60 seconds; Cloudflare relies on that, not the nonce, against
// replay. The directory response is signed over ("@authority";req), the host the directory was
// requested from, with the tag "http-message-signatures-directory". The req flag binds the
// response's signature to that request, so the directory cannot be replayed from another origin.
// Cloudflare's review required it (2026-11-02); the "req is not supported" line in its docs is
// about the bot's request signatures, not this one. Everything emitted is ASCII.
//
// Keys. The secret is the Ed25519 private key's 32-byte seed as base64url (wrangler.jsonc says how
// to make one). The public key is the JWK {kty:"OKP", crv:"Ed25519", x}, and the keyid is its
// thumbprint (RFC 7638 with the RFC 8037 A.3 member order): base64url(sha256 of the canonical JSON
// `{"crv":"Ed25519","kty":"OKP","x":"<x>"}`). All through WebCrypto, which Workers support for
// Ed25519.
//
// Privacy and limits. The Worker never logs the authority (the site being applied to); only counts
// are logged. To count distinct sites a day, the site's registrable domain (never the path) is kept
// in D1 for the current UTC day only. /agent/sign is limited to 120 calls per account per minute, counted in D1
// (`sign_limits`, a guarded upsert as in signin.ts) rather than an in-memory map: an isolate can be
// recycled or run in several places at once, and a per-isolate counter would reset or split with
// it. A page with twenty frames and redirects is a handful of signatures; 120 a minute is plenty
// for an agent and far too few for a scraper.
//
// Who may ask, and for what (2026-11-23). A signature vouches for the request under our name, so
// it is not handed to anyone with a token:
//  - The account must have a verified e-mail (a completed e-mail code or Google sign-in) or a paid
//    plan. Anonymous device accounts are free and quick to make; they get 403 sign_in_required and
//    their requests go out unsigned, as before signing existed.
//  - The authority must be a job application site (src/applysites.ts, the app's own lists): a
//    hiring system's host, or the application site of the apply run the request belongs to. The
//    app sends that run's apply host as `site`; the authority must be that host or on its domain.
//    The job boards that forbid automation are never signed for.
//  - Daily caps, counted in D1 (`sign_daily`, `sign_sites`; migrations/0016_sign_daily.sql): the
//    signatures per account and per client network a day, and the number of distinct application
//    sites (not hiring systems) an account may be signed for a day. Each is a Worker var with a
//    default (SIGNS_PER_ACCOUNT_PER_DAY, SIGNS_PER_IP_PER_DAY, SIGN_SITES_PER_DAY).
//  - Volume is logged as counts only (never the authority or the site): each refusal, and an
//    account or the whole service passing round numbers of signatures in a day.

import { identify } from "./accounts";
import { isAtsHost, isProhibitedHost, onSite, siteKey } from "./applysites";
import { type Env, intVar } from "./env";
import { isPass, PLANS } from "./metering";

const json = (data: unknown, status = 200, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", ...headers } });

async function body(req: Request): Promise<Record<string, unknown>> {
  try {
    const b = await req.json();
    return b && typeof b === "object" && !Array.isArray(b) ? (b as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

export const SIGNATURE_AGENT = "https://sponsorjobs.ai";  // where the directory is served (the registered origin)
export const REQUEST_TTL_SECONDS = 60;                     // a signed request is good this long
export const DIRECTORY_TTL_SECONDS = 3600;                 // the directory's signature and cache life
export const SIGNS_PER_MINUTE = 120;                       // per account
export const SIGNS_PER_ACCOUNT_PER_DAY = 3000;             // defaults; each a Worker var too
export const SIGNS_PER_IP_PER_DAY = 10000;                 // a campus shares one network
export const SIGN_SITES_PER_DAY = 60;                      // distinct application sites per account
const LOG_EVERY = 1000;                                    // log an account's daily count at each multiple
const LOG_TOTAL_EVERY = 10000;                             // and the service's
export const DIRECTORY_PATH = "/.well-known/http-message-signatures-directory";
export const DIRECTORY_CONTENT_TYPE = "application/http-message-signatures-directory+json";
export const REQUEST_LABEL = "sig2";                       // the labels in Cloudflare's examples
export const DIRECTORY_LABEL = "sig1";

// ---- encoding ---------------------------------------------------------------------------------- //

const enc = new TextEncoder();

export function b64(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

export const b64url = (bytes: Uint8Array) => b64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export function fromB64url(s: string): Uint8Array {
  const pad = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  return Uint8Array.from(atob(pad), (c) => c.charCodeAt(0));
}

// The PKCS#8 wrapping of a raw Ed25519 seed (RFC 8410): a fixed 16-byte prefix, then the 32 bytes.
const PKCS8_PREFIX = Uint8Array.from([0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20]);

function pkcs8(seed: Uint8Array): Uint8Array {
  const out = new Uint8Array(PKCS8_PREFIX.length + seed.length);
  out.set(PKCS8_PREFIX);
  out.set(seed, PKCS8_PREFIX.length);
  return out;
}

// ---- the key ----------------------------------------------------------------------------------- //

export interface SigningKey {
  privateKey: CryptoKey;
  x: string;       // the public key, base64url
  kid: string;     // its JWK thumbprint
}

/** RFC 7638 thumbprint of an Ed25519 public JWK, with the members in the order RFC 8037 A.3
 * shows (lexicographic: crv, kty, x) and no whitespace. */
export async function thumbprint(x: string): Promise<string> {
  const canonical = `{"crv":"Ed25519","kty":"OKP","x":"${x}"}`;
  return b64url(new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(canonical))));
}

/** Import the seed. The public half comes out of the imported private key's JWK export (`x`), so
 * the secret holds only the 32 seed bytes and the two halves can never disagree. */
export async function importSeed(seedB64url: string): Promise<SigningKey> {
  const seed = fromB64url(seedB64url.trim());
  if (seed.byteLength !== 32) throw new Error(`AGENT_SIGNING_KEY must be a 32-byte seed, got ${seed.byteLength} bytes`);
  const extractable = await crypto.subtle.importKey("pkcs8", pkcs8(seed), { name: "Ed25519" }, true, ["sign"]);
  const jwk = (await crypto.subtle.exportKey("jwk", extractable)) as JsonWebKey;
  if (!jwk.x) throw new Error("could not derive the public key from AGENT_SIGNING_KEY");
  // Re-import non-extractable for signing: nothing in the Worker can read the seed back out.
  const privateKey = await crypto.subtle.importKey("pkcs8", pkcs8(seed), { name: "Ed25519" }, false, ["sign"]);
  return { privateKey, x: jwk.x, kid: await thumbprint(jwk.x) };
}

// One import per isolate per secret value. Keyed by the secret so a rotated secret (or a test
// passing a different one) is never served with a stale key.
let cached: { secret: string; key: Promise<SigningKey> } | null = null;

export function keyFrom(env: Env): Promise<SigningKey> | null {
  const secret = env.AGENT_SIGNING_KEY?.trim();
  if (!secret) return null;
  if (!cached || cached.secret !== secret) {
    const key = importSeed(secret);
    key.catch(() => { cached = null; });      // a bad secret is retried next time, not cached
    cached = { secret, key };
  }
  return cached.key;
}

// ---- RFC 9421 ---------------------------------------------------------------------------------- //

/** A component identifier as serialized: "@authority" -> "\"@authority\"", "@authority;req" ->
 * "\"@authority\";req" (RFC 9421 section 2.4: a flag rides after the quoted name). */
const componentId = (c: string) => {
  const [name, ...flags] = c.split(";");
  return [`"${name}"`, ...flags].join(";");
};

/** The signature-params value: the covered components in order, then the parameters. */
export function signatureParams(components: string[], params: Array<[string, string | number]>): string {
  const list = components.map(componentId).join(" ");
  const tail = params.map(([k, v]) => (typeof v === "number" ? `;${k}=${v}` : `;${k}="${v}"`)).join("");
  return `(${list})${tail}`;
}

/** The signature base (RFC 9421 section 2.5): one line per covered component, "name": value, then
 * the "@signature-params" line. Lines are joined with LF and there is no trailing newline. */
export function signatureBase(components: Array<[name: string, value: string]>, params: string): string {
  const lines = components.map(([name, value]) => `${componentId(name)}: ${value}`);
  lines.push(`"@signature-params": ${params}`);
  return lines.join("\n");
}

async function sign(key: SigningKey, base: string): Promise<string> {
  const sig = await crypto.subtle.sign({ name: "Ed25519" }, key.privateKey, enc.encode(base));
  return b64(new Uint8Array(sig));
}

const nonce = () => b64(crypto.getRandomValues(new Uint8Array(16)));

export interface SignedHeaders {
  signature_input: string;
  signature: string;
  signature_agent: string;
  expires: number;
}

/** The three headers for one request to `authority`, in Cloudflare's example shape. */
export async function signRequest(key: SigningKey, authority: string, now: number): Promise<SignedHeaders> {
  const agent = `"${SIGNATURE_AGENT}"`;                 // a structured-field string: double quoted
  const expires = now + REQUEST_TTL_SECONDS;
  const params = signatureParams(["@authority", "signature-agent"], [
    ["created", now], ["keyid", key.kid], ["alg", "ed25519"], ["expires", expires], ["nonce", nonce()],
    ["tag", "web-bot-auth"],
  ]);
  const base = signatureBase([["@authority", authority], ["signature-agent", agent]], params);
  return {
    signature_input: `${REQUEST_LABEL}=${params}`,
    signature: `${REQUEST_LABEL}=:${await sign(key, base)}:`,
    signature_agent: agent,
    expires,
  };
}

/** The directory's own signature, over the host it was requested from (the req flag). */
export async function signDirectory(key: SigningKey, authority: string, now: number): Promise<{ input: string; signature: string }> {
  const params = signatureParams(["@authority;req"], [
    ["alg", "ed25519"], ["keyid", key.kid], ["nonce", nonce()], ["created", now],
    ["expires", now + DIRECTORY_TTL_SECONDS], ["tag", "http-message-signatures-directory"],
  ]);
  const base = signatureBase([["@authority;req", authority]], params);
  return { input: `${DIRECTORY_LABEL}=${params}`, signature: `${DIRECTORY_LABEL}=:${await sign(key, base)}:` };
}

// ---- validation -------------------------------------------------------------------------------- //

// A bare host, optionally with a port: lowercase ASCII labels, no scheme, path, userinfo or
// brackets. The shell sends `new URL(url).host`, which is exactly this (IDNs arrive as punycode).
const HOST = /^(?=[a-z0-9.-]{1,253}(?::\d{1,5})?$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::\d{1,5})?$/;

/** Lowercased, or null when it is not a string holding a bare host. */
export function normaliseAuthority(v: unknown): string | null {
  if (typeof v !== "string") return null;
  const a = v.trim().toLowerCase();
  return HOST.test(a) ? a : null;
}

// ---- routes ------------------------------------------------------------------------------------ //

const notConfigured = () => json({ error: "not_configured" }, 503);

/** Count one signature for this account this minute; false at the limit. A guarded upsert, as
 * signin.allowSend, so parallel calls cannot slip past; old minutes are swept in the same batch. */
async function allowSign(db: D1Database, user: string, now: number): Promise<boolean> {
  const minute = Math.floor(now / 60);
  const [, r] = await db.batch<{ n: number }>([
    db.prepare("DELETE FROM sign_limits WHERE minute < ?").bind(minute),
    db.prepare(
      "INSERT INTO sign_limits(user, minute, n) VALUES(?, ?, 1) " +
      "ON CONFLICT(user, minute) DO UPDATE SET n = n + 1 WHERE n < ? RETURNING n").bind(user, minute, SIGNS_PER_MINUTE),
  ]);
  return r!.results.length > 0;
}

/** May this account have requests signed? A verified e-mail (attached only after a proven code or
 * Google sign-in, src/accounts.ts signInWithEmail) or a paid plan that is still running. */
async function maySign(db: D1Database, user: string, now: number): Promise<boolean> {
  const row = await db.prepare(
    "SELECT a.email, u.plan, u.pass_until FROM accounts a LEFT JOIN users u ON u.user = a.account_id " +
    "WHERE a.account_id = ?").bind(user).first<{ email: string | null; plan: string | null; pass_until: number | null }>();
  if (!row) return false;
  if (row.email) return true;
  const plan = PLANS[row.plan ?? ""];
  return Boolean(plan && isPass(plan) && (row.pass_until ?? 0) > now);
}

const dayOfSeconds = (now: number) => new Date(now * 1000).toISOString().slice(0, 10);

async function sha256Hex(s: string): Promise<string> {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(s)));
  return [...d].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Count one signature today for the account, the client network and the service; null when the
 * account or the network is at its daily cap, else the account's and the service's new counts.
 * Guarded upserts as in allowSign; past days are swept in the same batch. */
async function allowDaily(db: D1Database, env: Env, user: string, ip: string, now: number):
    Promise<{ account: number; total: number } | null> {
  const day = dayOfSeconds(now);
  const perAccount = intVar(env.SIGNS_PER_ACCOUNT_PER_DAY, SIGNS_PER_ACCOUNT_PER_DAY);
  const perIp = intVar(env.SIGNS_PER_IP_PER_DAY, SIGNS_PER_IP_PER_DAY);
  const upsert = "INSERT INTO sign_daily(key, day, n) VALUES(?, ?, 1) " +
                 "ON CONFLICT(key, day) DO UPDATE SET n = n + 1 WHERE n < ? RETURNING n";
  const [, u, i] = await db.batch<{ n: number }>([
    db.prepare("DELETE FROM sign_daily WHERE day < ?").bind(day),
    db.prepare(upsert).bind(`u:${user}`, day, perAccount),
    db.prepare(upsert).bind(`i:${await sha256Hex(ip)}`, day, perIp),
  ]);
  if (!u!.results.length || !i!.results.length) return null;
  const t = await db.prepare("INSERT INTO sign_daily(key, day, n) VALUES('all', ?, 1) " +
                             "ON CONFLICT(key, day) DO UPDATE SET n = n + 1 RETURNING n").bind(day).first<{ n: number }>();
  return { account: u!.results[0]!.n, total: t?.n ?? 0 };
}

/** Count `site` among the account's application sites today; false when it is a new site and the
 * account is at its daily number of sites. One guarded insert: a site already counted is free. */
async function allowSite(db: D1Database, env: Env, user: string, site: string, now: number): Promise<boolean> {
  const day = dayOfSeconds(now);
  const cap = intVar(env.SIGN_SITES_PER_DAY, SIGN_SITES_PER_DAY);
  const [, , seen] = await db.batch<{ n: number }>([
    db.prepare("DELETE FROM sign_sites WHERE day < ?").bind(day),
    db.prepare(
      "INSERT INTO sign_sites(user, day, site) SELECT ?, ?, ? " +
      "WHERE (SELECT COUNT(*) FROM sign_sites WHERE user = ? AND day = ?) < ? " +
      "ON CONFLICT(user, day, site) DO NOTHING").bind(user, day, site, user, day, cap),
    db.prepare("SELECT 1 AS n FROM sign_sites WHERE user = ? AND day = ? AND site = ?").bind(user, day, site),
  ]);
  return seen!.results.length > 0;
}

const refuse = (reason: string, status: number, error: string) => {
  console.log(`agent/sign: refused (${reason})`);
  return json({ error, reason }, status);
};

/** POST /agent/sign {method, authority, path, site} -> {signature_input, signature, signature_agent,
 * expires}. `site` is the apply run's application host; it is needed unless the authority is a
 * hiring system's own host. */
export async function agentSign(req: Request, env: Env, now: number): Promise<Response> {
  const key = keyFrom(env);
  if (!key) return notConfigured();
  const user = await identify(req, env);
  if (!user) return json({ error: "no user" }, 401);
  if (!(await maySign(env.DB, user, now))) {
    return refuse("sign_in_required", 403, "signing needs a signed-in account");
  }
  const b = await body(req);
  const authority = normaliseAuthority(b.authority);
  if (!authority) return json({ error: "authority must be a bare host", reason: "bad_authority" }, 400);
  if (isProhibitedHost(authority)) return refuse("prohibited_site", 403, "this site does not allow automated applications");
  let site: string | null = null;
  if (!isAtsHost(authority)) {
    const s = normaliseAuthority(b.site);
    if (!s || isProhibitedHost(s) || !onSite(authority, s)) {
      return refuse("not_apply_site", 403, "only a job application site of an apply run is signed");
    }
    site = siteKey(s);
  }
  if (!(await allowSign(env.DB, user, now))) {
    console.log(`agent/sign: an account hit the ${SIGNS_PER_MINUTE}/minute limit`);
    return json({ error: "too many signatures this minute", reason: "rate_limited" }, 429);
  }
  const ip = req.headers.get("CF-Connecting-IP") || "unknown";
  const counts = await allowDaily(env.DB, env, user, ip, now);
  if (!counts) return refuse("daily_limit", 429, "too many signatures today");
  if (site && !(await allowSite(env.DB, env, user, site, now))) {
    return refuse("site_limit", 429, "too many different sites today");
  }
  if (counts.account % LOG_EVERY === 0) console.log(`agent/sign: an account reached ${counts.account} signatures today`);
  if (counts.total % LOG_TOTAL_EVERY === 0) console.log(`agent/sign: ${counts.total} signatures today in all`);
  return json(await signRequest(await key, authority, now), 200, { "Cache-Control": "no-store" });
}

/** GET /.well-known/http-message-signatures-directory: the public key set, signed. Served on
 * every host this Worker answers; the one Cloudflare reads is sponsorjobs.ai, the registered
 * Signature-Agent (wrangler.jsonc routes it here). */
export async function directory(req: Request, env: Env, now: number): Promise<Response> {
  const key = keyFrom(env);
  if (!key) return notConfigured();
  const k = await key;
  const { input, signature } = await signDirectory(k, new URL(req.url).host, now);
  return json({ keys: [{ kty: "OKP", crv: "Ed25519", kid: k.kid, x: k.x }] }, 200, {
    "content-type": DIRECTORY_CONTENT_TYPE,
    "Cache-Control": `max-age=${DIRECTORY_TTL_SECONDS}`,
    "Signature-Input": input,
    "Signature": signature,
  });
}
