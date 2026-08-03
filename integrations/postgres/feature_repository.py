"""PostgreSQL adapter for immutable, versioned claim feature snapshots."""

from __future__ import annotations

from typing import Any

from integrations.postgres.database import PostgresDatabase, PostgresStorageError
from integrations.postgres.feature_processor import (
    ClaimFeatureInput,
    ClaimFeatureSnapshot,
)

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


def feature_snapshot_from_row(
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


class PostgresFeatureRepository:
    """Create replay-safe snapshots using history from one atomic order."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def get_feature_snapshot(self, event_id: str) -> ClaimFeatureSnapshot | None:
        with self.database.cursor() as cursor:
            cursor.execute(
                f"SELECT {FEATURE_SELECT_COLUMNS} "
                "FROM claim_feature_snapshots WHERE event_id = %s",
                (event_id,),
            )
            return feature_snapshot_from_row(cursor.fetchone())

    def record_feature_snapshot(
        self,
        values: ClaimFeatureInput,
    ) -> ClaimFeatureSnapshot:
        """Atomically enrich and save an immutable historical snapshot."""

        normalized_contract = values.contract_address.lower()
        lock_identity = (
            f"claim-feature-history:{values.chain_id}:{normalized_contract}:"
            f"{values.insurer_id}"
        )
        with self.database.cursor() as cursor:
            # Historical counts must describe a single order. Claims for one
            # insurer serialize, while unrelated insurers remain concurrent.
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
            snapshot = feature_snapshot_from_row(cursor.fetchone())
            if snapshot is None:
                raise PostgresStorageError(
                    "PostgreSQL did not return the claim feature snapshot"
                )
            return snapshot
