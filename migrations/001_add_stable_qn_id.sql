-- Migration: add stable public qn id to questions.
-- This is the readable public identity the app should use everywhere.
-- Internal PK stays as questions.id for Dogs/reports/backend internals.
-- Public shape target: LCCCSSSFQQQQ (level, chapter, subtopic, file scope, sequence).

BEGIN;

ALTER TABLE questions
  ADD COLUMN IF NOT EXISTS stable_qn_id TEXT UNIQUE;

COMMIT;
