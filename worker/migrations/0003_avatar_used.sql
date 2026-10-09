-- Seconds charged against each live interview. One interview never charges more than 15 minutes
-- (INTERVIEW_SECONDS), however long its polite wrap-up runs, so finishing an answer after the
-- 15-minute mark never eats into the person's next interview.
ALTER TABLE avatar_sessions ADD COLUMN used INTEGER NOT NULL DEFAULT 0;
