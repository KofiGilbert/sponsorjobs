// GET /v1/feed/<file>: the job list for SponsorJobs installs only (src/feed.ts). No token, a
// made-up, revoked or suspended one: 401. A valid one: the file streamed from the feed bucket with
// its stored bytes and headers (gzip passed through, never re-compressed), 304 on a matching ETag,
// only the files the app reads, and the per-account / per-network rate limits.
import { beforeAll, describe, expect, it } from "vitest";
import worker from "../src/index";
import type { Env } from "../src/env";
import { forgetAccounts } from "../src/installauth";
import { call, env, freshIp, newAccount } from "./helpers";

const bucket = env.FEED_BUCKET!;
const MANIFEST = JSON.stringify({ generated_at: "2026-11-25T00:00:00Z", count: 2 });
let PART: Uint8Array;

async function gz(text: string): Promise<Uint8Array> {
  const stream = new Blob([text]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** One request straight to the Worker, keeping the raw response (bytes and headers). */
async function feed(path: string, o: { token?: string; ip?: string; headers?: Record<string, string>;
                                       env?: Partial<Env>; method?: string } = {}) {
  const headers: Record<string, string> = { "CF-Connecting-IP": o.ip ?? freshIp(), ...(o.headers ?? {}) };
  if (o.token) headers.Authorization = `Bearer ${o.token}`;
  return worker.fetch(new Request(`https://broker.test/v1/feed/${path}`, { method: o.method ?? "GET", headers }),
                      { ...env, DEV_HEADER_AUTH: "0", ...(o.env ?? {}) } as Env);
}

beforeAll(async () => {
  PART = await gz(JSON.stringify({ bucket: "2026-W47", jobs: [{ source_id: "a" }, { source_id: "b" }] }));
  // Stored exactly as scripts/upload_feed.py stores them, under the R2_PREFIX "feed".
  await bucket.put("feed/manifest.json", MANIFEST,
                   { httpMetadata: { contentType: "application/json", cacheControl: "no-cache" } });
  await bucket.put("feed/parts/s-0123456789abcdef0123.json.gz", PART, {
    httpMetadata: { contentType: "application/json", contentEncoding: "gzip",
                    cacheControl: "public, max-age=31536000, immutable" } });
  await bucket.put("feed/jd/0a.json.gz", PART, { httpMetadata: { contentType: "application/json", contentEncoding: "gzip" } });
  await bucket.put("feed/manifest-history.json", "{}");
  await bucket.put("feed/sponsors/manifest.json", "{}");
  await bucket.put("state/latest.json", "{}");
});

describe("who may read the job list", () => {
  it("401 no_account without a token", async () => {
    const r = await feed("manifest.json");
    expect(r.status).toBe(401);
    expect((await r.json() as any).reason).toBe("no_account");
    expect(r.headers.get("cache-control")).toBe("no-store");
  });

  it("401 for a made-up token, a revoked token and a suspended account", async () => {
    expect((await feed("manifest.json", { token: "made-up-" + crypto.randomUUID() })).status).toBe(401);
    const revoked = await newAccount();
    await call("/account/revoke", { method: "POST", token: revoked.token });
    expect((await feed("manifest.json", { token: revoked.token })).status).toBe(401);
    const suspended = await newAccount();
    await env.DB.prepare("UPDATE accounts SET suspended_at = 1 WHERE account_id = ?").bind(suspended.id).run();
    expect((await feed("manifest.json", { token: suspended.token })).status).toBe(401);
  });

  it("a token revoked after use stops once the account cache's minute is over", async () => {
    const a = await newAccount();
    expect((await feed("manifest.json", { token: a.token })).status).toBe(200);
    await call("/account/revoke", { method: "POST", token: a.token });
    forgetAccounts();
    expect((await feed("manifest.json", { token: a.token })).status).toBe(401);
  });

  it("the dev header is no account in production", async () => {
    expect((await feed("manifest.json", { headers: { "X-Tailor-User": "acct_x" } })).status).toBe(401);
  });

  it("checks the token before the path: a stranger cannot even probe which files exist", async () => {
    expect((await feed("nope.txt")).status).toBe(401);
    expect((await feed("parts/missing.json.gz")).status).toBe(401);
  });
});

describe("serving a file", () => {
  let token = "";
  beforeAll(async () => { token = (await newAccount()).token; });

  it("the manifest, with its ETag, private to the app", async () => {
    const r = await feed("manifest.json", { token });
    expect(r.status).toBe(200);
    expect(await r.text()).toBe(MANIFEST);
    expect(r.headers.get("content-type")).toBe("application/json");
    expect(r.headers.get("etag")).toMatch(/^".+"$/);
    expect(r.headers.get("cache-control")).toBe("no-cache");
  });

  it("304 when If-None-Match matches, 200 when it does not", async () => {
    const etag = (await feed("manifest.json", { token })).headers.get("etag")!;
    const same = await feed("manifest.json", { token, headers: { "If-None-Match": etag } });
    expect(same.status).toBe(304);
    expect(same.headers.get("etag")).toBe(etag);
    expect(await same.text()).toBe("");
    expect((await feed("manifest.json", { token, headers: { "If-None-Match": '"stale"' } })).status).toBe(200);
  });

  it("a gzipped part: the stored bytes as they are, Content-Encoding passed through, not public", async () => {
    const r = await feed("parts/s-0123456789abcdef0123.json.gz", { token, headers: { "Accept-Encoding": "gzip" } });
    expect(r.status).toBe(200);
    expect(r.headers.get("content-encoding")).toBe("gzip");
    expect(r.headers.get("cache-control")).toBe("private, max-age=31536000, immutable");
    const body = new Uint8Array(await r.arrayBuffer());
    expect([...body]).toEqual([...PART]);                // the sha256 the app checks is on these bytes
    expect(r.headers.get("content-length")).toBe(String(PART.byteLength));
  });

  it("a description shard, and HEAD with no body", async () => {
    expect((await feed("jd/0a.json.gz", { token })).status).toBe(200);
    const head = await feed("jd/0a.json.gz", { token, method: "HEAD" });
    expect(head.status).toBe(200);
    expect(await head.text()).toBe("");
  });

  it("only the files the app reads: never the history, the sponsor index, the crawl state or a trick path", async () => {
    for (const p of ["manifest-history.json", "sponsors/manifest.json", "../state/latest.json", "state/latest.json",
                     "parts/../manifest-history.json", "parts/.hidden.json.gz", "parts/x.json", "jd/%2e%2e.json.gz",
                     "parts/a/b.json.gz", ""]) {
      expect((await feed(p, { token })).status, p).toBe(404);
    }
  });

  it("404 for a file the bucket does not hold (a part replaced by a newer build)", async () => {
    expect((await feed("parts/s-ffffffffffffffffffff.json.gz", { token })).status).toBe(404);
  });

  it("503 feed_not_configured without the bucket binding", async () => {
    const r = await feed("manifest.json", { token, env: { FEED_BUCKET: undefined } });
    expect(r.status).toBe(503);
    expect((await r.json() as any).reason).toBe("feed_not_configured");
  });

  it("only GET (and HEAD)", async () => {
    expect((await call("/v1/feed/manifest.json", { method: "POST", body: {}, token })).status).toBe(405);
  });

  it("FEED_R2_PREFIX moves the files", async () => {
    await bucket.put("other/manifest.json", "{\"count\":9}");
    const r = await feed("manifest.json", { token, env: { FEED_R2_PREFIX: "other" } });
    expect(await r.text()).toBe("{\"count\":9}");
  });
});

describe("rate limits", () => {
  it("120 files a minute per account", async () => {
    const a = await newAccount();
    let last = 0;
    for (let i = 0; i < 121; i++) last = (await feed("manifest.json", { token: a.token })).status;
    expect(last).toBe(429);
    expect((await feed("manifest.json", { token: (await newAccount()).token })).status).toBe(200);
  });

  it("600 a minute per network, counted before the token is looked up", async () => {
    const ip = freshIp();
    let last = 0;
    for (let i = 0; i < 601; i++) {
      last = (await feed("manifest.json", { ip, headers: { "X-Tailor-User": `acct_f${i}` },
                                            env: { DEV_HEADER_AUTH: "1" } })).status;
    }
    expect(last).toBe(429);
    expect((await feed("manifest.json", { ip })).status).toBe(429);   // no token: still the network's 429
  });
});
