ALTER TABLE self_host_missions ADD COLUMN current_git_revision TEXT;
ALTER TABLE self_host_missions ADD COLUMN current_design_bundle_hash TEXT;
ALTER TABLE self_host_missions ADD COLUMN current_gate_bundle_hash TEXT;
