-- Durable, rebuildable read model for the public ClaimsRegistry contract.
--
-- The smart contract remains authoritative. These tables contain only values
-- already published by ClaimSubmitted and ClaimAssessed events. They can be
-- dropped and reconstructed by replaying confirmed logs from the deployment
-- block, but keeping them in PostgreSQL makes API pagination a small indexed
-- query instead of one RPC request per rendered claim.

CREATE TABLE claim_index_events (
    event_id TEXT PRIMARY KEY,
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    contract_address TEXT NOT NULL,
    claim_id BIGINT NOT NULL CHECK (claim_id >= 0),
    event_type TEXT NOT NULL CHECK (
        event_type IN ('ClaimSubmitted', 'ClaimAssessed')
    ),
    block_number BIGINT NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL,
    transaction_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL CHECK (log_index >= 0),
    event_timestamp BIGINT NOT NULL CHECK (event_timestamp > 0),
    status SMALLINT NOT NULL CHECK (status BETWEEN 0 AND 4),
    fraud_score INTEGER NOT NULL CHECK (fraud_score BETWEEN 0 AND 10000),
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chain_id, transaction_hash, log_index)
);

CREATE INDEX claim_index_events_deployment_order_idx
    ON claim_index_events (
        chain_id, contract_address, block_number, log_index
    );

CREATE INDEX claim_index_events_claim_history_idx
    ON claim_index_events (
        chain_id, contract_address, claim_id, block_number, log_index
    );

CREATE TABLE indexed_claims (
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    contract_address TEXT NOT NULL,
    claim_id BIGINT NOT NULL CHECK (claim_id >= 0),
    claimant TEXT NOT NULL,
    claim_hash TEXT NOT NULL,
    data_pointer TEXT NOT NULL,
    status SMALLINT NOT NULL CHECK (status BETWEEN 0 AND 4),
    fraud_score INTEGER NOT NULL CHECK (fraud_score BETWEEN 0 AND 10000),
    submitted_at BIGINT NOT NULL CHECK (submitted_at > 0),
    updated_at BIGINT NOT NULL CHECK (updated_at > 0),
    submission_block_number BIGINT NOT NULL CHECK (submission_block_number >= 0),
    submission_transaction_hash TEXT NOT NULL,
    state_block_number BIGINT NOT NULL CHECK (state_block_number >= 0),
    state_log_index INTEGER NOT NULL CHECK (state_log_index >= 0),
    state_event_id TEXT NOT NULL REFERENCES claim_index_events(event_id),
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chain_id, contract_address, claim_id)
);

-- The dashboard's default query is deployment-scoped and newest-first. The
-- INCLUDE columns let PostgreSQL satisfy that page from the index for typical
-- installations while preserving a narrow ordering key.
CREATE INDEX indexed_claims_dashboard_idx
    ON indexed_claims (chain_id, contract_address, claim_id DESC)
    INCLUDE (
        claimant, claim_hash, data_pointer, status, fraud_score,
        submitted_at, updated_at
    );

CREATE INDEX indexed_claims_status_idx
    ON indexed_claims (chain_id, contract_address, status, claim_id DESC);

-- The database checkpoint is shared by the listener and API. Unlike a local
-- file, it survives container replacement and lets every API instance report
-- exactly how far the public read model has advanced.
CREATE TABLE claim_index_checkpoints (
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    contract_address TEXT NOT NULL,
    last_processed_block BIGINT NOT NULL CHECK (last_processed_block >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chain_id, contract_address)
);
