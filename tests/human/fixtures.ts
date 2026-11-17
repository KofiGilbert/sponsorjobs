// Shared fixtures for the plain-English "human" tests.
//
// These tests never launch a browser. They attach over CDP to the SponsorJobs dev window
// that is ALREADY running, pick the SponsorJobs tab (never a job-site tab), and hand that
// page to Midscene so each step can be written in plain English.
//
// Two safety nets run for the whole test, on top of the tests only ever looking and
// navigating:
//   1. Read-only network: every request from the page that is not GET/HEAD/OPTIONS is
//      aborted, so nothing can tailor (paid credit), send, submit, save or delete.
//   2. Click guard: clicks on Tailor & apply, Send, Submit, Edit, Show, Forget, Delete,
//      Change photo, checkboxes and file pickers are swallowed before the app sees them,
//      and all typing into the page is blocked.
// Both are removed when the test ends; if the test process dies, the CDP session closes
// and they disappear with it.
import { test as base, chromium, type Browser, type Page } from '@playwright/test';
import { PlaywrightAiFixture, type PlayWrightAiFixtureType } from '@midscene/web/playwright';

export const CDP_URL = process.env.SJ_CDP_URL || 'http://127.0.0.1:9233';
export const APP_URL_MATCH = process.env.SJ_APP_URL_MATCH || '127.0.0.1:57010';

const READ_ONLY_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

// Runs inside the app page. Kept self-contained (no closures) because Playwright
// serialises it.
function installClickGuard() {
  const w = window as any;
  if (w.__humanTestGuard) return;
  const BLOCK_SELECTOR = [
    '[data-fd-tailor]', // "Tailor & apply" spends a paid tailoring credit
    '#pkgSubmitBtn', '#aaTelegramSend', '#projSubmit', '#signinResend',
    '#menuPhoto', '#acctPhotoFile', '#memEeoShow', '#memEeoDecline',
    'input[type=file]', 'input[type=checkbox]', 'input[type=radio]',
    'button[type=submit]', 'input[type=submit]',
  ].join(',');
  const BLOCK_TEXT = /^\s*(tailor\s*&\s*apply|send|submit|edit|show|forget|delete|remove|change photo|apply for me|sign out|log out)\b/i;
  const blocked: string[] = [];
  const isBlocked = (target: EventTarget | null) => {
    const el = target instanceof Element ? target : null;
    if (!el) return null;
    const hit = el.closest(BLOCK_SELECTOR);
    if (hit) return hit;
    const ctl = el.closest('button, a, [role=button], [role=menuitem]');
    if (ctl && BLOCK_TEXT.test((ctl.textContent || '').trim())) return ctl;
    return null;
  };
  const onPointer = (e: Event) => {
    const hit = isBlocked(e.target);
    if (!hit) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    if (e.type === 'click') {
      const label = ((hit.textContent || '').trim() || hit.id || hit.tagName).slice(0, 60);
      blocked.push(label);
      console.warn(`[human-test guard] blocked a click on "${label}"`);
    }
  };
  const onType = (e: Event) => {
    e.preventDefault();
    e.stopImmediatePropagation();
    console.warn('[human-test guard] blocked typing');
  };
  const pointerEvents = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'dblclick', 'auxclick'];
  pointerEvents.forEach((t) => document.addEventListener(t, onPointer, true));
  document.addEventListener('beforeinput', onType, true);
  document.addEventListener('keypress', onType, true);
  w.__humanTestGuard = {
    blocked,
    remove() {
      pointerEvents.forEach((t) => document.removeEventListener(t, onPointer, true));
      document.removeEventListener('beforeinput', onType, true);
      document.removeEventListener('keypress', onType, true);
      delete w.__humanTestGuard;
    },
  };
}

function removeClickGuard() {
  (window as any).__humanTestGuard?.remove();
}

async function findAppPage(browser: Browser): Promise<Page> {
  const pages = browser.contexts().flatMap((c) => c.pages());
  const app = pages.filter((p) => p.url().includes(APP_URL_MATCH) && !p.url().startsWith('devtools://'));
  if (app.length === 0) {
    const seen = pages.map((p) => `  - ${p.url()}`).join('\n') || '  (no pages)';
    throw new Error(
      `No SponsorJobs tab found at ${CDP_URL} (looking for a URL containing "${APP_URL_MATCH}").\n` +
        `Is the dev window running with remote debugging on? Tabs seen:\n${seen}`,
    );
  }
  return app[0];
}

type WorkerFixtures = { cdpBrowser: Browser };

const midscene = PlaywrightAiFixture({
  // Never let Midscene close popups or re-route new windows: the app's job-site pane
  // must be left alone.
  forceSameTabNavigation: false,
  autoFollowNewPage: false,
  // Fresh model calls every run (cached element positions can go stale on a live job list).
  cache: false,
});

export const test = base
  .extend<{}, WorkerFixtures>({
    // One CDP connection per worker. close() on a CDP-attached browser only disconnects;
    // it never closes the person's window.
    cdpBrowser: [
      async ({}, use) => {
        const browser = await chromium.connectOverCDP(CDP_URL);
        await use(browser);
        await browser.close();
      },
      { scope: 'worker' },
    ],
  })
  .extend<{ page: Page }>({
    // Replaces Playwright's built-in `page` so no browser is ever launched.
    page: async ({ cdpBrowser }, use) => {
      const page = await findAppPage(cdpBrowser);
      const blockedRequests: string[] = [];
      await page.route('**/*', (route) => {
        const req = route.request();
        if (READ_ONLY_METHODS.has(req.method())) return route.continue();
        blockedRequests.push(`${req.method()} ${req.url()}`);
        return route.abort('blockedbyclient');
      });
      await page.evaluate(installClickGuard);
      try {
        await use(page);
      } finally {
        const blockedClicks: string[] = await page
          .evaluate(() => (window as any).__humanTestGuard?.blocked ?? [])
          .catch(() => []);
        await page.evaluate(removeClickGuard).catch(() => {});
        await page.unrouteAll({ behavior: 'ignoreErrors' }).catch(() => {});
        if (blockedRequests.length || blockedClicks.length) {
          await base.info().attach('safety-guard', {
            contentType: 'text/plain',
            body:
              `Blocked writes:\n${blockedRequests.join('\n') || '(none)'}\n\n` +
              `Blocked clicks:\n${blockedClicks.join('\n') || '(none)'}\n`,
          });
        }
      }
    },
  })
  .extend<PlayWrightAiFixtureType>(midscene as any);

export { expect } from '@playwright/test';

// Start every test from Home with no menu or dialog open. Plain Playwright clicks, so
// resetting costs no model call.
export async function goHome(page: Page) {
  await page.keyboard.press('Escape').catch(() => {});
  await page.locator('[data-nav="dashboard"]').first().click();
}
