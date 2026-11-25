#!/usr/bin/env node
// A local stand-in for the Cloudflare D1 HTTP API, for trying the search loader and the search
// queries with no Cloudflare account (docs/server-search-plan.md, "Try it locally").
//
//   node worker/scripts/local-d1.mjs [--dir .search-d1] [--port 8791]
//
// It runs the real D1 engine (workerd, through wrangler's getPlatformProxy) on a database kept in --dir, applies
// worker/migrations-search/, and answers the one call the loader makes:
//
//   POST /query  {"sql": "...", "params": [...]}
//     -> {"success": true, "result": [{"results": [...], "meta": {rows_read, rows_written, ...}}]}
//
// so `SEARCH_D1_API_URL=http://127.0.0.1:8791/query python scripts/load_search_index.py ...`
// loads a feed into it and reports the rows D1 itself counts as read and written (the numbers
// the free plan's limits are measured in). GET /size answers the database's size in bytes.
// Nothing here is deployed or imported by the Worker.
import { readdirSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { getPlatformProxy } from "wrangler";

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const here = dirname(fileURLToPath(import.meta.url));
const dir = resolve(opt("dir", ".search-d1"));
const port = Number(opt("port", "8791"));
const migrations = resolve(here, "..", "migrations-search");

const proxy = await getPlatformProxy({
  configPath: join(here, "local-d1.wrangler.jsonc"),
  persist: { path: dir },
});
const db = proxy.env.SEARCH_DB;
for (const name of readdirSync(migrations).filter((f) => f.endsWith(".sql")).sort()) {
  await db.exec(readFileSync(join(migrations, name), "utf8")
    .split("\n").map((l) => l.replace(/--.*$/, "")).join(" ")
    .replace(/\s+/g, " "));
}

const server = createServer(async (req, res) => {
  const send = (status, body) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(body));
  };
  try {
    if (req.method === "GET" && req.url === "/size") {
      const r = await db.prepare("SELECT page_count * page_size AS bytes FROM pragma_page_count(), pragma_page_size()").first();
      return send(200, r);
    }
    if (req.method !== "POST") return send(405, { success: false, errors: [{ message: "POST /query" }] });
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const { sql, params } = JSON.parse(raw);
    const r = await db.prepare(sql).bind(...(params ?? [])).all();
    send(200, { success: true, result: [{ results: r.results, meta: r.meta, success: true }] });
  } catch (err) {
    send(200, { success: false, errors: [{ message: String(err?.message ?? err) }] });
  }
});
server.listen(port, "127.0.0.1", () => console.log(`local D1 on http://127.0.0.1:${port}/query (data in ${dir})`));
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, async () => { server.close(); await proxy.dispose(); process.exit(0); });
}
