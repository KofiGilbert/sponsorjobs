// Parity with the app's own filters: the fixture (scripts/gen_search_parity.py) holds synthetic
// feed rows, the SQL the loader writes for them, and the IDs sourcing/filters.apply_facets (and the
// picker's title focus) answer for ~70 facet mixes, in order. The Worker must answer the same IDs
// in the same order, walking every page through its cursors, with the same clock.
import { beforeAll, describe, expect, it } from "vitest";
import { forgetIndexVersion, handleSearch, histograms, parseParams, searchPage, type IndexInfo } from "../src/search";
import fixture from "./fixtures/search-parity.json";
import { env } from "./helpers";

// The rate limiters are real in the test runtime; the parity walk makes far more than 300 calls.
// No daily budget either: the walk reads the small table many times over. Search needs an account
// (src/installauth.ts): the dev header stands in for one here; search.test.ts checks real tokens.
const free = { ...env, SEARCH_IP_LIMITER: undefined, SEARCH_ACCOUNT_LIMITER: undefined,
               SEARCH_DAILY_ROWS_READ: "0", SEARCH_DAILY_REQUESTS: "0", DEV_HEADER_AUTH: "1" };
const asAccount = { headers: { "X-Tailor-User": "acct_parity" } };
const db = env.SEARCH_DB!;

interface Case { mode: "search" | "candidates"; params: Record<string, string>; ids: string[] }

export async function loadFixture() {
  await db.exec("DELETE FROM jobs; DELETE FROM extras; DELETE FROM search_meta; DELETE FROM search_usage;");
  for (const sql of fixture.statements) await db.prepare(sql).run();
  forgetIndexVersion();
}

async function walk(c: Case, limit: number) {
  const ids: string[] = [];
  let cursor = "";
  let last: any = null;
  for (let page = 0; page < 200; page++) {
    const qs = new URLSearchParams({ ...c.params, limit: String(limit), ...(cursor ? { cursor } : {}) });
    const res = await handleSearch(new Request(`https://api.test/v1/jobs/${c.mode}?${qs}`, asAccount), free,
                                   c.mode, fixture.now_ms);
    expect(res.status).toBe(200);
    last = await res.json();
    expect(last.count).toBeGreaterThanOrEqual(Math.min(ids.length + last.jobs.length, 1));
    ids.push(...last.jobs.map((j: any) => j.source_id));
    if (!last.next_cursor) break;
    cursor = last.next_cursor;
  }
  return { ids, last };
}

async function info(): Promise<IndexInfo> {
  const m = Object.fromEntries((await db.prepare("SELECT k, v FROM search_meta").all<{ k: string; v: string }>())
    .results.map((r) => [r.k, r.v]));
  return { v: m.index_version!, g: m.generated_at!, rows: Number(m.rows), sponsorRows: Number(m.sponsor_rows),
           fields: JSON.parse(m.fields!), tags: JSON.parse(m.tags!),
           visaBits: new Map((JSON.parse(m.visa!) as string[]).map((c, i) => [c, i])), ...histograms(m) };
}

/** The same walk straight through searchPage with a plan forced or a tiny scan cap, so the
 * walks a big index takes (no keyword index, no whole-result sort, pages cut short by the cap)
 * answer the same rows as the small fixture's default route. */
async function walkWith(c: Case, opts: { cap?: number; noFts?: boolean; noSort?: boolean }) {
  const { facets } = parseParams(new URLSearchParams(c.params), c.mode);
  const ix = await info();
  const ids: string[] = [];
  let after = null;
  for (let page = 0; page < 2000; page++) {
    const r = await searchPage(free, facets, 11, after, ix, fixture.now_ms, opts);
    ids.push(...r.rows.map((row) => row.source_id as string));
    if (!r.next) break;
    after = r.next;
  }
  return ids;
}

beforeAll(loadFixture);

describe("parity with sourcing/filters.apply_facets", () => {
  it("covers about seventy facet mixes", () => {
    expect(fixture.cases.length).toBeGreaterThanOrEqual(60);
  });

  for (const c of fixture.cases as Case[]) {
    it(`${c.mode} ${JSON.stringify(c.params)}`, async () => {
      const { ids, last } = await walk(c, c.mode === "search" ? 13 : 37);
      expect(ids).toEqual(c.ids);
      // Once the walk has reached the end, the count is exact.
      expect(last.count).toBe(c.ids.length);
      expect(last.count_capped).toBe(false);
    });
  }

  for (const c of fixture.cases as Case[]) {
    it(`every plan: ${c.mode} ${JSON.stringify(c.params)}`, async () => {
      expect(await walkWith(c, { noFts: true })).toEqual(c.ids);
      expect(await walkWith(c, { noSort: true })).toEqual(c.ids);
      // A tiny scan cap cuts most pages short: the cursor must carry on with no gap or repeat.
      expect(await walkWith(c, { cap: 7 })).toEqual(c.ids);
      expect(await walkWith(c, { cap: 7, noFts: true, noSort: true })).toEqual(c.ids);
      expect(await walkWith(c, { cap: 1 })).toEqual(c.ids);
    });
  }

  it("returns the slim row the feed carries, employer fields and tags joined back", async () => {
    const sample: any = fixture.sample;
    const qs = new URLSearchParams({ q: sample.title, limit: "50" });
    const res = await handleSearch(new Request(`https://api.test/v1/jobs/search?${qs}`, asAccount), free,
                                   "search", fixture.now_ms);
    const data: any = await res.json();
    expect(data.jobs.find((j: any) => j.source_id === sample.source_id)).toEqual(sample);
    expect(data.index_version).toBe(fixture.index_version);
    expect(data.generated_at).toBe("2026-10-20T12:00:00+00:00");
  });
});
