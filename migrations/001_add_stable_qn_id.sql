-- Add the stable public qn id to questions.
-- This is the readable public identity the app uses everywhere; the internal
-- primary key stays questions.id for reports and backend internals.
-- Public shape target: LCCCSSSFQQQQ (level, chapter, subtopic, file scope,
-- sequence). Idempotent: databases patched by hand earlier run it harmlessly.
ALTER TABLE questions
  ADD COLUMN IF NOT EXISTS stable_qn_id TEXT UNIQUE;
