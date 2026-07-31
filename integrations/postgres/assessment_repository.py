"""Keep the detailed, replay-safe assessment record that does not belong on-chain.

Sepolia stores a compact status and score for public verification. PostgreSQL
keeps the larger operational context: model version, probability, threshold,
SHAP reasons, write-back receipt, and any failure. The deterministic Kafka event
ID prevents a replay from creating a second assessment.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Callable

from duplicates import DuplicateCheck, DuplicateMatch
from integrations.postgres.feature_processor import (
    ClaimFeatureInput,
    ClaimFeatureSnapshot,
)
from model.contracts import FraudReason, FraudScore


class PostgresConfigurationError(ValueError):
    """Raised when assessment storage cannot be configured."""


class PostgresStorageError(RuntimeError):
    """Raised when PostgreSQL cannot complete an assessment operation."""


@dataclass(frozen=True)
class AssessmentRecord:
    """One model decision and its progress toward the Sepolia write-back."""

    event_id: str
    chain_id: int
    contract_address: str
    claim_id: int
    model_version: str
    probability: float
    threshold: float
    fraud_score: int
    status: str
    reasons: tuple[FraudReason, ...]
    processing_status: str = "scored"
    transaction_hash: str | None = None
    block_number: int | None = None
    error: str | None = None

    @classmethod
    def from_score(
        cls,
        *,
        event_id: str,
        chain_id: int,
        contract_address: str,
        claim_id: int,
        score: FraudScore,
    ) -> "AssessmentRecord":
        return cls(
            event_id=event_id,
            chain_id=chain_id,
            contract_address=contract_address.lower(),
            claim_id=claim_id,
            model_version=score.model_version,
            probability=score.probability,
            threshold=score.threshold,
            fraud_score=score.score_basis_points,
            status="Flagged" if score.flagged else "UnderReview",
            reasons=score.reasons,
        )


TABLE_SQL = """
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
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS claim_assessments_contract_claim_idx
    ON claim_assessments (
        chain_id, contract_address, claim_id, updated_at DESC
    );
"""

DUPLICATE_TABLE_SQL = """
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
"""

DUPLICATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS claim_incident_fingerprint_match_idx
    ON claim_incident_fingerprints (
        chain_id,
        contract_address,
        fingerprint_version,
        incident_fingerprint
);
"""

FEATURE_TABLE_SQL = """
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
"""

FEATURE_HISTORY_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS claim_feature_snapshots_history_idx
    ON claim_feature_snapshots (
        chain_id,
        contract_address,
        insurer_id,
        policy_reference_fingerprint
    );
