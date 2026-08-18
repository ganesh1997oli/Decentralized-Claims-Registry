-- One-time wallet challenges for public claimant sessions.
--
-- Challenges are intentionally durable across API replicas. A valid wallet
-- signature may create one short-lived bearer session exactly once; replaying
-- the same signed challenge after consumption is rejected under a row lock.

CREATE TABLE claimant_auth_challenges (
    challenge_id UUID PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    nonce TEXT NOT NULL UNIQUE CHECK (length(nonce) BETWEEN 16 AND 64),
    message TEXT NOT NULL CHECK (length(message) BETWEEN 64 AND 4096),
    client_fingerprint TEXT NOT NULL
        CHECK (length(client_fingerprint) = 64),
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    CHECK (expires_at > issued_at),
    CHECK (consumed_at IS NULL OR consumed_at >= issued_at)
);

CREATE INDEX claimant_auth_challenges_client_rate_idx
    ON claimant_auth_challenges (client_fingerprint, issued_at DESC);

CREATE INDEX claimant_auth_challenges_wallet_rate_idx
    ON claimant_auth_challenges (lower(wallet_address), issued_at DESC);

CREATE INDEX claimant_auth_challenges_expiry_idx
    ON claimant_auth_challenges (expires_at)
    WHERE consumed_at IS NULL;
