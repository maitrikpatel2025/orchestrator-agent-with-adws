-- Orchestrator Database Schema

CREATE TABLE IF NOT EXISTS orchestrator_agents (
    id UUID PRIMARY KEY,
    name VARCHAR,
    model VARCHAR,
    status VARCHAR DEFAULT 'idle',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_developer_workflows (
    id UUID PRIMARY KEY,
    orchestrator_agent_id UUID,
    adw_name VARCHAR NOT NULL,
    workflow_type VARCHAR NOT NULL,
    description TEXT,
    status VARCHAR NOT NULL DEFAULT 'pending',
    current_step VARCHAR,
    total_steps INT DEFAULT 0,
    completed_steps INT DEFAULT 0,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    duration_seconds INT,
    input_data JSONB DEFAULT '{}'::jsonb,
    output_data JSONB DEFAULT '{}'::jsonb,
    error_message TEXT,
    error_step VARCHAR,
    error_count INT DEFAULT 0,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    FOREIGN KEY (orchestrator_agent_id) REFERENCES orchestrator_agents(id)
);

CREATE TABLE IF NOT EXISTS agents (
    id UUID PRIMARY KEY,
    orchestrator_agent_id UUID NOT NULL,
    name VARCHAR NOT NULL,
    model VARCHAR NOT NULL,
    working_dir VARCHAR,
    adw_id VARCHAR,
    adw_step VARCHAR,
    status VARCHAR DEFAULT 'idle',
    session_id VARCHAR,
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    total_cost DECIMAL(10, 6) DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    FOREIGN KEY (orchestrator_agent_id) REFERENCES orchestrator_agents(id)
);

CREATE TABLE IF NOT EXISTS agent_logs (
    id UUID PRIMARY KEY,
    agent_id UUID,
    session_id VARCHAR,
    task_slug VARCHAR,
    adw_id VARCHAR,
    adw_step VARCHAR,
    entry_index INT DEFAULT 0,
    event_category VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    content TEXT,
    payload JSONB DEFAULT '{}'::jsonb,
    summary TEXT,
    timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS system_logs (
    id UUID PRIMARY KEY,
    file_path VARCHAR,
    adw_id VARCHAR,
    adw_step VARCHAR,
    level VARCHAR NOT NULL,
    message TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    timestamp TIMESTAMP NOT NULL DEFAULT NOW()
);
