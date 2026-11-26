// /v1/jobs/search and /v1/jobs/candidates: parameters, FTS escaping, cursors (round trip, 409 on
// a new index, tampering), the 1,000-result depth limit, the count, the bounded scan, the daily
// budget, the page limits and the rate limits. Facet semantics are checked against Python in
// search-parity.test.ts.
import { beforeEach, describe, expect, it } from "vitest";
import { decodeCursor, encodeCursor, forgetIndexVersion, ftsAll } from "../src/search";
import { forgetAccounts } from "../src/installauth";
import { call, env, freshIp, newAccount } from "./helpers";

const N = 1200;   // more than the depth limit
const db = env.SEARCH_DB!;
const DAY0 = 20_362;            // 2025-10-01 as a day number
const id = (i: number) => 2 ** 40 + (DAY0 + Math.floor(i / 60)) * 2 ** 24 + 64 * (i + 1);

/** N plain rows: every third a "Data Engineer", the rest nurses; row i newer than row i-1,
 * pay = i*100. Row i is a sponsor row unless i % 4 == 3. */
async function seed(version = "v1") {
  await db.exec("DELETE FROM jobs; DELETE FROM extras; DELETE FROM search_meta; DELETE FROM search_usage;");
  const stmts = [];
  for (let i = 0; i < N; i++) {
    const sid = `t:${String(i).padStart(4, "0")}`;
    const title = i % 3 === 0 ? `Data Engineer ${i}` : `Nurse ${i}`;
    const f = i % 4 === 3 ? 0 : 1;
    stmts.push(db.prepare(
      `INSERT INTO jobs (id, title, company, location, tl, cl, ll, f, vm, pay, ts, x, h, r)
       VALUES (?, ?, 'Acme', 'Austin, TX', NULL, NULL, NULL, ?, 0, ?, NULL, 0, 'h', ?)`)
      .bind(id(i), title, f, i * 100, JSON.stringify([sid])));
  }
  for (let i = 0; i < stmts.length; i += 200) await db.batch(stmts.slice(i, i + 200));
  await setMeta({ index_version: version, ready: "1", generated_at: "g", rows: String(N), sponsor_rows: "900",
                  fields: JSON.stringify(["source_id"]), tags: "[]", visa: "[]" });
}

async function setMeta(m: Record<string, string>) {
  for (const [k, v] of Object.entries(m)) {
    await db.prepare("INSERT INTO search_meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v")
      .bind(k, v).run();
  }
  forgetIndexVersion();
}
const setVersion = (v: string) => setMeta({ index_version: v });

// A fresh network and a fresh account per call, so neither limiter couples tests. The account is
// the dev header (DEV_HEADER_AUTH=1, no database row); "token checks" below uses real tokens.
let userSeq = 0;
const get = (path: string, headers: Record<string, string> = {}, extra: Record<string, any> = {}) =>
  call(path, { headers: { "CF-Connecting-IP": freshIp(), "X-Tailor-User": `acct_s${++userSeq}`, ...headers },
               env: { DEV_HEADER_AUTH: "1", ...extra } });
// A real bearer token, the dev header off (production).
const getAs = (token: string, path: string, headers: Record<string, string> = {}) =>
  call(path, { token, headers: { "CF-Connecting-IP": freshIp(), ...headers }, env: { DEV_HEADER_AUTH: "0" } });

