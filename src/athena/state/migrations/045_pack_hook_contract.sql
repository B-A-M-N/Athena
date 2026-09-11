ALTER TABLE pack_hook_outbox ADD COLUMN hook_session_id TEXT;
ALTER TABLE pack_hook_outbox ADD COLUMN pack_version TEXT;
ALTER TABLE pack_hook_outbox ADD COLUMN pack_integrity TEXT;
ALTER TABLE pack_hook_outbox ADD COLUMN workflow_id TEXT;
ALTER TABLE pack_hook_outbox ADD COLUMN workflow_integrity TEXT;
ALTER TABLE pack_hook_outbox ADD COLUMN effect_ceiling TEXT;
ALTER TABLE pack_hook_outbox ADD COLUMN recursion_limit INTEGER;
ALTER TABLE pack_hook_outbox ADD COLUMN hook_contract_digest TEXT;
