// b) Open a job: Jobs -> first job -> its detail shows a "Tailor & apply" button.
// NEVER clicks "Tailor & apply": it spends a paid tailoring credit (the fixture also blocks it).
import { test, goHome } from '../fixtures';

test('Opening the first job shows its detail with Tailor & apply', async ({ page, aiTap, aiAssert, aiWaitFor }) => {
  await goHome(page);

  await aiTap('"Jobs" in the left sidebar');
  await aiWaitFor('A list of jobs is showing, each row with a job title and a company', {
    timeoutMs: 20_000,
    checkIntervalMs: 4_000,
  });

  await aiTap('The job title in the first row of the jobs list (not a button inside the row)');

  // A role the person already tailored for shows "Open your resume" instead of "Tailor & apply".
  await aiAssert(
    'A job detail panel is open for the selected job, showing its title and company, and it has ' +
      'a primary button labelled "Tailor & apply" (or "Open your resume" if a resume was already tailored for it)',
  );
});
