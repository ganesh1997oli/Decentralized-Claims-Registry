"""PostgreSQL adapter for privacy-preserving cross-insurer matching."""

from __future__ import annotations

from packages.duplicates import DuplicateCheck, DuplicateMatch
from packages.integrations.postgres.database import PostgresDatabase


class PostgresDuplicateRepository:
    """Atomically store incident fingerprints and rebuild current matches."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

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
        """Record one fingerprint and return other-insurer matches atomically."""

        normalized_contract = contract_address.lower()
        lock_identity = (
            f"{chain_id}:{normalized_contract}:{fingerprint_version}:"
            f"{incident_fingerprint}"
        )
        with self.database.cursor() as cursor:
            # Equal fingerprints share an advisory transaction lock. Concurrent
            # submissions therefore have a definite order and at least the later
            # transaction observes the earlier one.
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
        """Rebuild the result so an earlier claim can see later matches."""

        with self.database.cursor() as cursor:
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
