// c) Memory page: profile photo -> My profile -> Self-identification lists four questions.
// Looks only: never clicks Edit, Show, Forget or the decline checkbox (the fixture also blocks them).
import { test, goHome } from '../fixtures';

test('My profile lists the Self-identification questions', async ({ page, aiTap, aiScroll, aiAssert, aiWaitFor }) => {
  await goHome(page);

  await aiTap('The profile photo (round avatar) at the bottom of the left sidebar');
  await aiTap('"My profile" in the menu that just opened');
  await aiWaitFor('A profile page is showing with cards of saved information', {
    timeoutMs: 20_000,
    checkIntervalMs: 4_000,
  });

  // Bring the Self-identification card into view. A plain scroll (no model call) when the
  // card is in the page; the plain-English scroll is the fallback.
  // Centre the whole card so its heading and all its rows are on screen together.
  const centred = await page
    .evaluate(() => {
      const card = document.getElementById('memEeo');
      if (!card || card.offsetParent === null) return false;
      card.scrollIntoView({ block: 'center' });
      return true;
    })
    .catch(() => false);
  if (!centred) {
    // (scroll-params-first form: the fixture's type only exposes this overload)
    await aiScroll({ direction: 'down', scrollType: 'untilBottom' }, 'the profile page content');
  }

  await aiAssert(
    'A card titled "Self-identification" is visible and lists these questions: Gender, Transgender, ' +
      'Sexual orientation, and First-generation professional',
  );
});
