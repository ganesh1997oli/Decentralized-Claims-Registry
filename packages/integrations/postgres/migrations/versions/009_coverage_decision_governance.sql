-- Introduce the governance audit trail and teach the rebuildable event index
-- about the contract's separate terminal-decision event.

ALTER TABLE claim_index_events
    DROP CONSTRAINT claim_index_events_event_type_check;

ALTER TABLE claim_index_events
    ADD CONSTRAINT claim_index_events_event_type_check CHECK (
        event_type IN ('ClaimSubmitted', 'ClaimAssessed', 'ClaimDecided')
    );

-- A proposal contains public references and hashes only; claim evidence and
-- human-review notes remain in their existing private stores. The unique claim
-- key prevents two API replicas from authorising competing final outcomes.
CREATE TABLE coverage_decision_proposals (
    decision_id UUID PRIMARY KEY,
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    contract_address TEXT NOT NULL,
    claim_id BIGINT NOT NULL CHECK (claim_id >= 0),
    decision_status TEXT NOT NULL CHECK (
        decision_status IN ('Approved', 'Rejected')
    ),
    decision_hash TEXT NOT NULL UNIQUE CHECK (
        decision_hash ~ '^0x[0-9a-f]{64}$'
    ),
    decision_maker_address TEXT NOT NULL CHECK (
        decision_maker_address ~ '^0x[0-9a-f]{40}$'
    ),
    proposed_by TEXT NOT NULL CHECK (
        proposed_by ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'
    ),
    human_outcome_id UUID NOT NULL REFERENCES claim_assessor_outcomes(outcome_id)
        ON DELETE RESTRICT,
    human_outcome_revision INTEGER NOT NULL CHECK (human_outcome_revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_transaction_hash TEXT CHECK (
        confirmed_transaction_hash IS NULL
        OR confirmed_transaction_hash ~ '^0x[0-9a-f]{64}$'
    ),
    confirmed_at TIMESTAMPTZ,
    UNIQUE (chain_id, contract_address, claim_id),
    CHECK (
        (confirmed_transaction_hash IS NULL AND confirmed_at IS NULL)
        OR (confirmed_transaction_hash IS NOT NULL AND confirmed_at IS NOT NULL)
    )
);

CREATE INDEX coverage_decision_proposals_pending_idx
    ON coverage_decision_proposals (chain_id, contract_address, created_at)
    WHERE confirmed_transaction_hash IS NULL;
