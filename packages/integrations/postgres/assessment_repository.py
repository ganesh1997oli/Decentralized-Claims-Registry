"""PostgreSQL adapter for replay-safe model assessment records.

The public blockchain stores only the compact status and score. This module
owns the richer probability, threshold, SHAP reasons, processing state and
write-back receipt. It deliberately knows nothing about duplicate fingerprints,
feature history, connection construction or schema migrations.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from packages.integrations.postgres.database import PostgresDatabase
from packages.integrations.postgres.records import AssessmentRecord, assessment_from_row

SELECT_COLUMNS = """
event_id, chain_id, contract_address, claim_id, model_version, probability,
threshold, fraud_score, assessment_status, reasons, processing_status,
transaction_hash, block_number, error
"""


class PostgresAssessmentRepository:
    """Persist one immutable scoring decision through its chain write-back."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def get_by_event_id(self, event_id: str) -> AssessmentRecord | None:
        with self.database.cursor() as cursor:
            cursor.execute(
                f"SELECT {SELECT_COLUMNS} FROM claim_assessments WHERE event_id = %s",
                (event_id,),
            )
            return assessment_from_row(cursor.fetchone())

    def get_latest_for_claim(
        self,
        *,
        chain_id: int,
        contract_address: str,
        claim_id: int,
    ) -> AssessmentRecord | None:
        with self.database.cursor() as cursor:
            cursor.execute(
                f"SELECT {SELECT_COLUMNS} FROM claim_assessments "
                "WHERE chain_id = %s AND contract_address = %s "
                "AND claim_id = %s ORDER BY updated_at DESC LIMIT 1",
                (chain_id, contract_address.lower(), claim_id),
            )
            return assessment_from_row(cursor.fetchone())

    def save_scored(self, record: AssessmentRecord) -> None:
        reasons = json.dumps([asdict(reason) for reason in record.reasons])
        with self.database.cursor() as cursor:
            # A replay may repair a failed or half-finished attempt, but a
            # completed record is immutable. Users therefore never see a score
            # silently replaced after a model artifact changes.
            cursor.execute(
                """
                INSERT INTO claim_assessments (
                    event_id, chain_id, contract_address, claim_id,
                    model_version, probability, threshold, fraud_score,
                    assessment_status, reasons, processing_status
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'scored'
                )
                ON CONFLICT (event_id) DO UPDATE SET
                    model_version = EXCLUDED.model_version,
                    probability = EXCLUDED.probability,
                    threshold = EXCLUDED.threshold,
                    fraud_score = EXCLUDED.fraud_score,
                    assessment_status = EXCLUDED.assessment_status,
                    reasons = EXCLUDED.reasons,
                    processing_status = 'scored',
                    transaction_hash = NULL,
                    block_number = NULL,
                    error = NULL,
                    updated_at = NOW()
                WHERE claim_assessments.processing_status <> 'completed'
                """,
                (
                    record.event_id,
                    record.chain_id,
                    record.contract_address.lower(),
                    record.claim_id,
                    record.model_version,
                    record.probability,
                    record.threshold,
                    record.fraud_score,
                    record.status,
                    reasons,
                ),
            )

    def mark_completed(
        self,
        event_id: str,
        *,
        transaction_hash: str | None,
        block_number: int | None,
    ) -> None:
        with self.database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE claim_assessments
                SET processing_status = 'completed',
                    transaction_hash = %s,
                    block_number = %s,
                    error = NULL,
                    updated_at = NOW()
                WHERE event_id = %s
                """,
                (transaction_hash, block_number, event_id),
            )

    def mark_failed(self, event_id: str, error: str) -> None:
        with self.database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE claim_assessments
                SET processing_status = 'failed',
                    error = %s,
                    updated_at = NOW()
                WHERE event_id = %s
                """,
                (error[:2_000], event_id),
            )
