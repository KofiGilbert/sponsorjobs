// Web Bot Auth for the apply agent: the key directory and POST /agent/sign. Every signature is
// checked the way a site would check it: the public key is taken from the directory, the keyid is
// recomputed from it, the RFC 9421 signature base is rebuilt from the headers alone, and WebCrypto
// verifies. Nothing in here uses the module's private helpers for that. No network.
//
// Against Cloudflare itself: a signed request from the shell to
// https://crawltest.com/cdn-cgi/web-bot-auth answers 200 once the key is registered (401 for an
// unknown key, 400 for a malformed signature). That is a manual step in docs/signed-agent.md, not
// a test here.
import { describe, expect, it } from "vitest";
import type { Env } from "../src/env";
import {
  DIRECTORY_CONTENT_TYPE, DIRECTORY_PATH, DIRECTORY_TTL_SECONDS, REQUEST_TTL_SECONDS, SIGNATURE_AGENT, SIGNS_PER_MINUTE,
  normaliseAuthority, signatureBase, signatureParams,
} from "../src/botauth";
import { call, newAccount } from "./helpers";

// A fixed seed (32 bytes, base64url), so the public key and the keyid are the same on every run.
const SEED = "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA";
const KEYED: Partial<Env> = { AGENT_SIGNING_KEY: SEED };

