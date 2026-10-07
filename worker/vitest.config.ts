// Tests run inside the real Workers runtime (workerd via Miniflare) against a local D1, with the
// same migrations the deploy applies. No Cloudflare account and no network are involved.
import { fileURLToPath } from "node:url";
import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

export default defineConfig(async () => {
  const migrations = await readD1Migrations(fileURLToPath(new URL("./migrations", import.meta.url)));
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: "./wrangler.jsonc" },
        miniflare: {
          // A dummy key so the Worker believes a model is configured; every call to Anthropic
          // is stubbed in the tests, so this value never leaves the test runtime.
          bindings: { TEST_MIGRATIONS: migrations, ANTHROPIC_API_KEY: "sk-test-not-real" },
        },
      }),
    ],
    test: { setupFiles: ["./test/apply-migrations.ts"] },
  };
});
