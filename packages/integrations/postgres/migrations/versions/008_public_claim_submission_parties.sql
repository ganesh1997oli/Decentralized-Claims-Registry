-- Public-intake party metadata required to confirm permit-backed submissions.
-- Existing insurer-operated rows remain valid and are explicitly classified as
-- `insurer`; new public rows persist the claimant and insurer values bound into
-- the on-chain permit without storing raw policy references.

ALTER TABLE gasless_claim_submissions
    ADD COLUMN submission_kind TEXT NOT NULL DEFAULT 'insurer'
        CHECK (submission_kind IN ('insurer', 'public')),
    ADD COLUMN claimant_address TEXT,
    ADD COLUMN insurer_address TEXT,
    ADD COLUMN claimant_commitment TEXT,
    ADD COLUMN policy_id TEXT,
    ADD COLUMN permit_issuer_address TEXT;

ALTER TABLE gasless_claim_submissions
    ADD CONSTRAINT gasless_public_submission_parties_check CHECK (
        (
            submission_kind = 'insurer'
            AND claimant_address IS NULL
            AND insurer_address IS NULL
            AND claimant_commitment IS NULL
            AND policy_id IS NULL
            AND permit_issuer_address IS NULL
        )
        OR (
            submission_kind = 'public'
            AND claimant_address IS NOT NULL
            AND claimant_address ~ '^0x[0-9a-fA-F]{40}$'
            AND insurer_address IS NOT NULL
            AND insurer_address ~ '^0x[0-9a-fA-F]{40}$'
            AND claimant_commitment ~ '^0x[0-9a-fA-F]{64}$'
            AND policy_id IS NOT NULL
            AND policy_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'
            AND permit_issuer_address IS NOT NULL
            AND permit_issuer_address ~ '^0x[0-9a-fA-F]{40}$'
        )
    );
