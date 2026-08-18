-- Scheduler claim idempotency (BUILDSPEC section 77).
-- Each scheduled occurrence is claimed exactly once via unique (job_id, scheduled_for).
ALTER TABLE job_runs ADD COLUMN scheduled_for TEXT;
ALTER TABLE job_runs ADD COLUMN claim_id TEXT;
CREATE UNIQUE INDEX idx_job_runs_unique_claim ON job_runs(job_id, scheduled_for);
CREATE INDEX idx_job_runs_due ON job_runs(status, scheduled_for);