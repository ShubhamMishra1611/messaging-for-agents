CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    from_agent VARCHAR(128) NOT NULL,
    "to" VARCHAR(128) NOT NULL,
    type VARCHAR(16) NOT NULL,
    content JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    recipient_count INT NOT NULL DEFAULT 1,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_to ON messages ("to");
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages (from_agent);

CREATE TABLE IF NOT EXISTS agents (
    agent_id VARCHAR(128) PRIMARY KEY,
    capabilities TEXT[] NOT NULL DEFAULT '{}',
    status VARCHAR(16) NOT NULL DEFAULT 'idle',
    server_id VARCHAR(128) NOT NULL,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS group_memberships (
    id SERIAL PRIMARY KEY,
    group_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    UNIQUE (group_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_group_memberships_group ON group_memberships (group_id);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    id SERIAL PRIMARY KEY,
    message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    agent_id VARCHAR(128) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (message_id, agent_id)
);

CREATE TABLE IF NOT EXISTS acks (
    id SERIAL PRIMARY KEY,
    message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    agent_id VARCHAR(128) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    acked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (message_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_acks_message_id ON acks (message_id);

CREATE TABLE IF NOT EXISTS claims (
    id SERIAL PRIMARY KEY,
    message_id UUID UNIQUE NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_claims_agent_id ON claims (agent_id);
