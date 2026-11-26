// GET /v1/feed/<file>: the job list for SponsorJobs installs only (docs/feed.md, "The locked
// route"). The app (sourcing/feedclient.py) sends its broker account token; anyone without a valid
// one gets 401 no_account, so a copycat app cannot read our crawl or spend our free allowance.
//
// Files come straight from the feed bucket through the FEED_BUCKET binding (the same bucket the
// feed workflow uploads to): no secret, no presigned URL, nothing to create on Cloudflare. The body
// is streamed through untouched (the Worker never reads or buffers it, so it costs almost no CPU),
// with the stored Content-Encoding passed through as is (`encodeBody: "manual"`: the parts are
// stored gzipped and the app checks their sha256 on exactly those bytes), and ETag /
// If-None-Match answered with a 304. One Worker request per file.
//
// Abuse: a per-network limit runs before the token is looked up (so a flood of made-up tokens
// cannot hammer the account database), then a per-account limit. Only the files the app reads are
// served (FEED_FILE): never the upload history, the sponsor index or any other key in the bucket.
// Nothing here logs a token, an account id or a path.

import type { Env } from "./env";
import { accountFor, clientIp, overLimit } from "./installauth";

export const FEED_ROUTE = /^\/v1\/feed\/(.+)$/;
/** The files the app reads: the manifest, the single list, list parts, description shards. */
export const FEED_FILE = /^(?:manifest\.json|jobs\.json\.gz|(?:jd|parts)\/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.json\.gz)$/;

const json = (data: unknown, status: number, extra: Record<string, string> = {}) =>
  new Response(JSON.stringify(data), {
    status, headers: { "content-type": "application/json", "cache-control": "no-store", ...extra },
  });

/** The bucket key for a feed file: FEED_R2_PREFIX ("feed" by default, the workflow's R2_PREFIX)
 * plus the file's path. */
export function feedKey(env: Env, file: string): string {
  const prefix = (env.FEED_R2_PREFIX ?? "feed").replace(/^\/+|\/+$/g, "");
  return prefix ? `${prefix}/${file}` : file;
}

export async function handleFeed(req: Request, env: Env, nowMs: number): Promise<Response> {
  if (await overLimit(env.FEED_IP_LIMITER, `ip:${clientIp(req)}`)) {
    return json({ error: "too many requests from this network; wait a minute", reason: "rate_limited" }, 429,
                { "retry-after": "60" });
  }
  const account = await accountFor(req, env, nowMs);
  if (!account) return json({ error: "the job list is for SponsorJobs accounts", reason: "no_account" }, 401);
  if (await overLimit(env.FEED_ACCOUNT_LIMITER, `a:${account}`)) {
    return json({ error: "too many requests; wait a minute", reason: "rate_limited" }, 429, { "retry-after": "60" });
  }
  // Not decoded: a feed file name never needs percent-encoding, so an encoded one is not a feed file.
  const file = new URL(req.url).pathname.match(FEED_ROUTE)?.[1] ?? "";
  if (!FEED_FILE.test(file)) return json({ error: "not found" }, 404);
  if (!env.FEED_BUCKET) {
    return json({ error: "the job list is not set up on this server yet", reason: "feed_not_configured" }, 503);
  }

  const obj = await env.FEED_BUCKET.get(feedKey(env, file), { onlyIf: req.headers });
  if (!obj) return json({ error: "not found" }, 404);
  const headers = new Headers();
  obj.writeHttpMetadata(headers);         // Content-Type, Content-Encoding, Cache-Control as uploaded
  headers.set("etag", obj.httpEtag);
  // The uploader marks parts `public, immutable`; this copy answered a request with a token, so
  // only the app may keep it.
  const cc = headers.get("cache-control") ?? "";
  headers.set("cache-control", cc.replace(/\bpublic\b/, "private") || "private, no-cache");
  if (!("body" in obj)) return new Response(null, { status: 304, headers });   // If-None-Match matched
  headers.set("content-length", String(obj.size));
  return new Response(req.method === "HEAD" ? null : obj.body, { status: 200, headers, encodeBody: "manual" });
}
