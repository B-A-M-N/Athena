ALTER TABLE tool_repairs ADD COLUMN arguments_replayable INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tool_repairs ADD COLUMN arguments_sensitive INTEGER NOT NULL DEFAULT 0;
