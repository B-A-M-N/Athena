CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT,
    FOREIGN KEY (parent_id) REFERENCES sessions(id)
);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    blocks TEXT NOT NULL,
    text_content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    provenance TEXT NOT NULL,
    metadata TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX idx_messages_session ON messages(session_id, created_at);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    parent_task_id TEXT,
    status TEXT NOT NULL,
    autonomy TEXT NOT NULL DEFAULT 'supervised',
    objective TEXT NOT NULL,
    acceptance_criteria TEXT,
    context_refs TEXT,
    workspace TEXT,
    capability_policy TEXT,
    model_policy TEXT,
    resource_budget TEXT,
    deadline TEXT,
    delivery TEXT,
    summary TEXT NOT NULL DEFAULT '',
    evidence TEXT,
    artifacts TEXT,
    mutations TEXT,
    unresolved TEXT,
    usage TEXT,
    result_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    metadata TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);
CREATE INDEX idx_tasks_session ON tasks(session_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_parent ON tasks(parent_task_id);

CREATE TABLE task_relations (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id, kind),
    FOREIGN KEY (parent_id) REFERENCES tasks(id),
    FOREIGN KEY (child_id) REFERENCES tasks(id)
);
CREATE INDEX idx_task_relations_child ON task_relations(child_id);

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    session_id TEXT,
    type TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    payload TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    UNIQUE (task_id, sequence)
);
CREATE INDEX idx_events_task ON events(task_id, sequence);
CREATE INDEX idx_events_session ON events(session_id);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    uri TEXT NOT NULL,
    hash TEXT,
    mime_type TEXT,
    size INTEGER,
    producer TEXT,
    task_id TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX idx_artifacts_task ON artifacts(task_id);

CREATE TABLE runtime_sessions (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    backend TEXT NOT NULL,
    pid INTEGER,
    is_alive INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    last_heartbeat TEXT,
    ended_at TEXT,
    metadata TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX idx_runtime_sessions_task ON runtime_sessions(task_id);

CREATE TABLE executions (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    runtime_session_id TEXT,
    command TEXT,
    args TEXT,
    cwd TEXT,
    env TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    exit_code INTEGER,
    stdout_path TEXT,
    stderr_path TEXT,
    metadata TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (runtime_session_id) REFERENCES runtime_sessions(id)
);
CREATE INDEX idx_executions_task ON executions(task_id);
CREATE INDEX idx_executions_runtime ON executions(runtime_session_id);

CREATE TABLE mutations (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    execution_id TEXT,
    resource TEXT NOT NULL,
    operation TEXT NOT NULL,
    reversible INTEGER NOT NULL DEFAULT 0,
    before_state TEXT,
    after_state TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (execution_id) REFERENCES executions(id)
);
CREATE INDEX idx_mutations_task ON mutations(task_id);

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    capability_id TEXT NOT NULL,
    arguments TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolver TEXT,
    metadata TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX idx_approvals_task ON approvals(task_id);

CREATE TABLE approval_grants (
    id TEXT PRIMARY KEY,
    approval_id TEXT,
    capability_id TEXT NOT NULL,
    scope TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT,
    FOREIGN KEY (approval_id) REFERENCES approvals(id)
);

CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    content TEXT NOT NULL,
    text_content TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    source_task_id TEXT,
    source_session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT,
    FOREIGN KEY (source_task_id) REFERENCES tasks(id),
    FOREIGN KEY (source_session_id) REFERENCES sessions(id)
);
CREATE INDEX idx_memories_scope ON memories(scope);
CREATE INDEX idx_memories_kind ON memories(kind);

CREATE TABLE memory_relations (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, kind),
    FOREIGN KEY (source_id) REFERENCES memories(id),
    FOREIGN KEY (target_id) REFERENCES memories(id)
);

CREATE TABLE skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    content TEXT NOT NULL,
    text_content TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT
);
CREATE INDEX idx_skills_name ON skills(name);

CREATE TABLE skill_versions (
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (skill_id, version),
    FOREIGN KEY (skill_id) REFERENCES skills(id)
);

CREATE TABLE scheduled_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cron TEXT,
    payload TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run TEXT,
    last_run TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT
);
CREATE INDEX idx_scheduled_jobs_next ON scheduled_jobs(next_run);

CREATE TABLE job_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    task_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    error TEXT,
    metadata TEXT,
    FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX idx_job_runs_job ON job_runs(job_id);

CREATE TABLE provider_usage (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    task_id TEXT,
    session_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    metadata TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX idx_provider_usage_task ON provider_usage(task_id);
CREATE INDEX idx_provider_usage_started ON provider_usage(started_at);

CREATE VIRTUAL TABLE messages_fts USING fts5(
    text_content,
    content='messages',
    content_rowid='rowid'
);

CREATE VIRTUAL TABLE memories_fts USING fts5(
    text_content,
    content='memories',
    content_rowid='rowid'
);

CREATE VIRTUAL TABLE skills_fts USING fts5(
    text_content,
    content='skills',
    content_rowid='rowid'
);

CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text_content) VALUES (new.rowid, new.text_content);
END;

CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text_content) VALUES('delete', old.rowid, old.text_content);
END;

CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text_content) VALUES('delete', old.rowid, old.text_content);
  INSERT INTO messages_fts(rowid, text_content) VALUES (new.rowid, new.text_content);
END;

CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, text_content) VALUES (new.rowid, new.text_content);
END;

CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text_content) VALUES('delete', old.rowid, old.text_content);
END;

CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text_content) VALUES('delete', old.rowid, old.text_content);
  INSERT INTO memories_fts(rowid, text_content) VALUES (new.rowid, new.text_content);
END;

CREATE TRIGGER skills_ai AFTER INSERT ON skills BEGIN
  INSERT INTO skills_fts(rowid, text_content) VALUES (new.rowid, new.text_content);
END;

CREATE TRIGGER skills_ad AFTER DELETE ON skills BEGIN
  INSERT INTO skills_fts(skills_fts, rowid, text_content) VALUES('delete', old.rowid, old.text_content);
END;

CREATE TRIGGER skills_au AFTER UPDATE ON skills BEGIN
  INSERT INTO skills_fts(skills_fts, rowid, text_content) VALUES('delete', old.rowid, old.text_content);
  INSERT INTO skills_fts(rowid, text_content) VALUES (new.rowid, new.text_content);
END;
