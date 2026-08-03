-- Initial claim-processing schema.
--
-- CREATE IF NOT EXISTS makes this migration safe for research installations
-- created by the older runtime-DDL implementation. The migration runner records
-- the file checksum after the transaction succeeds; later edits are rejected.

CREATE TABLE IF NOT EXISTS claim_assessments (
    event_id TEXT PRIMARY KEY,
    chain_id BIGINT NOT NULL,
    contract_address TEXT NOT NULL,
    claim_id BIGINT NOT NULL,
    model_version TEXT NOT NULL,
    probability DOUBLE PRECISION NOT NULL CHECK (probability BETWEEN 0 AND 1),
    threshold DOUBLE PRECISION NOT NULL CHECK (threshold > 0 AND threshold < 1),
    fraud_score INTEGER NOT NULL CHECK (fraud_score BETWEEN 0 AND 10000),
    assessment_status TEXT NOT NULL CHECK (
        assessment_status IN ('UnderReview', 'Flagged')
    ),
    reasons JSONB NOT NULL,
    processing_status TEXT NOT NULL CHECK (
        processing_status IN ('scored', 'completed', 'failed')
    ),
    transaction_hash TEXT,
    block_number BIGINT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chain_id, contract_address, claim_id)
);

CREATE INDEX IF NOT EXISTS claim_assessments_contract_claim_idx
    ON claim_assessments (
        chain_id, contract_address, claim_id, updated_at DESC
    );

CREATE TABLE IF NOT EXISTS claim_incident_fingerprints (
    chain_id BIGINT NOT NULL,
    contract_address TEXT NOT NULL,
    claim_id BIGINT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    insurer_id TEXT NOT NULL,
    fingerprint_version TEXT NOT NULL,
    incident_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chain_id, contract_address, claim_id)
);

CREATE INDEX IF NOT EXISTS claim_incident_fingerprint_match_idx
    ON claim_incident_fingerprints (
        chain_id,
        contract_address,
        fingerprint_version,
        incident_fingerprint
    );

CREATE TABLE IF NOT EXISTS claim_feature_snapshots (
    event_id TEXT PRIMARY KEY,
    chain_id BIGINT NOT NULL,
    contract_address TEXT NOT NULL,
    claim_id BIGINT NOT NULL,
    feature_version TEXT NOT NULL,
    insurer_id TEXT NOT NULL,
    policy_fingerprint_version TEXT NOT NULL,
    policy_reference_fingerprint TEXT NOT NULL,
    event_timestamp BIGINT NOT NULL CHECK (event_timestamp > 0),
    incident_date DATE NOT NULL,
    claim_type TEXT NOT NULL,
    claim_amount_usd DOUBLE PRECISION NOT NULL CHECK (claim_amount_usd > 0),
    policy_premium_usd DOUBLE PRECISION NOT NULL CHECK (policy_premium_usd > 0),
    claim_to_premium_ratio DOUBLE PRECISION NOT NULL CHECK (
        claim_to_premium_ratio > 0
    ),
    vehicle_age INTEGER NOT NULL CHECK (vehicle_age > 0),
    vehicle_type TEXT NOT NULL,
    country TEXT NOT NULL,
    region_type TEXT NOT NULL,
    third_party_injury_flag BOOLEAN NOT NULL,
    total_loss_flag BOOLEAN NOT NULL,
    report_delay_days INTEGER NOT NULL CHECK (report_delay_days >= 0),
    cross_insurer_duplicate_match_count INTEGER NOT NULL CHECK (
        cross_insurer_duplicate_match_count >= 0
    ),
    prior_policy_claim_count INTEGER NOT NULL CHECK (
        prior_policy_claim_count >= 0
    ),
    prior_insurer_claim_count INTEGER NOT NULL CHECK (
        prior_insurer_claim_count >= 0
    ),
    prior_insurer_average_claim_amount_usd DOUBLE PRECISION CHECK (
        prior_insurer_average_claim_amount_usd > 0
    ),
    claim_to_prior_insurer_average_ratio DOUBLE PRECISION CHECK (
        claim_to_prior_insurer_average_ratio > 0
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chain_id, contract_address, claim_id)
);

CREATE INDEX IF NOT EXISTS claim_feature_snapshots_history_idx
    ON claim_feature_snapshots (
        chain_id,
        contract_address,
        insurer_id,
        policy_reference_fingerprint
    );

