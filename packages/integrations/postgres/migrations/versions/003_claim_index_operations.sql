-- Durable audit results for the read-only index reconciliation command.
--
-- Reconciliation never repairs or mutates the blockchain projection. It only
-- compares indexed rows with contract state pinned to the saved checkpoint.
-- Persisting the compact result gives operators an auditable answer to "when
-- was this index last proven consistent?" without copying claim bodies or any
-- private insurer data into another store.

CREATE TABLE claim_index_reconciliations (
    reconciliation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    contract_address TEXT NOT NULL,
    indexed_through_block BIGINT NOT NULL CHECK (indexed_through_block >= 0),
    chain_claims BIGINT NOT NULL CHECK (chain_claims >= 0),
    indexed_claims BIGINT NOT NULL CHECK (indexed_claims >= 0),
    missing_claim_ids BIGINT[] NOT NULL DEFAULT '{}',
    unexpected_claim_ids BIGINT[] NOT NULL DEFAULT '{}',
    mismatched_claim_ids BIGINT[] NOT NULL DEFAULT '{}',
    consistent BOOLEAN NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The operations endpoint reads only the newest result for the selected
-- deployment. This index keeps that lookup bounded as audit history grows.
CREATE INDEX claim_index_reconciliations_latest_idx
    ON claim_index_reconciliations (
        chain_id, contract_address, checked_at DESC, reconciliation_id DESC
    );