"""

SELECT_COLUMNS = """
event_id, chain_id, contract_address, claim_id, model_version, probability,
threshold, fraud_score, assessment_status, reasons, processing_status,
transaction_hash, block_number, error
"""

FEATURE_SELECT_COLUMNS = """
event_id, chain_id, contract_address, claim_id, feature_version, insurer_id,
policy_fingerprint_version, policy_reference_fingerprint, event_timestamp,
incident_date, claim_type, claim_amount_usd, policy_premium_usd,
claim_to_premium_ratio, vehicle_age, vehicle_type, country, region_type,
third_party_injury_flag, total_loss_flag, report_delay_days,
cross_insurer_duplicate_match_count, prior_policy_claim_count,
prior_insurer_claim_count, prior_insurer_average_claim_amount_usd,
claim_to_prior_insurer_average_ratio
"""


def _default_connect(database_url: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise PostgresConfigurationError(
            "Install integrations/postgres/requirements.txt to use PostgreSQL"
        ) from exc
    return psycopg.connect(database_url, row_factory=dict_row)


def _record_from_row(row: dict[str, Any] | None) -> AssessmentRecord | None:
    if row is None:
        return None
    raw_reasons = row["reasons"]
    if isinstance(raw_reasons, str):
        raw_reasons = json.loads(raw_reasons)
    reasons = tuple(
        FraudReason(
            feature=str(reason["feature"]),
            label=str(reason["label"]),
            contribution=float(reason["contribution"]),
        )
        for reason in raw_reasons
    )
    return AssessmentRecord(
        event_id=str(row["event_id"]),
        chain_id=int(row["chain_id"]),
        contract_address=str(row["contract_address"]),
        claim_id=int(row["claim_id"]),
        model_version=str(row["model_version"]),
        probability=float(row["probability"]),
        threshold=float(row["threshold"]),
        fraud_score=int(row["fraud_score"]),
        status=str(row["assessment_status"]),
        reasons=reasons,
        processing_status=str(row["processing_status"]),
        transaction_hash=row["transaction_hash"],
        block_number=(
            int(row["block_number"]) if row["block_number"] is not None else None
        ),
        error=row["error"],
    )


def _feature_snapshot_from_row(
    row: dict[str, Any] | None,
) -> ClaimFeatureSnapshot | None:
    if row is None:
        return None
    return ClaimFeatureSnapshot(
        event_id=str(row["event_id"]),
        chain_id=int(row["chain_id"]),
        contract_address=str(row["contract_address"]),
        claim_id=int(row["claim_id"]),
        feature_version=str(row["feature_version"]),
        insurer_id=str(row["insurer_id"]),
        policy_fingerprint_version=str(row["policy_fingerprint_version"]),
        policy_reference_fingerprint=str(row["policy_reference_fingerprint"]),
        event_timestamp=int(row["event_timestamp"]),
        incident_date=row["incident_date"],
        claim_type=str(row["claim_type"]),
        claim_amount_usd=float(row["claim_amount_usd"]),
        policy_premium_usd=float(row["policy_premium_usd"]),
        claim_to_premium_ratio=float(row["claim_to_premium_ratio"]),
        vehicle_age=int(row["vehicle_age"]),
        vehicle_type=str(row["vehicle_type"]),
        country=str(row["country"]),
        region_type=str(row["region_type"]),
        third_party_injury_flag=bool(row["third_party_injury_flag"]),
        total_loss_flag=bool(row["total_loss_flag"]),
        report_delay_days=int(row["report_delay_days"]),
        cross_insurer_duplicate_match_count=int(
            row["cross_insurer_duplicate_match_count"]
        ),
        prior_policy_claim_count=int(row["prior_policy_claim_count"]),
        prior_insurer_claim_count=int(row["prior_insurer_claim_count"]),
        prior_insurer_average_claim_amount_usd=(
            float(row["prior_insurer_average_claim_amount_usd"])
            if row["prior_insurer_average_claim_amount_usd"] is not None
            else None
        ),
        claim_to_prior_insurer_average_ratio=(
            float(row["claim_to_prior_insurer_average_ratio"])
            if row["claim_to_prior_insurer_average_ratio"] is not None
            else None
        ),
    )


class PostgresAssessmentRepository:
    """Keep SQL and connection handling behind a small persistence interface."""

    def __init__(
        self,
        database_url: str,
        *,
        connect: Callable[[str], Any] = _default_connect,
    ) -> None:
        if not database_url.strip():
            raise PostgresConfigurationError("DATABASE_URL cannot be empty")
        self.database_url = database_url
        self._connect = connect

    @contextmanager
    def _cursor(self):
        try:
            with self._connect(self.database_url) as connection:
                with connection.cursor() as cursor:
                    yield cursor
        except PostgresConfigurationError:
            raise
        except PostgresStorageError:
            raise
        except Exception as exc:
            raise PostgresStorageError(
                "PostgreSQL assessment storage is unavailable"
            ) from exc

    @classmethod
    def from_env(cls) -> "PostgresAssessmentRepository":
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise PostgresConfigurationError(
                "DATABASE_URL is required for assessment storage"
            )
        return cls(database_url)

    def ensure_schema(self) -> None:
        with self._cursor() as cursor:
            cursor.execute(TABLE_SQL)
            cursor.execute(INDEX_SQL)
            cursor.execute(DUPLICATE_TABLE_SQL)
            cursor.execute(DUPLICATE_INDEX_SQL)
            cursor.execute(FEATURE_TABLE_SQL)
            cursor.execute(FEATURE_HISTORY_INDEX_SQL)

    def get_by_event_id(self, event_id: str) -> AssessmentRecord | None:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT {SELECT_COLUMNS} FROM claim_assessments "
                "WHERE event_id = %s",
                (event_id,),
            )
            return _record_from_row(cursor.fetchone())

    def get_latest_for_claim(
        self,
        *,
        chain_id: int,
        contract_address: str,
        claim_id: int,
    ) -> AssessmentRecord | None:
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT {SELECT_COLUMNS} FROM claim_assessments "
                "WHERE chain_id = %s AND contract_address = %s "
                "AND claim_id = %s ORDER BY updated_at DESC LIMIT 1",
                (chain_id, contract_address.lower(), claim_id),
            )
            return _record_from_row(cursor.fetchone())

    def get_feature_snapshot(
        self,
        event_id: str,
    ) -> ClaimFeatureSnapshot | None:
        """Return the exact historical feature values stored for one event."""

        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT {FEATURE_SELECT_COLUMNS} "
                "FROM claim_feature_snapshots WHERE event_id = %s",
                (event_id,),
            )
            return _feature_snapshot_from_row(cursor.fetchone())

    def record_feature_snapshot(
        self,
        values: ClaimFeatureInput,
    ) -> ClaimFeatureSnapshot:
        """Atomically enrich and save an immutable, replay-safe snapshot."""

        normalized_contract = values.contract_address.lower()
        lock_identity = (
            f"claim-feature-history:{values.chain_id}:{normalized_contract}:"
            f"{values.insurer_id}"
        )
        with self._cursor() as cursor:
            # Counts and averages must describe a single processing order. Claims
            # for the same insurer serialize; unrelated insurers remain concurrent.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_identity,),
            )
            cursor.execute(
                f"""
                WITH input (
                    event_id, chain_id, contract_address, claim_id,
                    feature_version, insurer_id, policy_fingerprint_version,
                    policy_reference_fingerprint, event_timestamp, incident_date,
                    claim_type, claim_amount_usd, policy_premium_usd,
                    claim_to_premium_ratio, vehicle_age, vehicle_type, country,
                    region_type, third_party_injury_flag, total_loss_flag,
                    report_delay_days, cross_insurer_duplicate_match_count
                ) AS (
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                ),
                history AS (
                    SELECT
                        COUNT(*) FILTER (
                            WHERE existing.policy_reference_fingerprint =
                                  input.policy_reference_fingerprint
                        )::integer AS prior_policy_claim_count,
                        COUNT(*)::integer AS prior_insurer_claim_count,
                        AVG(existing.claim_amount_usd)::double precision
                            AS prior_insurer_average_claim_amount_usd
                    FROM claim_feature_snapshots AS existing
                    CROSS JOIN input
                    WHERE existing.chain_id = input.chain_id
                      AND existing.contract_address = input.contract_address
                      AND existing.insurer_id = input.insurer_id
                ),
                inserted AS (
                    INSERT INTO claim_feature_snapshots (
                        event_id, chain_id, contract_address, claim_id,
                        feature_version, insurer_id, policy_fingerprint_version,
                        policy_reference_fingerprint, event_timestamp,
                        incident_date, claim_type, claim_amount_usd,
                        policy_premium_usd, claim_to_premium_ratio, vehicle_age,
                        vehicle_type, country, region_type,
                        third_party_injury_flag, total_loss_flag,
                        report_delay_days,
                        cross_insurer_duplicate_match_count,
                        prior_policy_claim_count, prior_insurer_claim_count,
                        prior_insurer_average_claim_amount_usd,
                        claim_to_prior_insurer_average_ratio
                    )
                    SELECT
                        input.event_id, input.chain_id, input.contract_address,
                        input.claim_id, input.feature_version, input.insurer_id,
                        input.policy_fingerprint_version,
                        input.policy_reference_fingerprint,
                        input.event_timestamp, input.incident_date,
                        input.claim_type, input.claim_amount_usd,
                        input.policy_premium_usd,
                        input.claim_to_premium_ratio, input.vehicle_age,
                        input.vehicle_type, input.country, input.region_type,
                        input.third_party_injury_flag, input.total_loss_flag,
                        input.report_delay_days,
                        input.cross_insurer_duplicate_match_count,
                        history.prior_policy_claim_count,
                        history.prior_insurer_claim_count,
                        history.prior_insurer_average_claim_amount_usd,
                        CASE
                            WHEN history.prior_insurer_average_claim_amount_usd
                                 IS NULL
                            THEN NULL
                            ELSE input.claim_amount_usd /
                                 history.prior_insurer_average_claim_amount_usd
                        END
                    FROM input
                    CROSS JOIN history
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING {FEATURE_SELECT_COLUMNS}
                )
                SELECT {FEATURE_SELECT_COLUMNS} FROM inserted
                UNION ALL
                SELECT {FEATURE_SELECT_COLUMNS}
                FROM claim_feature_snapshots
                WHERE event_id = (SELECT event_id FROM input)
                LIMIT 1
                """,
                (
                    values.event_id,
                    values.chain_id,
                    normalized_contract,
                    values.claim_id,
                    values.feature_version,
                    values.insurer_id,
                    values.policy_fingerprint_version,
                    values.policy_reference_fingerprint,
                    values.event_timestamp,
                    values.incident_date,
                    values.claim_type,
                    values.claim_amount_usd,
                    values.policy_premium_usd,
                    values.claim_to_premium_ratio,
                    values.vehicle_age,
                    values.vehicle_type,
                    values.country,
                    values.region_type,
                    values.third_party_injury_flag,
                    values.total_loss_flag,
                    values.report_delay_days,
                    values.cross_insurer_duplicate_match_count,
                ),
            )
            snapshot = _feature_snapshot_from_row(cursor.fetchone())
            if snapshot is None:
                raise PostgresStorageError(
                    "PostgreSQL did not return the claim feature snapshot"
                )
            return snapshot

    def record_and_find_duplicates(
        self,
        *,
        event_id: str,
        chain_id: int,
        contract_address: str,
        claim_id: int,
        insurer_id: str,
        fingerprint_version: str,
        incident_fingerprint: str,
    ) -> DuplicateCheck:
        """Atomically record one fingerprint and return other-insurer matches."""

        normalized_contract = contract_address.lower()
        lock_identity = (
            f"{chain_id}:{normalized_contract}:{fingerprint_version}:"
            f"{incident_fingerprint}"
        )
        with self._cursor() as cursor:
            # Concurrent submissions with the same fingerprint must serialize so
            # at least the later transaction observes the earlier one.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_identity,),
            )
            cursor.execute(
                """
                INSERT INTO claim_incident_fingerprints (
                    chain_id, contract_address, claim_id, event_id, insurer_id,
                    fingerprint_version, incident_fingerprint
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (chain_id, contract_address, claim_id) DO UPDATE SET
                    event_id = EXCLUDED.event_id,
                    insurer_id = EXCLUDED.insurer_id,
                    fingerprint_version = EXCLUDED.fingerprint_version,
                    incident_fingerprint = EXCLUDED.incident_fingerprint,
                    updated_at = NOW()
                """,
                (
                    chain_id,
                    normalized_contract,
                    claim_id,
                    event_id,
                    insurer_id,
                    fingerprint_version,
                    incident_fingerprint,
                ),
            )
            cursor.execute(
                """
                SELECT claim_id, insurer_id
                FROM claim_incident_fingerprints
                WHERE chain_id = %s
                  AND contract_address = %s
                  AND fingerprint_version = %s
                  AND incident_fingerprint = %s
                  AND claim_id <> %s
                  AND insurer_id <> %s
                ORDER BY claim_id
                """,
                (
                    chain_id,
                    normalized_contract,
                    fingerprint_version,
                    incident_fingerprint,
                    claim_id,
                    insurer_id,
                ),
            )
            matches = tuple(
                DuplicateMatch(
                    claim_id=int(row["claim_id"]),
                    insurer_id=str(row["insurer_id"]),
                )
                for row in cursor.fetchall()
            )
        return DuplicateCheck(
            insurer_id=insurer_id,
            fingerprint_version=fingerprint_version,
            matches=matches,
        )

    def get_duplicate_check_for_claim(
        self,
        *,
        chain_id: int,
        contract_address: str,
        claim_id: int,
    ) -> DuplicateCheck | None:
        """Rebuild the current match result so earlier claims see later matches."""

        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT chain_id, contract_address, insurer_id,
                       fingerprint_version, incident_fingerprint
                FROM claim_incident_fingerprints
                WHERE chain_id = %s
                  AND contract_address = %s
                  AND claim_id = %s
                """,
                (chain_id, contract_address.lower(), claim_id),
            )
            current = cursor.fetchone()
            if current is None:
                return None

            cursor.execute(
                """
                SELECT claim_id, insurer_id
                FROM claim_incident_fingerprints
                WHERE chain_id = %s
                  AND contract_address = %s
                  AND fingerprint_version = %s
                  AND incident_fingerprint = %s
                  AND claim_id <> %s
                  AND insurer_id <> %s
                ORDER BY claim_id
                """,
                (
                    current["chain_id"],
                    current["contract_address"],
                    current["fingerprint_version"],
                    current["incident_fingerprint"],
                    claim_id,
                    current["insurer_id"],
                ),
            )
            matches = tuple(
                DuplicateMatch(
                    claim_id=int(row["claim_id"]),
                    insurer_id=str(row["insurer_id"]),
                )
                for row in cursor.fetchall()
            )
            return DuplicateCheck(
                insurer_id=str(current["insurer_id"]),
                fingerprint_version=str(current["fingerprint_version"]),
                matches=matches,
            )

    def save_scored(self, record: AssessmentRecord) -> None:
        reasons = json.dumps([asdict(reason) for reason in record.reasons])
        with self._cursor() as cursor:
            # A replay may update a failed or half-finished attempt, but a completed
            # record is immutable. This preserves the audit trail users already saw.
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
                    record.contract_address,
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
        with self._cursor() as cursor:
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
        with self._cursor() as cursor:
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
