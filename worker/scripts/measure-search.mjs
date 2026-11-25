#!/usr/bin/env node
// Measure what typical searches cost on a loaded local search index: D1's own rows_read per
// request, page 1 and page 2, for a list of real board queries (docs/server-search-plan.md).
//
//   node worker/scripts/measure-search.mjs --dir .search-d1 [--now 2026-11-25T11:15:36Z]
//   node worker/scripts/measure-search.mjs --dir .search-d1 --trace 'q=engineer&level=entry'
//
// --dir is the folder local-d1.mjs keeps its database in, loaded by scripts/load_search_index.py.
// Runs the Worker's own search code (src/search.ts, through Node's TypeScript type stripping)
// against the real D1 engine. Nothing is deployed and nothing leaves the machine.
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { getPlatformProxy } from "wrangler";
import { forgetIndexVersion, histograms, parseParams, searchPage } from "../src/search.ts";

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const here = dirname(fileURLToPath(import.meta.url));
const proxy = await getPlatformProxy({ configPath: join(here, "local-d1.wrangler.jsonc"),
                                       persist: { path: resolve(opt("dir", ".search-d1")) } });
const db = proxy.env.SEARCH_DB;
const env = { SEARCH_DB: db };
const meta = Object.fromEntries((await db.prepare("SELECT k, v FROM search_meta").all()).results.map((r) => [r.k, r.v]));
const nowMs = Date.parse(opt("now", meta.generated_at || new Date().toISOString()));
const info = { v: meta.index_version, g: meta.generated_at, rows: Number(meta.rows), sponsorRows: Number(meta.sponsor_rows),
               fields: JSON.parse(meta.fields), tags: JSON.parse(meta.tags),
               visaBits: new Map(JSON.parse(meta.visa).map((c, i) => [c, i])), ...histograms(meta) };

const QUERIES = [
  "", "scope=sponsors", "sort=pay", "scope=sponsors&sort=pay", "days=7", "scope=sponsors&days=7",
  "sponsored=1", "pay=150000&sort=pay", "level=entry", "scope=sponsors&level=entry", "remote=remote",
  "visa=STEM-OPT", "visa=CAP-EXEMPT", "loc=remote", "loc=new york", "loc=texas",
  "q=engineer", "q=software engineer", "q=nurse", "q=data analyst", "q=data analyst&sort=pay",
  "q=accountant&sort=pay", "q=product manager", "q=stripe", "q=walmart", "q=engineer&scope=sponsors",
  "q=engineer&visa=STEM-OPT", "q=engineer&days=7", "q=nurse&days=30", "q=analyst&loc=new york",
  "q=data scientist&remote=remote", "q=zzqx", "q=engineer&level=entry", "q=engineer&days=7&level=entry",
  "q=product manager&visa=CAP-EXEMPT&days=14", "q=intern&scope=sponsors&level=entry",
  "q=ai&scope=sponsors", "loc=ca&scope=sponsors", "q=machine learning&sort=pay&scope=sponsors",
];

const one = opt("trace", "");
if (one) {
  const { facets } = parseParams(new URLSearchParams(one + "&limit=30"), "search");
  const trace = [];
  const p = await searchPage(env, facets, 30, null, info, nowMs, { trace });
  for (const t of trace) console.log(t.read, t.sql.slice(0, 140), JSON.stringify(t.binds.filter((b) => typeof b === "number")));
  console.log(p.rows.length, p.stats);
  await proxy.dispose();
  process.exit(0);
}
const rows = [];
for (const qs of QUERIES) {
  forgetIndexVersion();
  const { facets } = parseParams(new URLSearchParams(qs + "&limit=30"), "search");
  const t = Date.now();
  const p1 = await searchPage(env, facets, 30, null, info, nowMs);
  const ms = Date.now() - t;
  const p2 = p1.next ? await searchPage(env, facets, 30, p1.next, info, nowMs) : null;
  rows.push({ query: qs || "(default board)", page1: p1.stats.rows_read, page2: p2 ? p2.stats.rows_read : "-",
              got: p1.rows.length, count: p1.count_exact ? p1.count : `~${p1.count_estimate ?? p1.count + "+"}`,
              plan: p1.stats.plan, ms });
}
console.table(rows);
const r1 = rows.map((r) => r.page1);
console.log(`rows: ${info.rows}  page-1 rows read: mean ${Math.round(r1.reduce((a, b) => a + b, 0) / r1.length)}, `
  + `max ${Math.max(...r1)}`);
await proxy.dispose();
