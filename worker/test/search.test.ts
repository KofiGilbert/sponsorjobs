// /v1/jobs/search and /v1/jobs/candidates: parameters, FTS escaping, cursors (round trip, 409 on
// a new index, tampering), the 1,000-result depth limit, the capped count, the page limits and
// the rate limits. Facet semantics are checked against Python in search-parity.test.ts.
import { beforeEach, describe, expect, it } from "vitest";
import { decodeCursor, encodeCursor, forgetIndexVersion, ftsPhrase } from "../src/search";
import { call, env, freshIp } from "./helpers";

const N = 1200;   // more than the depth limit and the count cap

/** N plain rows: "Engineer 0000".."Engineer 1199", newest first by id, pay = id * 100. */
async function seed(version = "v1") {
  await env.DB.exec("DELETE FROM jobs; DELETE FROM job_extras; DELETE FROM search_meta;");
  const stmts = [];
  for (let i = 0; i < N; i++) {
    const id = `t:${String(i).padStart(4, "0")}`;
    const day = `2026-10-${String(1 + (i % 20)).padStart(2, "0")}`;
    const title = i % 3 === 0 ? `Data Engineer ${i}` : `Nurse ${i}`;
    const row = JSON.stringify({ source_id: id, title, company: "Acme", location: "Austin, TX", posted_at: day });
    stmts.push(env.DB.prepare(
      `INSERT INTO jobs (source_id, h, rkey, pay, posted_ts, remote_l, entry, sponsored, visa_any, visa_codes,
                         title_l, company_l, location_l, xh, row)
       VALUES (?, 'h', ?, ?, NULL, '', 0, 0, 0, '', ?, 'acme', 'austin, tx', '', ?)`)
      .bind(id, `1${day}\x01\x01${id}`, i * 100, title.toLowerCase(), row));
  }
  for (let i = 0; i < stmts.length; i += 200) await env.DB.batch(stmts.slice(i, i + 200));
  await setVersion(version);
}

async function setVersion(v: string) {
  await env.DB.prepare("INSERT INTO search_meta (k, v) VALUES ('index_version', ?), ('generated_at', 'g') "
                       + "ON CONFLICT(k) DO UPDATE SET v = excluded.v").bind(v).run();
  forgetIndexVersion();
}

// A fresh network per call, so the per-IP limiter never couples tests; no token unless asked.
const get = (path: string, headers: Record<string, string> = {}) =>
  call(path, { headers: { "CF-Connecting-IP": freshIp(), ...headers } });

beforeEach(() => seed());

