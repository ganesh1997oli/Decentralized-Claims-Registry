-- Durable outbox and idempotency ledger for sponsored ERC-2771 submissions.
--
-- The HTTP process prepares an insurer-signed request and records its signature.
-- A separate relay worker owns EOA nonce allocation and broadcasting. Persisting
-- the signed raw transaction before network submission closes the normal crash
-- window between "sent to Ethereum" and "recorded in PostgreSQL".

CREATE TABLE gasless_claim_submissions (
    submission_id UUID PRIMARY KEY,
    credential_id TEXT NOT NULL,
    insurer_id TEXT NOT NULL,
    signer_address TEXT NOT NULL,
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    contract_address TEXT NOT NULL,
    forwarder_address TEXT NOT NULL,
    idempotency_key_hash TEXT NOT NULL CHECK (length(idempotency_key_hash) = 64),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    client_fingerprint TEXT NOT NULL CHECK (length(client_fingerprint) = 64),
    state TEXT NOT NULL CHECK (
        state IN (
            'preparing', 'prepared', 'authorized', 'signed', 'broadcast',
            'confirmed', 'failed', 'expired'
        )
    ),
    claim_hash TEXT,
    data_pointer TEXT,
    call_data TEXT,
    forwarder_nonce NUMERIC(78, 0),
    forward_gas BIGINT,
    deadline BIGINT,
    insurer_signature TEXT,
    relayer_address TEXT,
    relayer_nonce NUMERIC(78, 0),
    raw_transaction TEXT,
    transaction_hash TEXT,
    max_fee_per_gas NUMERIC(78, 0),
    max_priority_fee_per_gas NUMERIC(78, 0),
    block_number BIGINT,
    claim_id BIGINT,
    relay_attempts INTEGER NOT NULL DEFAULT 0 CHECK (relay_attempts >= 0),
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    authorized_at TIMESTAMPTZ,
    broadcast_at TIMESTAMPTZ,
    last_broadcast_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    UNIQUE (credential_id, idempotency_key_hash)
);

-- One active forwarder nonce per insurer signer. A second browser tab must
-- finish or expire its current request before preparing another one.
CREATE UNIQUE INDEX gasless_claim_submissions_active_signer_idx
    ON gasless_claim_submissions (
        chain_id, lower(forwarder_address), lower(signer_address)
    )
    WHERE state IN (
        'preparing', 'prepared', 'authorized', 'signed', 'broadcast'
    );

CREATE INDEX gasless_claim_submissions_relay_queue_idx
    ON gasless_claim_submissions (state, updated_at, created_at)
    WHERE state IN ('authorized', 'signed', 'broadcast');

CREATE INDEX gasless_claim_submissions_credential_rate_idx
    ON gasless_claim_submissions (credential_id, created_at DESC);

CREATE INDEX gasless_claim_submissions_client_rate_idx
    ON gasless_claim_submissions (client_fingerprint, created_at DESC);

CREATE TABLE gasless_relayer_nonces (
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    relayer_address TEXT NOT NULL,
    next_nonce NUMERIC(78, 0) NOT NULL CHECK (next_nonce >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chain_id, relayer_address)
);

-- Keep every transaction hash for one EOA nonce. A fee-bumped replacement can
-- race the original into a block, so confirmation must inspect all attempts
-- instead of assuming the newest hash is the one that mined.
CREATE TABLE gasless_relay_attempts (
    submission_id UUID NOT NULL REFERENCES gasless_claim_submissions(submission_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    transaction_hash TEXT NOT NULL UNIQUE,
    raw_transaction TEXT NOT NULL,
    relayer_nonce NUMERIC(78, 0) NOT NULL CHECK (relayer_nonce >= 0),
    max_fee_per_gas NUMERIC(78, 0) NOT NULL CHECK (max_fee_per_gas > 0),
    max_priority_fee_per_gas NUMERIC(78, 0) NOT NULL
        CHECK (max_priority_fee_per_gas > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    broadcast_at TIMESTAMPTZ,
    PRIMARY KEY (submission_id, attempt_number)
);

CREATE INDEX gasless_relay_attempts_lookup_idx
    ON gasless_relay_attempts (submission_id, attempt_number DESC);
