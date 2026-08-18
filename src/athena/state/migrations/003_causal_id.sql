-- P0-21: persist causal_id on events.
-- Event sequencing is now computed inside the EventStore; causal_id links an
-- event to the causal event that produced it (e.g. the model response that
-- requested a capability).
ALTER TABLE events ADD COLUMN causal_id TEXT;