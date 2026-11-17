// Developer-only: plain-English UI tests (Midscene.js) against the RUNNING SponsorJobs dev
// window over CDP. Nothing here ships in the app, and nothing here launches a browser.
import 'dotenv/config'; // model keys from tests/human/.env (git-ignored)
import { defineConfig } from '@playwright/test';

// Cheaper, faster model calls unless you opt in to longer reasoning.
process.env.MIDSCENE_MODEL_REASONING_ENABLED ??= 'false';

export default defineConfig({
  testDir: './specs',
  // One window, one test at a time, in order. No retries: a retry is another round of paid calls.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 120_000,
  reporter: [['list'], ['@midscene/web/playwright-reporter', { type: 'merged' }]],
  use: {
    // No trace/video/screenshot: those need a context Playwright owns, and this window is not ours.
    trace: 'off',
    video: 'off',
    screenshot: 'off',
    actionTimeout: 15_000,
  },
});
