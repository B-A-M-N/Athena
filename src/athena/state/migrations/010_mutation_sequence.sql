ALTER TABLE mutations ADD COLUMN sequence INTEGER;
CREATE INDEX idx_mutations_task_sequence ON mutations(task_id, sequence);
