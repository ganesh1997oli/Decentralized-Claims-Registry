"""PostgreSQL adapter for append-only human fraud-outcome revisions.

This adapter intentionally does not read or write ``claim_assessments``. Model
probabilities remain reproducible screening evidence, while this repository owns
human conclusions that may later be exported through a governed dataset process.
It contains no retraining, label conversion, model deployment, or on-chain write.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from packages.integrations.postgres.database import PostgresDatabase
from packages.integrations.postgres.records import (
    AssessorOutcomeRecord,
    HumanFraudOutcome,
    assessor_outcome_from_row,
)

SELECT_COLUMNS = """
outcome_id, chain_id, contract_address, claim_id, revision, outcome,
assessor_reference, notes, assessed_at
"""


class PostgresAssessorOutcomeRepository:
    """Record and read human conclusions at a deployment-scoped claim seam."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def get_latest_for_claim(
        self,
        *,
        chain_id: int,
        contract_address: str,
        claim_id: int,
    ) -> AssessorOutcomeRecord | None:
        """Return the newest human revision, or ``None`` before human review."""

        with self.database.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {SELECT_COLUMNS}
                FROM claim_assessor_outcomes
                WHERE chain_id = %s AND contract_address = %s AND claim_id = %s
                ORDER BY revision DESC
                LIMIT 1
                """,
                (chain_id, contract_address.lower(), claim_id),
            )
            return assessor_outcome_from_row(cursor.fetchone())

    def record(
        self,
        *,
        chain_id: int,
        contract_address: str,
        claim_id: int,
        outcome: HumanFraudOutcome,
        assessor_reference: str,
        notes: str | None,
        outcome_id: UUID | None = None,
    ) -> AssessorOutcomeRecord:
        """Append one serialized revision and return the committed-shaped row.

        A transaction-scoped advisory lock prevents two assessors from selecting
        the same next revision concurrently. Repeating a submission intentionally
        creates a visible correction revision rather than silently overwriting the
        audit trail. The caller must first verify that the indexed claim exists.
        """

        normalized_contract = contract_address.lower()
        normalized_notes = notes.strip() if notes and notes.strip() else None
        new_outcome_id = outcome_id or uuid4()
        with self.database.cursor() as cursor:
            # Hash the complete deployment-scoped claim identity into PostgreSQL's
            # 64-bit advisory-lock space. Including claim_id in the text avoids the
            # signed 32-bit limit of the two-integer lock overload; contract claim
            # identifiers are BIGINT values and must not be narrowed by this seam.
            # The lock is transaction-scoped, so it also works across API replicas.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{chain_id}:{normalized_contract}:{claim_id}",),
            )
            cursor.execute(
                f"""
                INSERT INTO claim_assessor_outcomes (
                    outcome_id, chain_id, contract_address, claim_id, revision,
                    outcome, assessor_reference, notes
                )
                SELECT %s, %s, %s, %s,
                       COALESCE(MAX(revision), 0) + 1,
                       %s, %s, %s
                FROM claim_assessor_outcomes
                WHERE chain_id = %s AND contract_address = %s AND claim_id = %s
                RETURNING {SELECT_COLUMNS}
                """,
                (
                    new_outcome_id,
                    chain_id,
                    normalized_contract,
                    claim_id,
                    outcome,
                    assessor_reference,
                    normalized_notes,
                    chain_id,
                    normalized_contract,
                    claim_id,
                ),
            )
            record = assessor_outcome_from_row(cursor.fetchone())
            if record is None:
                raise ValueError("PostgreSQL did not return the assessor outcome")
            return record