async function walk(path: string, max = 80) {
  const seen: string[] = [];
  let cursor = "";
  let first: any = null, last: any = null, calls = 0;
  for (; calls < max; calls++) {
    const r = await get(`${path}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
    expect(r.status).toBe(200);
    first ??= r.data;
    last = r.data;
    seen.push(...r.data.jobs.map((j: any) => j.source_id));
    if (!r.data.next_cursor) break;
    cursor = r.data.next_cursor;
  }
  return { seen, first, last, calls: calls + 1 };
}

beforeEach(() => seed());

describe("parameters", () => {
  it.each([
    ["limit=0", "limit"], ["limit=51", "limit"], ["limit=abc", "limit"], ["limit=-3", "limit"],
    ["sort=price", "sort"], ["level=senior", "level"], ["days=7.5", "days"], ["days=week", "days"],
    ["pay=lots", "pay"], ["visa=H 1B", "visa"], ["visa=" + Array(11).fill("A").join(","), "visa"],
    ["q=" + "x".repeat(201), "q"], ["loc=" + "x".repeat(201), "loc"], ["q=a%00b", "q"], ["titles=Data", "titles"],
    ["scope=mine", "scope"], ["cursor=nonsense", "cursor"], ["cursor=" + "x".repeat(2049), "cursor"],
  ])("search rejects %s", async (qs, param) => {
    const r = await get(`/v1/jobs/search?${qs}`);
    expect(r.status).toBe(400);
    expect(r.data.param).toBe(param);
  });

  it("accepts what apply_facets accepts: padded numbers, Python truthy strings, empty values", async () => {
    for (const qs of ["days=%207%20", "pay=1_000", "sponsored=yes", "sponsored=nope", "q=&loc=&visa=,,",
                      "sort=PAY", "level=Entry", "days=-5", "scope=", "scope=ALL", "scope=sponsors"]) {
      expect((await get(`/v1/jobs/search?${qs}`)).status).toBe(200);
    }
  });

  it("caps limit at 50 for search and 500 for candidates", async () => {
    expect((await get("/v1/jobs/search?limit=50")).data.jobs).toHaveLength(50);
    expect((await get("/v1/jobs/search?limit=51")).status).toBe(400);
    expect((await get("/v1/jobs/candidates?limit=500")).data.jobs).toHaveLength(500);
    expect((await get("/v1/jobs/candidates?limit=501")).status).toBe(400);
    expect((await get("/v1/jobs/candidates")).data.jobs).toHaveLength(500);   // its default
    expect((await get("/v1/jobs/search")).data.jobs).toHaveLength(30);
  });

  it("scope=sponsors keeps the sponsor board only", async () => {
    const all = await walk("/v1/jobs/search?limit=50&q=nurse%201");
    const sp = await walk("/v1/jobs/search?limit=50&q=nurse%201&scope=sponsors");
    expect(sp.seen.length).toBeGreaterThan(0);
    expect(sp.seen.length).toBeLessThan(all.seen.length);
    for (const s of sp.seen) expect(Number(s.slice(2)) % 4).not.toBe(3);
  });

  it("candidates takes titles=A|B, matched as whole words in the title", async () => {
    const r = await get("/v1/jobs/candidates?titles=data%20engineer|nurse%2010&limit=500");
    expect(r.status).toBe(200);
    const titles = r.data.jobs.map((j: any) => j.title);
    expect(titles.some((t: string) => t.startsWith("Data Engineer"))).toBe(true);
    expect(titles.filter((t: string) => t.startsWith("Nurse"))).toEqual(["Nurse 10"]);   // not "Nurse 100"
  });

  it("answers 503 until the first full load is in, and without a search database", async () => {
    await setMeta({ ready: "0" });
    expect((await get("/v1/jobs/search")).data.reason).toBe("no_index");
    expect((await get("/v1/jobs/search", {}, { SEARCH_DB: undefined })).status).toBe(503);
  });

  it("only GET", async () => {
    expect((await call("/v1/jobs/search", { method: "POST", body: {} })).status).toBe(405);
  });
});

describe("FTS escaping: user text is plain terms", () => {
  it("turns the text into quoted trigrams", () => {
    expect(ftsAll("ab")).toBeNull();
    expect(ftsAll('a"bc')).toBe('"a""b" AND """bc"');
    expect(ftsAll("eng*")).toBe('"eng" AND "ng*"');
  });

  it.each([
    '"', '""', '"engineer', 'engineer"', "NEAR(data engineer)", "data NEAR/2 engineer", "eng*", "*",
    "title:nurse", "{title}: nurse", "data AND engineer", "data OR nurse", "NOT nurse", "-nurse",
    "(data", "data)", "^data", "a'b", "'; DROP TABLE jobs; --", "\\", "%", "_", "[a]", "?",
  ])("q=%s runs and matches only literal substrings", async (q) => {
    const r = await get(`/v1/jobs/search?${new URLSearchParams({ q, limit: "50" })}`);
    expect(r.status).toBe(200);
    const lq = q.trim().toLowerCase();
    for (const j of r.data.jobs) {
      expect(j.title.toLowerCase().includes(lq) || j.company.toLowerCase().includes(lq)).toBe(true);
    }
    expect((await db.prepare("SELECT count(*) AS n FROM jobs").first())?.n).toBe(N);
  });

  it("a phrase operator is matched literally, not obeyed", async () => {
    expect((await get("/v1/jobs/search?q=data%20OR%20nurse")).data.count).toBe(0);
  });
});

describe("cursors", () => {
  it("walks every page in order without gaps or repeats", async () => {
    const { seen } = await walk("/v1/jobs/search?q=engineer&sort=pay&limit=50");
    expect(seen).toHaveLength(400);
    expect(new Set(seen).size).toBe(400);
    expect(seen[0]).toBe("t:1197");            // highest pay first
  });

  it("round-trips its own fields", async () => {
    const body = { v: "v1", f: "abc", n: 30, c: 812, e: null, x: 0 as const, p: "P" as const, s: 0, b: 5, k: 7 };
    expect(await decodeCursor(env, await encodeCursor(env, body))).toEqual(body);
  });

  it("answers 409 for a cursor from an older index", async () => {
    const first = await get("/v1/jobs/search?limit=10");
    await setVersion("v2");
    const r = await get(`/v1/jobs/search?limit=10&cursor=${encodeURIComponent(first.data.next_cursor)}`);
    expect(r.status).toBe(409);
    expect(r.data.reason).toBe("stale_cursor");
    expect(r.data.index_version).toBe("v2");
  });

  it("rejects a tampered cursor and a cursor reused with other facets", async () => {
    const first = await get("/v1/jobs/search?q=nurse&limit=10");
    const [p, m] = first.data.next_cursor.split(".");
    const forged = JSON.parse(atob(p.replace(/-/g, "+").replace(/_/g, "/")));
    forged.n = 0;
    const fp = btoa(JSON.stringify(forged)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    expect((await get(`/v1/jobs/search?q=nurse&cursor=${fp}.${m}`)).status).toBe(400);
    expect((await get(`/v1/jobs/search?q=data&cursor=${first.data.next_cursor}`)).status).toBe(400);
  });

  it("is signed with SEARCH_CURSOR_SECRET when set", async () => {
    const body = { v: "v1", f: "abc", n: 0, c: 1, e: null, x: 0 as const, p: "W" as const, s: 0, b: 1, k: 0 };
    const c = await encodeCursor({ ...env, SEARCH_CURSOR_SECRET: "one" }, body);
    await expect(decodeCursor({ ...env, SEARCH_CURSOR_SECRET: "two" }, c)).rejects.toThrow("cursor");
  });
});

describe("depth, count and the bounded scan", () => {
  it("stops at 1,000 results", async () => {
    const { seen, calls } = await walk("/v1/jobs/search?limit=50");
    expect(seen).toHaveLength(1000);
    expect(calls).toBe(20);
  });

  it("the count is exact when the scan reached the end, an estimate otherwise", async () => {
    let want = 0;
    for (let i = 0; i < N; i += 3) if (`data engineer ${i}`.includes("engineer 3")) want++;
    expect(want).toBeGreaterThan(30);
    // The whole answer fits the page: exact.
    expect((await get("/v1/jobs/search?q=engineer%203&limit=50")).data)
      .toMatchObject({ count: want, count_capped: false, count_estimate: null });
    // It does not: no counting past the page, the keyword index's own count is the estimate.
    const short = (await get("/v1/jobs/search?q=engineer%203&limit=30")).data;
    expect(short).toMatchObject({ count: 31, count_capped: true });
    expect(short.count_estimate).toBeGreaterThanOrEqual(want);
    const broad = (await get("/v1/jobs/search?limit=10")).data;
    expect(broad.count_capped).toBe(true);
    expect(broad.count_estimate).toBe(N);            // every row matches: the whole list
    const most = (await get("/v1/jobs/search?limit=10&scope=sponsors")).data;
    expect(most.count_estimate).toBe(900);           // search_meta sponsor_rows
  });

  it("an empty answer that needs the whole table is found by cursor, a bounded scan a request", async () => {
    const { seen, last, calls } = await walk("/v1/jobs/search?limit=10&level=entry");
    expect(seen).toHaveLength(0);
    expect(last).toMatchObject({ count: 0, count_capped: false, next_cursor: null });
    expect(calls).toBeGreaterThanOrEqual(1);
  });

  it("candidates stop at the same depth", async () => {
    const a = await get("/v1/jobs/candidates?limit=500");
    const b = await get(`/v1/jobs/candidates?limit=500&cursor=${encodeURIComponent(a.data.next_cursor)}`);
    expect(b.data.jobs).toHaveLength(500);
    expect(b.data.next_cursor).toBeNull();
  });
});

describe("the daily budget (free plan)", () => {
  it("answers 503 daily_budget once today's rows read reach the limit; 0 is no limit", async () => {
    const day = new Date().toISOString().slice(0, 10);
    await db.prepare("INSERT INTO search_usage (day, rows_read, rows_written, requests) VALUES (?, 1000, 0, 0)")
      .bind(day).run();
    forgetIndexVersion();
    const r = await get("/v1/jobs/search?limit=5&q=x1", {}, { SEARCH_DAILY_ROWS_READ: "1000" });
    expect(r.status).toBe(503);
    expect(r.data.reason).toBe("daily_budget");
    expect((await get("/v1/jobs/search?limit=5&q=x2", {}, { SEARCH_DAILY_ROWS_READ: "0" })).status).toBe(200);
    expect((await get("/v1/jobs/search?limit=5&q=x3", {}, { SEARCH_DAILY_ROWS_READ: "5000" })).status).toBe(200);
  });

  it("writes this isolate's reads to search_usage in batches", async () => {
    // Flushed every 500 requests (or 25,000 rows read): an edge-cache hit still counts a request.
    for (let i = 0; i < 501; i++) await get("/v1/jobs/search?limit=1&q=nurse");
    const row = await db.prepare("SELECT rows_read, requests FROM search_usage").first<any>();
    expect(row?.rows_read).toBeGreaterThan(0);
    expect(row?.requests).toBeGreaterThan(0);
  });
});

describe("token checks (SponsorJobs installs only)", () => {
  it("401 no_account without a token, with a made-up one, a revoked one or a suspended account", async () => {
    for (const path of ["/v1/jobs/search?limit=1", "/v1/jobs/candidates?limit=1"]) {
      const none = await call(path, { headers: { "CF-Connecting-IP": freshIp() }, env: { DEV_HEADER_AUTH: "0" } });
      expect(none.status).toBe(401);
      expect(none.data.reason).toBe("no_account");
      expect((await getAs("not-a-real-token-" + crypto.randomUUID(), path)).status).toBe(401);
    }
    const revoked = await newAccount();
    await call("/account/revoke", { method: "POST", token: revoked.token });
    expect((await getAs(revoked.token, "/v1/jobs/search?limit=1")).data.reason).toBe("no_account");
    const suspended = await newAccount();
    await env.DB.prepare("UPDATE accounts SET suspended_at = 1 WHERE account_id = ?").bind(suspended.id).run();
    expect((await getAs(suspended.token, "/v1/jobs/search?limit=1")).status).toBe(401);
  });

  it("the X-Tailor-User header is no account in production", async () => {
    const r = await call("/v1/jobs/search?limit=1",
                         { headers: { "CF-Connecting-IP": freshIp(), "X-Tailor-User": "acct_x" }, env: { DEV_HEADER_AUTH: "0" } });
    expect(r.status).toBe(401);
  });

  it("a valid token searches; the answer is private to the app, and the edge cache serves every account", async () => {
    const a = await newAccount(), b = await newAccount();
    const ra = await getAs(a.token, "/v1/jobs/search?limit=3&q=nurse");
    expect(ra.status).toBe(200);
    expect(ra.data.jobs).toHaveLength(3);
    const rb = await getAs(b.token, "/v1/jobs/search?limit=3&q=nurse");
    expect(rb.data).toEqual(ra.data);
    const raw = await (await import("../src/index")).default.fetch(
      new Request("https://broker.test/v1/jobs/search?limit=3&q=nurse",
                  { headers: { Authorization: `Bearer ${a.token}`, "CF-Connecting-IP": freshIp() } }),
      { ...env, DEV_HEADER_AUTH: "0" } as any);
    expect(raw.headers.get("cache-control")).toMatch(/^private/);
  });

  it("checks the token before anything else: no index, a bad cursor or a 503 are not shown to strangers", async () => {
    const r = await call("/v1/jobs/search?cursor=nonsense",
                         { headers: { "CF-Connecting-IP": freshIp() }, env: { DEV_HEADER_AUTH: "0", SEARCH_DB: undefined } });
    expect(r.status).toBe(401);
  });

  it("a revoked token stops within the account cache's minute", async () => {
    const a = await newAccount();
    expect((await getAs(a.token, "/v1/jobs/search?limit=1")).status).toBe(200);
    await call("/account/revoke", { method: "POST", token: a.token });
    forgetAccounts();                                // as when the cached minute has passed
    expect((await getAs(a.token, "/v1/jobs/search?limit=1")).status).toBe(401);
  });
});

describe("rate limits", () => {
  it("60 a minute per account, keyed on the account, not the token", async () => {
    const a = await newAccount();
    let last = 0;
    for (let i = 0; i < 61; i++) last = (await getAs(a.token, "/v1/jobs/search?limit=1")).status;
    expect(last).toBe(429);
    // A second token for the SAME account shares its limit; another account does not.
    const { issueToken } = await import("../src/accounts");
    expect((await getAs(await issueToken(env.DB, a.id), "/v1/jobs/search?limit=1")).status).toBe(429);
    expect((await getAs((await newAccount()).token, "/v1/jobs/search?limit=1")).status).toBe(200);
  });

  it("300 a minute per network, counted before the token is looked up", async () => {
    const ip = freshIp();
    let last = 0;
    for (let i = 0; i < 301; i++) {
      last = (await call("/v1/jobs/search?limit=1",
                         { headers: { "CF-Connecting-IP": ip, "X-Tailor-User": `acct_n${i}` },
                           env: { DEV_HEADER_AUTH: "1" } })).status;
    }
    expect(last).toBe(429);
    // Over the network's limit, even a request with no token is turned away without a lookup.
    expect((await call("/v1/jobs/search?limit=1", { headers: { "CF-Connecting-IP": ip } })).status).toBe(429);
  });
});
