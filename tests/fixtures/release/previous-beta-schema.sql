BEGIN TRANSACTION;
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
INSERT INTO "approvals" VALUES('previous-approval','previous-task','files.write','{}','APPROVED','2026-01-01T00:00:00+00:00',NULL,NULL,'{}');
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
CREATE TABLE capability_health (
    capability_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    total_calls INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure TEXT,
    last_failure_at REAL,
    last_success_at REAL,
    opened_at REAL,
    cooldown_seconds REAL NOT NULL DEFAULT 30.0,
    updated_at TEXT NOT NULL
);
CREATE TABLE capability_pack_contributions (
    pack_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    contribution_id TEXT NOT NULL,
    PRIMARY KEY(pack_id, kind, contribution_id),
    FOREIGN KEY(pack_id) REFERENCES capability_packs(id) ON DELETE CASCADE
);
CREATE TABLE capability_packs (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    manifest TEXT NOT NULL,
    install_path TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    source_integrity TEXT NOT NULL,
    installed_at TEXT NOT NULL
);
INSERT INTO "capability_packs" VALUES('previous-pack','1.0.0','{"id":"previous-pack"}','/previous-pack',1,'{"sha256":"previous"}','2026-01-01T00:00:00+00:00');
CREATE TABLE context_block_versions (
    block_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    trust TEXT NOT NULL,
    max_tokens INTEGER NOT NULL,
    attached INTEGER NOT NULL,
    provenance TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(block_id, version)
);
CREATE TABLE context_blocks (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    trust TEXT NOT NULL,
    max_tokens INTEGER NOT NULL,
    attached INTEGER NOT NULL,
    version INTEGER NOT NULL,
    provenance TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE context_digests (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    session_id TEXT,
    principal_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    fields TEXT NOT NULL,
    transcript_anchors TEXT NOT NULL DEFAULT '[]',
    recovery_queries TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, parent_digest_id TEXT, source_digest_ids TEXT NOT NULL DEFAULT '[]', range_start_message_id TEXT, range_end_message_id TEXT);
CREATE TABLE continuations(id TEXT PRIMARY KEY, task_id TEXT, call_id TEXT, capability_id TEXT, canonical_arguments TEXT, schema_hash TEXT, effects TEXT, workspace_id TEXT, approval_id TEXT, provider_profile_id TEXT, model_id TEXT, repair_policy_version TEXT, model_turn INTEGER, policy_context TEXT, created_at TEXT NOT NULL, resolved_at TEXT, decision TEXT, claimed_at TEXT, consumed_at TEXT);
INSERT INTO "continuations" VALUES('previous-continuation','previous-task','previous-call','files.write','{"path": "workspace/file.txt", "content": "old beta"}','previous-schema','["FILESYSTEM_WRITE"]','/previous-project','previous-approval',NULL,NULL,NULL,NULL,'{}','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','granted',NULL,NULL);
CREATE TABLE delegate_sessions (
    id TEXT PRIMARY KEY,
    delegate_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_id TEXT,
    remote_session_id TEXT,
    workspace_root TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    launch_signature TEXT NOT NULL,
    metadata TEXT NOT NULL
);
INSERT INTO "delegate_sessions" VALUES('previous-delegate','previous-delegate-id','previous-task','previous-session','remote-previous','/previous-project','PAUSED','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','previous-launch','{}');
CREATE TABLE events (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    session_id TEXT,
    type TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    payload TEXT, causal_id TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    UNIQUE (task_id, sequence)
);
INSERT INTO "events" VALUES('previous-event','previous-task','previous-session','task.completed',1,'2026-01-01T00:00:00+00:00',1,'{"source":"previous-wheel"}',NULL);
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
CREATE TABLE external_effect_receipts (
    transaction_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    task_id TEXT,
    capability_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    external_identity TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    idempotency_key TEXT,
    response TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, recovery_origin_phase TEXT, recovery_origin_status TEXT, verification_target TEXT);
CREATE TABLE failure_memory (
    id TEXT PRIMARY KEY,
    signature_fingerprint TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    environment_fingerprint TEXT NOT NULL DEFAULT '',
    project_scope TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL,
    remediation TEXT,
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_success TEXT,
    last_failure TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE TABLE generated_capabilities (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    owner TEXT NOT NULL,
    project_scope TEXT,
    user_scope TEXT,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE invariant_results (
    id TEXT PRIMARY KEY,
    invariant_id TEXT NOT NULL,
    task_id TEXT,
    passed INTEGER NOT NULL,
    error TEXT,
    details TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE invariants (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    description TEXT NOT NULL,
    definition TEXT NOT NULL,
    required INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE job_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    task_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    error TEXT,
    metadata TEXT, scheduled_for TEXT, claim_id TEXT,
    FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
INSERT INTO "job_runs" VALUES('previous-job-run','previous-job','previous-task','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','SUCCEEDED',NULL,'{}','2026-01-01T00:00:00+00:00',NULL);
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
INSERT INTO "memories" VALUES('previous-memory','session','remembered beta fact','remembered beta fact','fact','previous-task','previous-session','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','{}');
CREATE VIRTUAL TABLE memories_fts USING fts5(text_content, content='memories', content_rowid='rowid');
INSERT INTO memories_fts(rowid, text_content) SELECT rowid, text_content FROM memories;
CREATE TABLE memory_embeddings (
    memory_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    vector TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE TABLE memory_relations (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, kind),
    FOREIGN KEY (source_id) REFERENCES memories(id),
    FOREIGN KEY (target_id) REFERENCES memories(id)
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
INSERT INTO "messages" VALUES('previous-message','previous-session','user','[]','preserve N-1 transcript','2026-01-01T00:00:00+00:00','{}','{"source":"previous-wheel"}');
CREATE VIRTUAL TABLE messages_fts USING fts5(text_content, content='messages', content_rowid='rowid');
INSERT INTO messages_fts(rowid, text_content) SELECT rowid, text_content FROM messages;
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
    metadata TEXT, status TEXT NOT NULL DEFAULT 'COMPLETED', before_ref TEXT, inverse TEXT, sequence INTEGER,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (execution_id) REFERENCES executions(id)
);
INSERT INTO "mutations" VALUES('previous-mutation','previous-task',NULL,'workspace/file.txt','write',1,'{}','{"content":"old beta"}','2026-01-01T00:00:00+00:00','{}','COMPLETED',NULL,'{"operation":"restore"}',1);
CREATE TABLE project_indexes (
    root TEXT PRIMARY KEY,
    index_revision TEXT NOT NULL,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
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
INSERT INTO "provider_usage" VALUES('previous-usage','fixture-provider','fixture-model','previous-task','previous-session',10,5,'0.01','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','{}');
CREATE TABLE research_claim_evidence (
    claim_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(claim_id, evidence_id),
    FOREIGN KEY(evidence_id) REFERENCES research_evidence(id)
);
CREATE TABLE research_evidence (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    claim_id TEXT,
    extracted_claim TEXT NOT NULL,
    exact_supporting_excerpt TEXT NOT NULL,
    locator TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    authority_class TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    extraction_model TEXT,
    confidence REAL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES research_sources(id)
);
CREATE TABLE research_evidence_links (
    evidence_id TEXT NOT NULL,
    related_evidence_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    PRIMARY KEY(evidence_id, related_evidence_id, relation),
    FOREIGN KEY(evidence_id) REFERENCES research_evidence(id),
    FOREIGN KEY(related_evidence_id) REFERENCES research_evidence(id)
);
CREATE TABLE research_gaps (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    question TEXT NOT NULL,
    kind TEXT NOT NULL,
    required INTEGER NOT NULL,
    status TEXT NOT NULL,
    task_id TEXT,
    evidence_ids TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    metadata TEXT NOT NULL
);
CREATE TABLE research_source_content (
    source_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(source_id) REFERENCES research_sources(id) ON DELETE CASCADE
);
CREATE TABLE research_sources (
    id TEXT PRIMARY KEY,
    canonical_uri TEXT NOT NULL,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    authority_class TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    published_at TEXT,
    content_hash TEXT,
    artifact_uri TEXT,
    task_id TEXT,
    project_id TEXT,
    metadata TEXT NOT NULL
);
CREATE TABLE runtime_sessions (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    backend TEXT NOT NULL,
    pid INTEGER,
    is_alive INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    last_heartbeat TEXT,
    ended_at TEXT,
    metadata TEXT, runtime TEXT, cwd TEXT, workspace_identity TEXT, network_policy TEXT, process_identity TEXT, environment_fingerprint TEXT, runtime_version TEXT, start_identity TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
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
INSERT INTO "scheduled_jobs" VALUES('previous-job','previous scheduled job','0 * * * *','{}',1,'2026-01-01T00:00:00+00:00',NULL,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','{}');
CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
INSERT INTO "schema_migrations" VALUES('001','previous-release');
INSERT INTO "schema_migrations" VALUES('002','previous-release');
INSERT INTO "schema_migrations" VALUES('003','previous-release');
INSERT INTO "schema_migrations" VALUES('004','previous-release');
INSERT INTO "schema_migrations" VALUES('005','previous-release');
INSERT INTO "schema_migrations" VALUES('006','previous-release');
INSERT INTO "schema_migrations" VALUES('007','previous-release');
INSERT INTO "schema_migrations" VALUES('008','previous-release');
INSERT INTO "schema_migrations" VALUES('009','previous-release');
INSERT INTO "schema_migrations" VALUES('010','previous-release');
INSERT INTO "schema_migrations" VALUES('011','previous-release');
INSERT INTO "schema_migrations" VALUES('012','previous-release');
INSERT INTO "schema_migrations" VALUES('013','previous-release');
INSERT INTO "schema_migrations" VALUES('014','previous-release');
INSERT INTO "schema_migrations" VALUES('015','previous-release');
INSERT INTO "schema_migrations" VALUES('016','previous-release');
INSERT INTO "schema_migrations" VALUES('017','previous-release');
INSERT INTO "schema_migrations" VALUES('018','previous-release');
INSERT INTO "schema_migrations" VALUES('019','previous-release');
INSERT INTO "schema_migrations" VALUES('020','previous-release');
INSERT INTO "schema_migrations" VALUES('021','previous-release');
INSERT INTO "schema_migrations" VALUES('022','previous-release');
INSERT INTO "schema_migrations" VALUES('023','previous-release');
INSERT INTO "schema_migrations" VALUES('024','previous-release');
INSERT INTO "schema_migrations" VALUES('025','previous-release');
INSERT INTO "schema_migrations" VALUES('026','previous-release');
INSERT INTO "schema_migrations" VALUES('027','previous-release');
INSERT INTO "schema_migrations" VALUES('028','previous-release');
INSERT INTO "schema_migrations" VALUES('029','previous-release');
INSERT INTO "schema_migrations" VALUES('030','previous-release');
INSERT INTO "schema_migrations" VALUES('031','previous-release');
INSERT INTO "schema_migrations" VALUES('032','previous-release');
INSERT INTO "schema_migrations" VALUES('033','previous-release');
INSERT INTO "schema_migrations" VALUES('034','previous-release');
INSERT INTO "schema_migrations" VALUES('035','previous-release');
INSERT INTO "schema_migrations" VALUES('036','previous-release');
INSERT INTO "schema_migrations" VALUES('037','previous-release');
INSERT INTO "schema_migrations" VALUES('038','previous-release');
CREATE TABLE self_host_missions (
    id TEXT PRIMARY KEY,
    project_root TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    current_task_id TEXT,
    base_revision TEXT NOT NULL,
    design_bundle_hash TEXT NOT NULL,
    gate_bundle_hash TEXT NOT NULL,
    candidate_fingerprint TEXT,
    plan TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, current_base_fingerprint TEXT, current_git_revision TEXT, current_design_bundle_hash TEXT, current_gate_bundle_hash TEXT,
    FOREIGN KEY (current_task_id) REFERENCES tasks(id)
);
INSERT INTO "self_host_missions" VALUES('previous-mission','/previous-project','preserve N-1 continuation','PAUSED','previous-task','previous-source','previous-design','previous-gates',NULL,'{"phase":"CONTINUE","source":"previous-wheel"}',NULL,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',NULL,NULL,NULL,NULL);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT, principal_id TEXT, project_id TEXT,
    FOREIGN KEY (parent_id) REFERENCES sessions(id)
);
INSERT INTO "sessions" VALUES('previous-session',NULL,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','{"release":"0.1.0b1","source":"previous-wheel"}',NULL,NULL);
CREATE TABLE skill_candidates (
    id TEXT PRIMARY KEY,
    source_task_id TEXT NOT NULL,
    name TEXT NOT NULL,
    draft TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0,
    lifecycle_state TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
    promoted_skill_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT
);
CREATE TABLE skill_versions (
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (skill_id, version),
    FOREIGN KEY (skill_id) REFERENCES skills(id)
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
CREATE VIRTUAL TABLE skills_fts USING fts5(text_content, content='skills', content_rowid='rowid');
INSERT INTO skills_fts(rowid, text_content) SELECT rowid, text_content FROM skills;
CREATE TABLE task_relations (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id, kind),
    FOREIGN KEY (parent_id) REFERENCES tasks(id),
    FOREIGN KEY (child_id) REFERENCES tasks(id)
);
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
    metadata TEXT, claimed_by TEXT, claim_started_at TEXT, lease_expires_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);
INSERT INTO "tasks" VALUES('previous-task','previous-session',NULL,'COMPLETE','supervised','preserve N-1 task',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'previous release task',NULL,NULL,NULL,NULL,NULL,NULL,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',NULL,NULL,NULL,NULL,NULL,NULL);
CREATE TABLE tool_repairs (
    id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL UNIQUE,
    task_id TEXT,
    capability_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    outcome TEXT NOT NULL,
    schema_hash TEXT,
    repair_policy_version TEXT NOT NULL,
    provider_profile_id TEXT,
    model_id TEXT,
    original_shape_hash TEXT,
    canonical_shape_hash TEXT,
    original_arguments TEXT NOT NULL,
    canonical_arguments TEXT,
    created_at TEXT NOT NULL
, arguments_replayable INTEGER NOT NULL DEFAULT 1, arguments_sensitive INTEGER NOT NULL DEFAULT 0);
CREATE TABLE workflow_observations (
    id TEXT PRIMARY KEY,
    trace_signature TEXT NOT NULL,
    task_id TEXT NOT NULL,
    workspace_id TEXT,
    workspace_revision TEXT,
    steps TEXT NOT NULL,
    argument_shape TEXT NOT NULL,
    verification TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (trace_signature, task_id)
);
CREATE TABLE workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    task_id TEXT,
    status TEXT NOT NULL,
    inputs TEXT NOT NULL,
    outputs TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, definition_hash TEXT, input_hash TEXT, workspace_identity TEXT, workspace_revision TEXT, environment_identity TEXT, initial_environment_identity TEXT, parent_call_id TEXT, parent_workflow_id TEXT);
INSERT INTO "workflow_runs" VALUES('previous-workflow-run','previous-workflow','previous-task','COMPLETE','{}','{}','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);
CREATE TABLE workflow_step_item_runs (
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    item_index INTEGER NOT NULL,
    execution_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    capability_id TEXT,
    argument_digest TEXT,
    state TEXT NOT NULL,
    output TEXT,
    failures TEXT NOT NULL DEFAULT '[]',
    approval_id TEXT,
    continuation_id TEXT,
    nested_run_id TEXT,
    external_transaction_id TEXT,
    output_recorded INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    PRIMARY KEY (run_id, step_id, item_index),
    FOREIGN KEY (run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
);
CREATE TABLE workflow_step_runs (
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL,
    output TEXT,
    failures TEXT NOT NULL DEFAULT '[]',
    started_at TEXT,
    completed_at TEXT, execution_records TEXT NOT NULL DEFAULT '[]', execution_id TEXT, call_id TEXT, argument_digest TEXT, capability_id TEXT, state TEXT NOT NULL DEFAULT 'PENDING', approval_id TEXT, continuation_id TEXT, output_recorded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, step_id),
    FOREIGN KEY (run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
);
CREATE TABLE workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scope TEXT NOT NULL,
    task_scope TEXT,
    project_scope TEXT,
    user_scope TEXT,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO "workflows" VALUES('previous-workflow','previous workflow','task','previous-task',NULL,NULL,'{"id":"previous-workflow","name":"previous workflow","steps":[],"scope":"task","task_scope":"previous-task","version":1}','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
CREATE INDEX idx_messages_session ON messages(session_id, created_at);
CREATE INDEX idx_tasks_session ON tasks(session_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_parent ON tasks(parent_task_id);
CREATE INDEX idx_task_relations_child ON task_relations(child_id);
CREATE INDEX idx_events_task ON events(task_id, sequence);
CREATE INDEX idx_events_session ON events(session_id);
CREATE INDEX idx_artifacts_task ON artifacts(task_id);
CREATE INDEX idx_runtime_sessions_task ON runtime_sessions(task_id);
CREATE INDEX idx_executions_task ON executions(task_id);
CREATE INDEX idx_executions_runtime ON executions(runtime_session_id);
CREATE INDEX idx_mutations_task ON mutations(task_id);
CREATE INDEX idx_approvals_task ON approvals(task_id);
CREATE INDEX idx_memories_scope ON memories(scope);
CREATE INDEX idx_memories_kind ON memories(kind);
CREATE INDEX idx_skills_name ON skills(name);
CREATE INDEX idx_scheduled_jobs_next ON scheduled_jobs(next_run);
CREATE INDEX idx_job_runs_job ON job_runs(job_id);
CREATE INDEX idx_provider_usage_task ON provider_usage(task_id);
CREATE INDEX idx_provider_usage_started ON provider_usage(started_at);
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
CREATE UNIQUE INDEX idx_job_runs_unique_claim ON job_runs(job_id, scheduled_for);
CREATE INDEX idx_job_runs_due ON job_runs(status, scheduled_for);
CREATE INDEX idx_mutations_status ON mutations(status);
CREATE INDEX idx_workflows_scope
    ON workflows(scope, task_scope, project_scope, user_scope);
CREATE INDEX idx_generated_scope_owner
    ON generated_capabilities(scope, owner);
CREATE UNIQUE INDEX idx_research_source_version
    ON research_sources(canonical_uri, content_hash);
CREATE INDEX idx_research_evidence_source
    ON research_evidence(source_id);
CREATE INDEX idx_research_evidence_claim
    ON research_evidence(claim_id);
CREATE INDEX idx_research_gaps_task_status
    ON research_gaps(task_id, status);
CREATE INDEX idx_tool_repairs_task
    ON tool_repairs(task_id, created_at);
CREATE INDEX idx_research_source_content_hash
    ON research_source_content(content_hash);
CREATE INDEX idx_mutations_task_sequence ON mutations(task_id, sequence);
CREATE INDEX idx_invariants_task ON invariants(task_id, created_at);
CREATE INDEX idx_invariant_results_task
    ON invariant_results(task_id, checked_at);
CREATE INDEX idx_context_blocks_scope
    ON context_blocks(scope, scope_id, attached, updated_at);
CREATE INDEX idx_context_block_versions_block
    ON context_block_versions(block_id, version);
CREATE INDEX idx_capability_packs_enabled
    ON capability_packs(enabled, id);
CREATE INDEX idx_capability_pack_contributions_pack
    ON capability_pack_contributions(pack_id, kind);
CREATE INDEX idx_delegate_sessions_task
    ON delegate_sessions(task_id, last_seen_at);
CREATE INDEX idx_project_indexes_updated
    ON project_indexes(updated_at);
CREATE INDEX idx_failure_memory_signature
    ON failure_memory(signature_fingerprint, capability_id);
CREATE INDEX idx_failure_memory_scope
    ON failure_memory(project_scope, environment_fingerprint, updated_at);
CREATE INDEX idx_workflow_runs_task
    ON workflow_runs(task_id, updated_at);
CREATE INDEX idx_capability_health_status
    ON capability_health(status, updated_at);
CREATE INDEX idx_workflow_step_call
    ON workflow_step_runs(run_id, call_id);
CREATE INDEX idx_external_effect_idempotency
    ON external_effect_receipts(idempotency_key);
CREATE UNIQUE INDEX uq_external_effect_identity_key
    ON external_effect_receipts(capability_id, external_identity, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX idx_workflow_item_call
    ON workflow_step_item_runs(call_id);
CREATE UNIQUE INDEX idx_workflow_item_execution
    ON workflow_step_item_runs(execution_id);
CREATE INDEX idx_workflow_item_nested
    ON workflow_step_item_runs(nested_run_id);
CREATE INDEX idx_workflow_runs_parent_call
    ON workflow_runs(task_id, parent_call_id, workflow_id);
CREATE INDEX idx_self_host_missions_status
    ON self_host_missions(status, updated_at);
CREATE INDEX idx_events_session_type
ON events(session_id, type);
CREATE INDEX idx_workflow_observations_signature
    ON workflow_observations(trace_signature, observed_at, created_at);
CREATE INDEX idx_workflow_observations_observed_at
    ON workflow_observations(observed_at);
CREATE INDEX idx_skill_candidates_state
    ON skill_candidates(lifecycle_state, updated_at);
CREATE INDEX idx_skill_candidates_source_task
    ON skill_candidates(source_task_id);
CREATE INDEX idx_context_digests_task
    ON context_digests(task_id, level DESC, updated_at DESC);
CREATE INDEX idx_context_digests_session
    ON context_digests(principal_id, session_id, level DESC, updated_at DESC);
CREATE INDEX idx_runtime_sessions_backend_runtime
    ON runtime_sessions(backend, runtime);
CREATE INDEX idx_sessions_principal
    ON sessions(principal_id, created_at);
CREATE INDEX idx_sessions_principal_project
    ON sessions(principal_id, project_id, created_at);
CREATE INDEX idx_context_digests_parent
    ON context_digests(parent_digest_id);
CREATE INDEX idx_memory_embeddings_model
    ON memory_embeddings(embedding_model, embedding_version);
CREATE INDEX idx_runtime_sessions_process_identity
    ON runtime_sessions(process_identity, start_identity);
COMMIT;
