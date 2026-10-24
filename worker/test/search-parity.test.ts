// Parity with the app's own filters: the fixture (scripts/gen_search_parity.py) holds synthetic
// feed rows, the SQL the loader writes for them, and the IDs sourcing/filters.apply_facets (and the
// picker's title focus) answer for ~50 facet mixes, in order. The Worker must answer the same IDs
// in the same order, walking every page through its cursors, with the same clock.
import { beforeAll, describe, expect, it } from "vitest";
import { forgetIndexVersion, handleSearch, parseParams, searchPage } from "../src/search";
import fixture from "./fixtures/search-parity.json";
import { env } from "./helpers";

// The rate limiters are real in the test runtime; the parity walk makes far more than 300 calls.
const free = { ...env, SEARCH_IP_LIMITER: undefined, SEARCH_TOKEN_LIMITER: undefined };

interface Case { mode: "search" | "candidates"; params: Record<string, string>; ids: string[] }

export async function loadFixture() {
  await env.DB.exec("DELETE FROM jobs; DELETE FROM job_extras; DELETE FROM search_meta;");
  for (const sql of fixture.statements) await env.DB.prepare(sql).run();
  forgetIndexVersion();
}

async function walk(c: Case, limit: number) {
  const ids: string[] = [];
  let cursor = "";
  let count = -1;
  for (let page = 0; page < 100; page++) {
    const qs = new URLSearchParams({ ...c.params, limit: String(limit), ...(cursor ? { cursor } : {}) });
    const res = await handleSearch(new Request(`https://api.test/v1/jobs/${c.mode}?${qs}`), free,
                                   c.mode, fixture.now_ms);
    expect(res.status).toBe(200);
    const data: any = await res.json();
    if (count < 0) count = data.count;
    expect(data.count).toBe(count);                 // page 1's count rides along in the cursor
    ids.push(...data.jobs.map((j: any) => j.source_id));
    if (!data.next_cursor) break;
    cursor = data.next_cursor;
  }
  return { ids, count };
}

/** The same walk straight through searchPage with one plan forced: the small fixture always takes
 * the FTS plan through the route, and the ordered walk (what a broad term takes at 184k rows)
 * must answer the same rows. */
async function walkPlan(c: Case, plan: "fts" | "scan") {
  const { facets } = parseParams(new URLSearchParams(c.params), c.mode);
  const ids: string[] = [];
  let after = null;
  for (let page = 0; page < 100; page++) {
    const r = await searchPage(free, facets, 11, after, fixture.index_version, fixture.now_ms, { plan });
    ids.push(...r.rows.map((row) => JSON.parse(row).source_id));
    if (!r.next) break;
    after = r.next;
  }
  return ids;
}

beforeAll(loadFixture);

describe("parity with sourcing/filters.apply_facets", () => {
  it("covers about fifty facet mixes", () => {
    expect(fixture.cases.length).toBeGreaterThanOrEqual(50);
  });

  for (const c of fixture.cases as Case[]) {
    it(`${c.mode} ${JSON.stringify(c.params)}`, async () => {
      const { ids, count } = await walk(c, c.mode === "search" ? 13 : 37);
      expect(ids).toEqual(c.ids);
      expect(count).toBe(c.ids.length);
    });
  }

  for (const c of (fixture.cases as Case[]).filter((x) => x.params.q || x.params.loc || x.params.titles)) {
    it(`both plans: ${c.mode} ${JSON.stringify(c.params)}`, async () => {
      expect(await walkPlan(c, "scan")).toEqual(c.ids);
      expect(await walkPlan(c, "fts")).toEqual(c.ids);
    });
  }

  it("returns the slim row the feed carries, employer fields joined back", async () => {
    const sample: any = fixture.sample;
    const qs = new URLSearchParams({ q: sample.title, limit: "50" });
    const res = await handleSearch(new Request(`https://api.test/v1/jobs/search?${qs}`), free,
                                   "search", fixture.now_ms);
    const data: any = await res.json();
    expect(data.jobs.find((j: any) => j.source_id === sample.source_id)).toEqual(sample);
    expect(data.index_version).toBe(fixture.index_version);
    expect(data.generated_at).toBe("2026-10-20T12:00:00+00:00");
  });
});
