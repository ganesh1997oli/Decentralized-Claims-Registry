-- Human-reviewed fraud outcomes are deliberately separate from model assessments.
--
-- ``claim_assessments`` records what a versioned model predicted and whether that
-- screening result reached Sepolia. This table records what an authorised human
-- reviewer concluded after considering the claim. Keeping the concepts separate
-- prevents Approved/Rejected business dispositions or model thresholds from being
-- mistaken for ground-truth fraud labels.

CREATE TABLE claim_assessor_outcomes (
    outcome_id UUID PRIMARY KEY,
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    contract_address TEXT NOT NULL,
    claim_id BIGINT NOT NULL CHECK (claim_id >= 0),
    revision INTEGER NOT NULL CHECK (revision > 0),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('ConfirmedFraud', 'Legitimate', 'Inconclusive')
    ),
    assessor_reference TEXT NOT NULL CHECK (
        assessor_reference ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'
    ),
    notes TEXT CHECK (notes IS NULL OR char_length(notes) <= 2000),
    assessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chain_id, contract_address, claim_id, revision)
);

-- Reads always ask for the current revision of one deployment-scoped claim. Older
-- rows remain available as an append-only correction history; a later conclusion
-- never silently destroys the reviewer, time, or wording of an earlier one.
CREATE INDEX claim_assessor_outcomes_latest_idx
    ON claim_assessor_outcomes (
        chain_id, contract_address, claim_id, revision DESC
    );
