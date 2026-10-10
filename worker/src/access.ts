// Cloudflare Access check for the admin dashboard (src/admin.ts).
//
// Cloudflare Access sits in front of admin.sponsorjobs.ai and only lets the founder's e-mail
// through. As a second lock, the Worker itself re-checks the signed token Access adds to every
// request it lets through (the Cf-Access-Jwt-Assertion header), so a mistake in the Access policy,
// or a request that reaches the Worker some other way, still gets 403. Checked here:
//   - the signature, against the team's public keys (https://<team>.cloudflareaccess.com/cdn-cgi/access/certs)
//   - the audience: the AUD tag of THIS Access application (another app's token is refused)
//   - the issuer (the team), the expiry and not-before
//   - optionally, that the e-mail is one of ADMIN_EMAILS
// With ACCESS_TEAM_DOMAIN or ACCESS_AUD unset, every admin request is refused: the dashboard is
// closed until it is configured, never open by default.
// Docs: https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/

export interface AccessConfig {
  ACCESS_TEAM_DOMAIN?: string;  // "myteam" or "myteam.cloudflareaccess.com"
  ACCESS_AUD?: string;          // the Access application's "Application Audience (AUD) Tag"
  ADMIN_EMAILS?: string;        // optional, comma-separated: only these e-mails, even if Access lets more in
}

export interface AccessIdentity { email: string }

const CERTS_TTL_MS = 60 * 60 * 1000;   // Access rotates keys every 6 weeks; an hour's cache is plenty
const CLOCK_SKEW = 60;                 // seconds of clock difference tolerated on exp / nbf

interface Jwk { kid: string; kty: string; n: string; e: string; alg?: string }
const certCache = new Map<string, { keys: Jwk[]; at: number }>();

/** Tests only: forget cached keys so a stubbed certs endpoint is fetched again. */
export function resetAccessCertCache(): void { certCache.clear(); }

/** "myteam" | "myteam.cloudflareaccess.com" | "https://myteam.cloudflareaccess.com/" -> the host. */
export function teamHost(value: string): string {
  const v = value.trim().replace(/^https?:\/\//, "").replace(/\/.*$/, "").toLowerCase();
  if (!v) return "";
  return v.includes(".") ? v : `${v}.cloudflareaccess.com`;
}

function b64urlBytes(s: string): Uint8Array {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

const b64urlJson = (s: string): Record<string, unknown> =>
  JSON.parse(new TextDecoder().decode(b64urlBytes(s))) as Record<string, unknown>;

async function fetchKeys(host: string, force: boolean): Promise<Jwk[]> {
  const hit = certCache.get(host);
  if (hit && !force && Date.now() - hit.at < CERTS_TTL_MS) return hit.keys;
  const res = await fetch(`https://${host}/cdn-cgi/access/certs`);
  if (!res.ok) throw new Error(`access certs: HTTP ${res.status}`);
  const body = (await res.json()) as { keys?: Jwk[] };
  const keys = Array.isArray(body.keys) ? body.keys.filter((k) => k && k.kty === "RSA" && k.kid) : [];
  certCache.set(host, { keys, at: Date.now() });
  return keys;
}

/** The verified identity behind an admin request, or null (answer 403). Never throws. */
export async function verifyAccess(req: Request, cfg: AccessConfig, now: number): Promise<AccessIdentity | null> {
  try {
    const host = teamHost(cfg.ACCESS_TEAM_DOMAIN ?? "");
    const aud = (cfg.ACCESS_AUD ?? "").trim();
    if (!host || !aud) return null;                                  // not configured: closed
    const jwt = (req.headers.get("Cf-Access-Jwt-Assertion") ?? "").trim();
    const parts = jwt.split(".");
    if (parts.length !== 3) return null;
    const [h, p, sig] = parts as [string, string, string];
    const header = b64urlJson(h);
    if (header.alg !== "RS256" || typeof header.kid !== "string") return null;

    let keys = await fetchKeys(host, false);
    let jwk = keys.find((k) => k.kid === header.kid);
    if (!jwk) {                                                      // keys rotated: look once more
      keys = await fetchKeys(host, true);
      jwk = keys.find((k) => k.kid === header.kid);
    }
    if (!jwk) return null;
    const key = await crypto.subtle.importKey(
      "jwk", { kty: "RSA", n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    const ok = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5", key, b64urlBytes(sig), new TextEncoder().encode(`${h}.${p}`));
    if (!ok) return null;

    const claims = b64urlJson(p);
    const auds = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (!auds.includes(aud)) return null;
    if (claims.iss !== `https://${host}`) return null;
    if (typeof claims.exp !== "number" || claims.exp + CLOCK_SKEW < now) return null;
    if (typeof claims.nbf === "number" && claims.nbf - CLOCK_SKEW > now) return null;
    const email = typeof claims.email === "string" ? claims.email.trim().toLowerCase() : "";
    if (!email) return null;                                         // a service token, not a person
    const allowed = (cfg.ADMIN_EMAILS ?? "").split(",").map((e) => e.trim().toLowerCase()).filter(Boolean);
    if (allowed.length > 0 && !allowed.includes(email)) return null;
    return { email };
  } catch (err) {
    console.error("access check failed:", err instanceof Error ? err.message : err);
    return null;
  }
}
