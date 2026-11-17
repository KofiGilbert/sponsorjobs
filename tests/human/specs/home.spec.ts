// a) Home: the "What are you applying for?" box and at least one role card under TODAY.
import { test, goHome } from '../fixtures';

test('Home shows the applying-for box and today\'s roles', async ({ page, aiAssert, aiWaitFor }) => {
  await goHome(page);

  await aiWaitFor(
    'The main area shows a heading "What are you applying for?" with a text box under it',
    { timeoutMs: 20_000, checkIntervalMs: 4_000 },
  );
  await aiAssert(
    'Under a section label that starts with "Today" there is at least one job role card ' +
      'showing a job title and a company name',
  );
});
