-- Persist recovery provenance outside the JSON response so VERIFY cannot
-- silently lose the original effect direction after restart.
ALTER TABLE external_effect_receipts ADD COLUMN recovery_origin_phase TEXT;
ALTER TABLE external_effect_receipts ADD COLUMN recovery_origin_status TEXT;
ALTER TABLE external_effect_receipts ADD COLUMN verification_target TEXT;
