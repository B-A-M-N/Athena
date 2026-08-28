-- A step can have a known capability outcome while its aggregate row is not
-- finalized yet. Keep that distinction durable so restart can complete from
-- the receipt instead of dispatching the side effect twice.
ALTER TABLE workflow_step_runs ADD COLUMN output_recorded INTEGER NOT NULL DEFAULT 0;
