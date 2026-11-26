-- Causality is intentionally nullable. A terminal task result alone does
-- not prove that a skill materially contributed to the result.
ALTER TABLE skill_evidence ADD COLUMN materially_contributed INTEGER;