describe("parameters", () => {
  it.each([
    ["limit=0", "limit"], ["limit=51", "limit"], ["limit=abc", "limit"], ["limit=-3", "limit"],
    ["sort=price", "sort"], ["level=senior", "level"], ["days=7.5", "days"], ["days=week", "days"],
    ["pay=lots", "pay"], ["visa=H 1B", "visa"], ["visa=" + Array(11).fill("A").join(","), "visa"],
    ["q=" + "x".repeat(201), "q"], ["q=a%00b", "q"], ["titles=Data", "titles"],
    ["cursor=nonsense", "cursor"], ["cursor=" + "x".repeat(2049), "cursor"],
  ])("search rejects %s", async (qs, param) => {
    const r = await get(`/v1/jobs/search?${qs}`);
    expect(r.status).toBe(400);
    expect(r.data.param).toBe(param);
  });

  it("accepts what apply_facets accepts: padded numbers, Python truthy strings, empty values", async () => {
    for (const qs of ["days=%207%20", "pay=1_000", "sponsored=yes", "sponsored=nope", "q=&loc=&visa=,,",
                      "sort=PAY", "level=Entry", "days=-5"]) {
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

  it("candidates takes titles=A|B, matched as whole words in the title", async () => {
    const r = await get("/v1/jobs/candidates?titles=data%20engineer|nurse%2010&limit=500");
    expect(r.status).toBe(200);
    const titles = r.data.jobs.map((j: any) => j.title);
    expect(titles.some((t: string) => t.startsWith("Data Engineer"))).toBe(true);
    expect(titles.filter((t: string) => t.startsWith("Nurse"))).toEqual(["Nurse 10"]);   // not "Nurse 100"
  });

  it("answers 503 until an index is loaded", async () => {
    await env.DB.exec("DELETE FROM search_meta;");
    forgetIndexVersion();
    expect((await get("/v1/jobs/search")).status).toBe(503);
  });

  it("only GET", async () => {
    expect((await call("/v1/jobs/search", { method: "POST", body: {} })).status).toBe(405);
  });
});

describe("FTS escaping: user text is plain terms", () => {
  it("quotes the whole string as one phrase", () => {
    expect(ftsPhrase('say "hi"')).toBe('"say ""hi"""');
    expect(ftsPhrase("a NEAR b")).toBe('"a NEAR b"');
    expect(ftsPhrase("eng*")).toBe('"eng*"');
  });

  it.each([
    '"', '""', '"engineer', 'engineer"', "NEAR(data engineer)", "data NEAR/2 engineer", "eng*", "*",
    "title_l:nurse", "{title_l}: nurse", "data AND engineer", "data OR nurse", "NOT nurse", "-nurse",
    "(data", "data)", "^data", "a'b", "'; DROP TABLE jobs; --", "\\", "%", "_", "[a]", "?",
  ])("q=%s runs and matches only literal substrings", async (q) => {
    const r = await get(`/v1/jobs/search?${new URLSearchParams({ q, limit: "50" })}`);
    expect(r.status).toBe(200);
    const lq = q.trim().toLowerCase();
    for (const j of r.data.jobs) {
      expect(j.title.toLowerCase().includes(lq) || j.company.toLowerCase().includes(lq)).toBe(true);
    }
    expect((await env.DB.prepare("SELECT count(*) AS n FROM jobs").first())?.n).toBe(N);
  });

  it("a phrase operator is matched literally, not obeyed", async () => {
    expect((await get("/v1/jobs/search?q=data%20OR%20nurse")).data.count).toBe(0);
    expect((await get("/v1/jobs/search?q=engineer")).data.count).toBe(400);
  });
});

describe("cursors", () => {
  it("walks every page in order without gaps or repeats", async () => {
    const seen: string[] = [];
    let cursor = "";
    for (let i = 0; i < 30; i++) {
      const r = await get(`/v1/jobs/search?q=engineer&sort=pay&limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
      expect(r.status).toBe(200);
      seen.push(...r.data.jobs.map((j: any) => j.source_id));
      if (!r.data.next_cursor) break;
      cursor = r.data.next_cursor;
    }
    expect(seen).toHaveLength(400);
    expect(new Set(seen).size).toBe(400);
    expect(seen[0]).toBe("t:1197");            // highest pay first
  });

  it("round-trips its own fields", async () => {
    const body = { v: "v1", f: "abc", n: 30, c: 812, k: [5, "1x\u0001y"] as [number, string] };
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
    const body = { v: "v1", f: "abc", n: 0, c: 1, k: [0, "x"] as [number, string] };
    const c = await encodeCursor({ ...env, SEARCH_CURSOR_SECRET: "one" }, body);
    await expect(decodeCursor({ ...env, SEARCH_CURSOR_SECRET: "two" }, c)).rejects.toThrow("cursor");
  });
});

describe("depth and count", () => {
  it("stops at 1,000 results and caps the count", async () => {
    let cursor = "";
    let total = 0;
    let pages = 0;
    let first: any = null;
    for (; pages < 40; pages++) {
      const r = await get(`/v1/jobs/search?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
      expect(r.status).toBe(200);
      first ??= r.data;
      total += r.data.jobs.length;
      if (!r.data.next_cursor) break;
      cursor = r.data.next_cursor;
    }
    expect(total).toBe(1000);
    expect(pages + 1).toBe(20);
    expect(first.count).toBe(1000);
    expect(first.count_capped).toBe(true);
  });

  it("an exact count below the cap", async () => {
    const r = await get("/v1/jobs/search?q=engineer");
    expect(r.data).toMatchObject({ count: 400, count_capped: false });
  });

  it("candidates stop at the same depth", async () => {
    const a = await get("/v1/jobs/candidates?limit=500");
    const b = await get(`/v1/jobs/candidates?limit=500&cursor=${encodeURIComponent(a.data.next_cursor)}`);
    expect(b.data.jobs).toHaveLength(500);
    expect(b.data.next_cursor).toBeNull();
  });
});

describe("rate limits", () => {
  it("60 a minute per install token", async () => {
    const token = "install-" + crypto.randomUUID();
    let last = 0;
    for (let i = 0; i < 61; i++) last = (await get("/v1/jobs/search?limit=1", { "X-Install": token })).status;
    expect(last).toBe(429);
    expect((await get("/v1/jobs/search?limit=1", { "X-Install": "install-" + crypto.randomUUID() })).status).toBe(200);
  });

  it("300 a minute per network", async () => {
    const ip = freshIp();
    let last = 0;
    for (let i = 0; i < 301; i++) {
      last = (await call("/v1/jobs/search?limit=1", { headers: { "CF-Connecting-IP": ip } })).status;
    }
    expect(last).toBe(429);
  });
});
