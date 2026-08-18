"""PostgreSQL adapter for immutable insurer coverage-decision proposals."""

from __future__ import annotations

from uuid import UUID

from packages.integrations.postgres.database import PostgresDatabase
from packages.integrations.postgres.records import (
    CoverageDecisionProposalRecord,
    CoverageDecisionStatus,
    coverage_decision_proposal_from_row,
)

SELECT_COLUMNS = """
decision_id, chain_id, contract_address, claim_id, decision_status,
decision_hash, decision_maker_address, proposed_by, human_outcome_id,
human_outcome_revision, created_at, confirmed_transaction_hash, confirmed_at
"""


class CoverageDecisionConflictError(ValueError):
    """Raised when a claim already has a different immutable proposal."""


class PostgresCoverageDecisionRepository:
    """Create one auditable proposal per deployment-scoped claim."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def create_or_get(
        self,
        *,
        decision_id: UUID,
        chain_id: int,
        contract_address: str,
        claim_id: int,
        decision_status: CoverageDecisionStatus,
        decision_hash: str,
        decision_maker_address: str,
        proposed_by: str,
        human_outcome_id: UUID,
        human_outcome_revision: int,
    ) -> CoverageDecisionProposalRecord:
        """Insert once, returning an exact retry and rejecting a competing one.

        The database uniqueness constraint is the concurrency boundary between
        API replicas. A request that lost its HTTP response can safely retry the
        same proposal; a different wallet, outcome revision, or terminal status
        requires an explicit governance migration rather than silent mutation.
        """

        normalized_contract = contract_address.lower()
        normalized_maker = decision_maker_address.lower()
        with self.database.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO coverage_decision_proposals (
                    decision_id, chain_id, contract_address, claim_id,
                    decision_status, decision_hash, decision_maker_address,
                    proposed_by, human_outcome_id, human_outcome_revision
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (chain_id, contract_address, claim_id) DO NOTHING
                RETURNING {SELECT_COLUMNS}
                """,
                (
                    decision_id,
                    chain_id,
                    normalized_contract,
                    claim_id,
                    decision_status,
                    decision_hash.lower(),
                    normalized_maker,
                    proposed_by,
                    human_outcome_id,
                    human_outcome_revision,
                ),
            )
            record = coverage_decision_proposal_from_row(cursor.fetchone())
            if record is None:
                cursor.execute(
                    f"""
                    SELECT {SELECT_COLUMNS}
                    FROM coverage_decision_proposals
                    WHERE chain_id = %s
                      AND contract_address = %s
                      AND claim_id = %s
                    """,
                    (chain_id, normalized_contract, claim_id),
                )
                record = coverage_decision_proposal_from_row(cursor.fetchone())
            if record is None:
                raise ValueError("PostgreSQL did not return the decision proposal")

        # Domain conflicts are evaluated after the database context closes. The
        # shared database adapter intentionally normalizes unexpected exceptions
        # raised inside its context, but this conflict is a stable HTTP 409—not
        # a storage outage—and must retain its precise type at the service seam.
        retry_identity = (
            record.decision_status,
            record.decision_hash,
            record.decision_maker_address,
            record.proposed_by,
            record.human_outcome_id,
            record.human_outcome_revision,
        )
        requested_identity = (
            decision_status,
            decision_hash.lower(),
            normalized_maker,
            proposed_by,
            human_outcome_id,
            human_outcome_revision,
        )
        if retry_identity != requested_identity:
            raise CoverageDecisionConflictError(
                "This claim already has a different coverage decision proposal"
            )
        return record