const enc = new TextEncoder();
const b64url = (bytes: Uint8Array) =>
  btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const fromB64 = (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

/** Call the Worker and keep the raw headers too (helpers.call drops them). */
async function raw(path: string, o: { method?: string; body?: unknown; token?: string; env?: Partial<Env> } = {}) {
  const { default: worker } = await import("../src/index");
  const { env } = await import("./helpers");
  const headers: Record<string, string> = {};
  if (o.token) headers.Authorization = `Bearer ${o.token}`;
  if (o.body !== undefined) headers["content-type"] = "application/json";
  const res = await worker.fetch(
    new Request(`https://broker.test${path}`, { method: o.method ?? (o.body !== undefined ? "POST" : "GET"),
      headers, body: o.body === undefined ? undefined : JSON.stringify(o.body) }),
    { ...env, ...(o.env ?? {}) } as Env,
  );
  const text = await res.text();
  let data: any = null;
  try { data = JSON.parse(text); } catch { /* not JSON */ }
  return { status: res.status, headers: res.headers, data, text };
}

/** The public key as a site would get it: from the directory. */
async function publicKey(): Promise<{ key: CryptoKey; x: string; kid: string }> {
  const r = await raw(DIRECTORY_PATH, { env: KEYED });
  const jwk = r.data.keys[0];
  const key = await crypto.subtle.importKey("jwk", { kty: "OKP", crv: "Ed25519", x: jwk.x }, { name: "Ed25519" }, false, ["verify"]);
  return { key, x: jwk.x, kid: jwk.kid };
}

/** Split "label=(...)...;tag=..." and "label=:base64:" into their parts. */
function parse(signatureInput: string, signature: string) {
  const m = /^([a-z0-9]+)=(\(.*)$/.exec(signatureInput);
  if (!m) throw new Error(`bad Signature-Input: ${signatureInput}`);
  const [, label, params] = m;
  const s = new RegExp(`^${label}=:([A-Za-z0-9+/=]+):$`).exec(signature);
  if (!s) throw new Error(`bad Signature: ${signature}`);
  const components = /^\(([^)]*)\)/.exec(params!)![1]!.split(" ").map((c) => c.replace(/^"|"$/g, ""));
  const param = (name: string) => {
    const q = new RegExp(`;${name}="([^"]*)"`).exec(params!);
    if (q) return q[1]!;
    const n = new RegExp(`;${name}=(\\d+)`).exec(params!);
    return n ? Number(n[1]) : undefined;
  };
  return { label: label!, params: params!, components, bytes: fromB64(s[1]!), param };
}

/** Rebuild the base from the covered components and verify, as RFC 9421 section 3.2 says. */
async function verifies(key: CryptoKey, values: Record<string, string>, signatureInput: string, signature: string) {
  const p = parse(signatureInput, signature);
  const base = p.components.map((c) => `"${c}": ${values[c] ?? ""}`).concat(`"@signature-params": ${p.params}`).join("\n");
  return crypto.subtle.verify({ name: "Ed25519" }, key, p.bytes, enc.encode(base));
}

const sign = (token: string | undefined, authority: unknown, env: Partial<Env> = KEYED) =>
  raw("/agent/sign", { body: { method: "GET", authority, path: "/jobs/1" }, token, env });

describe("the key directory", () => {
  it("is a JWKS with one Ed25519 key whose kid is the RFC 8037 thumbprint of the key", async () => {
    const r = await raw(DIRECTORY_PATH, { env: KEYED });
    expect(r.status).toBe(200);
    expect(r.headers.get("content-type")).toBe(DIRECTORY_CONTENT_TYPE);
    expect(r.headers.get("cache-control")).toBe(`max-age=${DIRECTORY_TTL_SECONDS}`);
    expect(r.data.keys).toHaveLength(1);
    const k = r.data.keys[0];
    expect(Object.keys(k).sort()).toEqual(["crv", "kid", "kty", "x"]);   // no private member
    expect(k).toMatchObject({ kty: "OKP", crv: "Ed25519" });
    expect(k.x).toMatch(/^[A-Za-z0-9_-]{43}$/);
    const canonical = `{"crv":"Ed25519","kty":"OKP","x":"${k.x}"}`;
    const expected = b64url(new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(canonical))));
    expect(k.kid).toBe(expected);
    expect(r.text).toMatch(/^[\x20-\x7e]*$/);
  });

  it("is the same key every time for the same seed", async () => {
    const a = await publicKey();
    const b = await publicKey();
    expect(a.x).toBe(b.x);
    expect(a.kid).toBe(b.kid);
  });

  it("is signed over its own @authority with the directory tag, and the signature verifies", async () => {
    const { key, kid } = await publicKey();
    const r = await raw(DIRECTORY_PATH, { env: KEYED });
    const input = r.headers.get("signature-input")!;
    const signature = r.headers.get("signature")!;
    expect(input).toMatch(/^sig1=\("@authority"\);alg="ed25519";keyid="[^"]+";nonce="[^"]+";created=\d+;expires=\d+;tag="http-message-signatures-directory"$/);
    expect(signature).toMatch(/^sig1=:[A-Za-z0-9+/]+=*:$/);
    const p = parse(input, signature);
    expect(p.components).toEqual(["@authority"]);
    expect(p.param("keyid")).toBe(kid);
    expect(p.param("alg")).toBe("ed25519");
    const now = Math.floor(Date.now() / 1000);
    expect(Math.abs((p.param("created") as number) - now)).toBeLessThanOrEqual(2);
    expect((p.param("expires") as number) - (p.param("created") as number)).toBe(DIRECTORY_TTL_SECONDS);
    expect(await verifies(key, { "@authority": "broker.test" }, input, signature)).toBe(true);
    expect(await verifies(key, { "@authority": "sponsorjobs.ai" }, input, signature)).toBe(false);  // another host
  });

  it("answers HEAD like GET and 503 not_configured without the secret", async () => {
    expect((await raw(DIRECTORY_PATH, { method: "HEAD", env: KEYED })).status).toBe(200);
    const r = await raw(DIRECTORY_PATH, { env: { AGENT_SIGNING_KEY: "" } });
    expect(r.status).toBe(503);
    expect(r.data).toEqual({ error: "not_configured" });
  });

  it("refuses a seed that is not 32 bytes rather than publishing a wrong key", async () => {
    const r = await raw(DIRECTORY_PATH, { env: { AGENT_SIGNING_KEY: "dG9vLXNob3J0" } });
    expect(r.status).toBe(500);
  });
});

