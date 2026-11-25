// Tests run inside the real Workers runtime (workerd via Miniflare) against a local D1, with the
// same migrations the deploy applies. No Cloudflare account and no network are involved.
import { fileURLToPath } from "node:url";
import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

export default defineConfig(async () => {
  const migrations = await readD1Migrations(fileURLToPath(new URL("./migrations", import.meta.url)));
  const searchMigrations = await readD1Migrations(fileURLToPath(new URL("./migrations-search", import.meta.url)));
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: "./wrangler.jsonc" },
        miniflare: {
          // A dummy key so the Worker believes a model is configured; every call to Anthropic
          // is stubbed in the tests, so this value never leaves the test runtime.
          bindings: { TEST_MIGRATIONS: migrations, TEST_SEARCH_MIGRATIONS: searchMigrations, ANTHROPIC_API_KEY: "sk-test-not-real" },
          // The search database and its rate limits are not in wrangler.jsonc until go-live
          // (docs/server-search-plan.md); the tests bind their own.
          d1Databases: { SEARCH_DB: "sponsorjobs-search-test" },
          ratelimits: {
            SEARCH_TOKEN_LIMITER: { namespace_id: "7101", simple: { limit: 60, period: 60 } },
            SEARCH_IP_LIMITER: { namespace_id: "7102", simple: { limit: 300, period: 60 } },
          },
        },
      }),
    ],
    test: { setupFiles: ["./test/apply-migrations.ts"] },
  };
});