describe("POST /agent/sign", () => {
  it("answers the three headers in Cloudflare's shape, and the signature verifies for that host", async () => {
    const { key, kid } = await publicKey();
    const { token } = await newAccount();
    const r = await sign(token, "Jobs.Example.COM");
    expect(r.status).toBe(200);
    expect(r.headers.get("cache-control")).toBe("no-store");
    expect(Object.keys(r.data).sort()).toEqual(["expires", "signature", "signature_agent", "signature_input"]);
    expect(r.data.signature_agent).toBe(`"${SIGNATURE_AGENT}"`);
    expect(r.data.signature_agent).toBe('"https://sponsorjobs.ai"');
    expect(r.data.signature_input).toMatch(
      /^sig2=\("@authority" "signature-agent"\);created=\d+;keyid="[^"]+";alg="ed25519";expires=\d+;nonce="[A-Za-z0-9+/]+=*";tag="web-bot-auth"$/);
    expect(r.data.signature).toMatch(/^sig2=:[A-Za-z0-9+/]+=*:$/);
    expect(r.text).toMatch(/^[\x20-\x7e]*$/);
    const p = parse(r.data.signature_input, r.data.signature);
    expect(p.param("keyid")).toBe(kid);
    expect(p.param("tag")).toBe("web-bot-auth");
    expect(fromB64(p.param("nonce") as string)).toHaveLength(16);
    const now = Math.floor(Date.now() / 1000);
    expect(Math.abs((p.param("created") as number) - now)).toBeLessThanOrEqual(2);
    expect(p.param("expires")).toBe((p.param("created") as number) + REQUEST_TTL_SECONDS);
    expect(r.data.expires).toBe(p.param("expires"));
    // The authority was lowercased: the signature is over "jobs.example.com", not what was sent.
    const values = { "@authority": "jobs.example.com", "signature-agent": r.data.signature_agent };
    expect(await verifies(key, values, r.data.signature_input, r.data.signature)).toBe(true);
    expect(await verifies(key, { ...values, "@authority": "other.example.com" }, r.data.signature_input, r.data.signature)).toBe(false);
    expect(await verifies(key, { ...values, "signature-agent": '"https://evil.test"' }, r.data.signature_input, r.data.signature)).toBe(false);
  });

  it("every signature is fresh (a new nonce), and a host with a port is fine", async () => {
    const { token } = await newAccount();
    const a = await sign(token, "jobs.example.com:8443");
    const b = await sign(token, "jobs.example.com:8443");
    expect(a.status).toBe(200);
    expect(b.status).toBe(200);
    expect(a.data.signature).not.toBe(b.data.signature);
    expect(a.data.signature_input).not.toBe(b.data.signature_input);
  });

  it("401 without a token, 503 not_configured without the key (before any auth)", async () => {
    expect((await sign(undefined, "jobs.example.com")).status).toBe(401);
    expect((await sign("not-a-token", "jobs.example.com")).status).toBe(401);
    const r = await sign(undefined, "jobs.example.com", { AGENT_SIGNING_KEY: "" });
    expect(r.status).toBe(503);
    expect(r.data).toEqual({ error: "not_configured" });
  });

  it("400 bad_authority for anything that is not a bare host", async () => {
    const { token } = await newAccount();
    for (const bad of ["", "https://jobs.example.com", "jobs.example.com/apply", "user@jobs.example.com",
                       "jobs example.com", "-jobs.example.com", "jobs.example.com:", "x".repeat(254), 42, null]) {
      const r = await sign(token, bad);
      expect(r.status, String(bad)).toBe(400);
      expect(r.data.reason).toBe("bad_authority");
    }
  });

  it(`429 rate_limited after ${SIGNS_PER_MINUTE} signatures in a minute, per account`, async () => {
    const { token } = await newAccount();
    for (let i = 0; i < SIGNS_PER_MINUTE; i++) expect((await sign(token, "jobs.example.com")).status, `call ${i}`).toBe(200);
    const r = await sign(token, "jobs.example.com");
    expect(r.status).toBe(429);
    expect(r.data.reason).toBe("rate_limited");
    const other = await newAccount();
    expect((await sign(other.token, "jobs.example.com")).status).toBe(200);   // another account is fine
  });
});

describe("the pieces", () => {
  it("normaliseAuthority lowercases a bare host and refuses the rest", () => {
    expect(normaliseAuthority(" Boards.Greenhouse.IO ")).toBe("boards.greenhouse.io");
    expect(normaliseAuthority("localhost:8080")).toBe("localhost:8080");
    expect(normaliseAuthority("xn--bcher-kva.example")).toBe("xn--bcher-kva.example");
    expect(normaliseAuthority("jobs.example.com.")).toBeNull();
    expect(normaliseAuthority("[::1]")).toBeNull();
    expect(normaliseAuthority(undefined)).toBeNull();
  });

  it("the signature base is one line per component, then @signature-params, LF-joined, no trailing newline", () => {
    const params = signatureParams(["@authority", "signature-agent"], [["created", 1], ["keyid", "k"], ["tag", "web-bot-auth"]]);
    expect(params).toBe('("@authority" "signature-agent");created=1;keyid="k";tag="web-bot-auth"');
    expect(signatureBase([["@authority", "a.test"], ["signature-agent", '"https://sponsorjobs.ai"']], params)).toBe(
      '"@authority": a.test\n"signature-agent": "https://sponsorjobs.ai"\n"@signature-params": ' + params);
  });

  it("helpers.call sees the same routes (plain JSON, no headers needed)", async () => {
    const r = await call(DIRECTORY_PATH, { env: KEYED });
    expect(r.status).toBe(200);
    expect(r.data.keys[0].kty).toBe("OKP");
  });
});
