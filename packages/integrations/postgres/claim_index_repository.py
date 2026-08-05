"""PostgreSQL projection of confirmed ClaimsRegistry events.

The blockchain remains the source of truth. This repository owns a disposable
read model that can be rebuilt by replaying logs from the deployment block. All
writes are idempotent because the listener deliberately retries a block range
when RPC, IPFS, Kafka, PostgreSQL, or checkpoint persistence fails.
"""

from __future__ import annotations

from datetime import datetime

from packages.integrations.postgres.database import (
    PostgresDatabase,
    PostgresStorageError,
)
from packages.integrations.postgres.records import (
    ClaimIndexEventRecord,
    ClaimIndexOperationsSnapshot,
    ClaimIndexReconciliationRecord,
    ClaimIndexStatus,
    IndexedClaim,
)

CLAIM_SELECT_COLUMNS = """
claim_id, claimant, claim_hash, data_pointer, status, fraud_score,
submitted_at, updated_at
"""


def claim_index_event_id(
    *, chain_id: int, transaction_hash: str, log_index: int
) -> str:
    """Return the stable identity of one Ethereum log.

    Transaction hash plus log index identifies a log on one chain. Including
    the chain ID prevents a testnet transaction from colliding with an event on
    another network if this database later serves multiple deployments.
    """

    return f"{chain_id}:{transaction_hash.lower()}:{log_index}"


def _claim_from_row(row) -> IndexedClaim:
    return IndexedClaim(
        claim_id=int(row["claim_id"]),
        claimant=str(row["claimant"]),
        claim_hash=str(row["claim_hash"]),
        data_pointer=str(row["data_pointer"]),
        status=int(row["status"]),
        fraud_score=int(row["fraud_score"]),
        submitted_at=int(row["submitted_at"]),
        updated_at=int(row["updated_at"]),
    )


def _event_from_row(row) -> ClaimIndexEventRecord:
    indexed_at = row["indexed_at"]
    if not isinstance(indexed_at, datetime):
        raise PostgresStorageError(
            "PostgreSQL returned an invalid claim index event timestamp"
        )
    return ClaimIndexEventRecord(
        event_id=str(row["event_id"]),
        claim_id=int(row["claim_id"]),
        event_type=str(row["event_type"]),
        block_number=int(row["block_number"]),
        transaction_hash=str(row["transaction_hash"]),
        log_index=int(row["log_index"]),
        event_timestamp=int(row["event_timestamp"]),
        status=int(row["status"]),
        fraud_score=int(row["fraud_score"]),
        indexed_at=indexed_at,
    )


def _reconciliation_from_row(row) -> ClaimIndexReconciliationRecord:
    checked_at = row["reconciliation_checked_at"]
    if not isinstance(checked_at, datetime):
        raise PostgresStorageError(
            "PostgreSQL returned an invalid reconciliation timestamp"
        )
    return ClaimIndexReconciliationRecord(
        indexed_through_block=int(row["reconciliation_indexed_through_block"]),
        chain_claims=int(row["reconciliation_chain_claims"]),
        indexed_claims=int(row["reconciliation_indexed_claims"]),
        missing_claim_ids=tuple(int(item) for item in row["missing_claim_ids"]),
        unexpected_claim_ids=tuple(
            int(item) for item in row["unexpected_claim_ids"]
        ),
        mismatched_claim_ids=tuple(
            int(item) for item in row["mismatched_claim_ids"]
        ),
        consistent=bool(row["reconciliation_consistent"]),
        duration_ms=int(row["reconciliation_duration_ms"]),
        checked_at=checked_at,
    )


class PostgresClaimIndexRepository:
    """Persist and query the deployment-scoped public claims projection."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    @staticmethod
    def _record_event(
        cursor,
        *,
        event_id: str,
        chain_id: int,
        contract_address: str,
        claim_id: int,
        event_type: str,
        block_number: int,
        block_hash: str,
        transaction_hash: str,
        log_index: int,
        event_timestamp: int,
        status: int,
        fraud_score: int,
    ) -> None:
        """Append an immutable audit event, tolerating an exact listener replay."""

        cursor.execute(
            """
            INSERT INTO claim_index_events (
                event_id, chain_id, contract_address, claim_id, event_type,
                block_number, block_hash, transaction_hash, log_index,
                event_timestamp, status, fraud_score
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                event_id,
                chain_id,
                contract_address,
                claim_id,
                event_type,
                block_number,
                block_hash,
                transaction_hash,
                log_index,
                event_timestamp,
                status,
                fraud_score,
            ),
        )

    def index_claim_submitted(
        self,
        *,
        chain_id: int,
        contract_address: str,
        claim_id: int,
        claimant: str,
        claim_hash: str,
        data_pointer: str,
        block_number: int,
        block_hash: str,
        transaction_hash: str,
        log_index: int,
        event_timestamp: int,
    ) -> None:
        """Create the initial claim projection and immutable history entry.

        The upsert handles a crash after committing this transaction but before
        the block checkpoint is advanced. Position comparisons make an older
        replay unable to overwrite a later assessment already present in the
        projection.
        """

        normalized_contract = contract_address.lower()
        normalized_transaction = transaction_hash.lower()
        event_id = claim_index_event_id(
            chain_id=chain_id,
            transaction_hash=normalized_transaction,
            log_index=log_index,
        )
        with self.database.cursor() as cursor:
            self._record_event(
                cursor,
                event_id=event_id,
                chain_id=chain_id,
                contract_address=normalized_contract,
                claim_id=claim_id,
                event_type="ClaimSubmitted",
                block_number=block_number,
                block_hash=block_hash.lower(),
                transaction_hash=normalized_transaction,
                log_index=log_index,
                event_timestamp=event_timestamp,
                status=0,
                fraud_score=0,
            )
            cursor.execute(
                """
                INSERT INTO indexed_claims (
                    chain_id, contract_address, claim_id, claimant, claim_hash,
                    data_pointer, status, fraud_score, submitted_at, updated_at,
                    submission_block_number, submission_transaction_hash,
                    state_block_number, state_log_index, state_event_id
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, 0, 0, %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (chain_id, contract_address, claim_id) DO UPDATE SET
                    claimant = EXCLUDED.claimant,
                    claim_hash = EXCLUDED.claim_hash,
                    data_pointer = EXCLUDED.data_pointer,
                    submitted_at = EXCLUDED.submitted_at,
                    submission_block_number = EXCLUDED.submission_block_number,
                    submission_transaction_hash = EXCLUDED.submission_transaction_hash,
                    status = CASE
                        WHEN (
                            EXCLUDED.state_block_number, EXCLUDED.state_log_index
                        ) >= (
                            indexed_claims.state_block_number,
                            indexed_claims.state_log_index
                        ) THEN EXCLUDED.status
                        ELSE indexed_claims.status
                    END,
                    fraud_score = CASE
                        WHEN (
                            EXCLUDED.state_block_number, EXCLUDED.state_log_index
                        ) >= (
                            indexed_claims.state_block_number,
                            indexed_claims.state_log_index
                        ) THEN EXCLUDED.fraud_score
                        ELSE indexed_claims.fraud_score
                    END,
                    updated_at = CASE
                        WHEN (
                            EXCLUDED.state_block_number, EXCLUDED.state_log_index
                        ) >= (
                            indexed_claims.state_block_number,
                            indexed_claims.state_log_index
                        ) THEN EXCLUDED.updated_at
                        ELSE indexed_claims.updated_at
                    END,
                    state_block_number = GREATEST(
                        indexed_claims.state_block_number,
                        EXCLUDED.state_block_number
                    ),
                    state_log_index = CASE
                        WHEN EXCLUDED.state_block_number
                            > indexed_claims.state_block_number
                        THEN EXCLUDED.state_log_index
                        WHEN EXCLUDED.state_block_number
                            = indexed_claims.state_block_number
                        THEN GREATEST(
                            indexed_claims.state_log_index,
                            EXCLUDED.state_log_index
                        )
                        ELSE indexed_claims.state_log_index
                    END,
                    state_event_id = CASE
                        WHEN (
                            EXCLUDED.state_block_number, EXCLUDED.state_log_index
                        ) >= (
                            indexed_claims.state_block_number,
                            indexed_claims.state_log_index
                        ) THEN EXCLUDED.state_event_id
                        ELSE indexed_claims.state_event_id
                    END,
                    indexed_at = NOW()
                """,
                (
                    chain_id,
                    normalized_contract,
                    claim_id,
                    claimant,
                    claim_hash.lower(),
                    data_pointer,
                    event_timestamp,
                    event_timestamp,
                    block_number,
                    normalized_transaction,
                    block_number,
                    log_index,
                    event_id,
                ),
            )

    def index_claim_assessed(
        self,
        *,
        chain_id: int,
        contract_address: str,
        claim_id: int,
        status: int,
        fraud_score: int,
        block_number: int,
        block_hash: str,
        transaction_hash: str,
        log_index: int,
        event_timestamp: int,
    ) -> None:
        """Apply a lifecycle event without allowing an older replay to regress it."""

        normalized_contract = contract_address.lower()
        normalized_transaction = transaction_hash.lower()
        event_id = claim_index_event_id(
            chain_id=chain_id,
            transaction_hash=normalized_transaction,
            log_index=log_index,
        )
        with self.database.cursor() as cursor:
            self._record_event(
                cursor,
                event_id=event_id,
                chain_id=chain_id,
                contract_address=normalized_contract,
                claim_id=claim_id,
                event_type="ClaimAssessed",
                block_number=block_number,
                block_hash=block_hash.lower(),
                transaction_hash=normalized_transaction,
                log_index=log_index,
                event_timestamp=event_timestamp,
                status=status,
                fraud_score=fraud_score,
            )
            cursor.execute(
                """
                UPDATE indexed_claims
                SET status = %s,
                    fraud_score = %s,
                    updated_at = %s,
                    state_block_number = %s,
                    state_log_index = %s,
                    state_event_id = %s,
                    indexed_at = NOW()
                WHERE chain_id = %s
                  AND contract_address = %s
                  AND claim_id = %s
                  AND (state_block_number, state_log_index) <= (%s, %s)
                """,
                (
                    status,
                    fraud_score,
                    event_timestamp,
                    block_number,
                    log_index,
                    event_id,
                    chain_id,
                    normalized_contract,
                    claim_id,
                    block_number,
                    log_index,
                ),
            )
            if cursor.rowcount:
                return

            # A zero row count can mean either a safe replay of an older event or
            # a missing submission. Only the latter indicates an incomplete
            # backfill and must stop checkpoint advancement.
            cursor.execute(
                """
                SELECT 1 FROM indexed_claims
                WHERE chain_id = %s AND contract_address = %s AND claim_id = %s
                """,
                (chain_id, normalized_contract, claim_id),
            )
            if cursor.fetchone() is None:
                raise PostgresStorageError(
                    "Claim index is missing an earlier ClaimSubmitted event; "
                    "rebuild from the deployment block"
                )

    def list_claims(
        self,
        *,
        chain_id: int,
        contract_address: str,
        page: int,
        page_size: int,
    ) -> tuple[list[IndexedClaim], int]:
        """Read one newest-first page from the indexed contract projection."""

        normalized_contract = contract_address.lower()
        offset = (page - 1) * page_size
        with self.database.cursor() as cursor:
            # Count and page are deliberately returned by one SQL statement.
            # PostgreSQL therefore evaluates both against one statement snapshot
            # even while the listener inserts another confirmed claim.
            cursor.execute(
                """
                WITH page_rows AS (
                    SELECT
                        claim_id, claimant, claim_hash, data_pointer, status,
                        fraud_score, submitted_at, updated_at
                    FROM indexed_claims
                    WHERE chain_id = %s AND contract_address = %s
                    ORDER BY claim_id DESC
                    LIMIT %s OFFSET %s
                ),
                totals AS (
                    SELECT COUNT(*) AS total_items
                    FROM indexed_claims
                    WHERE chain_id = %s AND contract_address = %s
                )
                SELECT
                    page_rows.claim_id,
                    page_rows.claimant,
                    page_rows.claim_hash,
                    page_rows.data_pointer,
                    page_rows.status,
                    page_rows.fraud_score,
                    page_rows.submitted_at,
                    page_rows.updated_at,
                    totals.total_items
                FROM totals
                LEFT JOIN page_rows ON TRUE
                ORDER BY page_rows.claim_id DESC
                """,
                (
                    chain_id,
                    normalized_contract,
                    page_size,
                    offset,
                    chain_id,
                    normalized_contract,
                ),
            )
            rows = cursor.fetchall()
            total_items = int(rows[0]["total_items"]) if rows else 0
            claims = [
                _claim_from_row(row)
                for row in rows
                if row["claim_id"] is not None
            ]
            return claims, total_items

    def get_claim(
        self, *, chain_id: int, contract_address: str, claim_id: int
    ) -> IndexedClaim | None:
        """Return one projected claim for reconciliation and focused reads."""

        with self.database.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {CLAIM_SELECT_COLUMNS}
                FROM indexed_claims
                WHERE chain_id = %s AND contract_address = %s AND claim_id = %s
                """,
                (chain_id, contract_address.lower(), claim_id),
            )
            row = cursor.fetchone()
            return _claim_from_row(row) if row is not None else None

    def get_status(
        self, *, chain_id: int, contract_address: str
    ) -> ClaimIndexStatus | None:
        """Return the checkpoint visible to API instances and operators."""

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                SELECT chain_id, contract_address, last_processed_block, updated_at
                FROM claim_index_checkpoints
                WHERE chain_id = %s AND contract_address = %s
                """,
                (chain_id, contract_address.lower()),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            updated_at = row["updated_at"]
            if not isinstance(updated_at, datetime):
                raise PostgresStorageError(
                    "PostgreSQL returned an invalid claim index checkpoint"
                )
            return ClaimIndexStatus(
                chain_id=int(row["chain_id"]),
                contract_address=str(row["contract_address"]),
                last_processed_block=int(row["last_processed_block"]),
                updated_at=updated_at,
            )

    def get_operations_snapshot(
        self,
        *,
        chain_id: int,
        contract_address: str,
        recent_event_limit: int = 20,
    ) -> ClaimIndexOperationsSnapshot:
        """Return bounded operational telemetry for one deployment.

        Totals, checkpoint and last reconciliation come from one SQL statement,
        so the headline cards cannot disagree with each other while the listener
        commits another block. Recent events are deliberately bounded and read
        in the same transaction; the dashboard is an observability surface, not
        an unbounded event export API.
        """

        if not 1 <= recent_event_limit <= 100:
            raise ValueError("recent_event_limit must be between 1 and 100")
        normalized_contract = contract_address.lower()
        with self.database.cursor() as cursor:
            cursor.execute(
                """
                WITH claim_counts AS (
                    SELECT
                        COUNT(*) AS total_claims,
                        COUNT(*) FILTER (WHERE status = 0) AS submitted_claims,
                        COUNT(*) FILTER (WHERE status = 1) AS under_review_claims,
                        COUNT(*) FILTER (WHERE status = 2) AS approved_claims,
                        COUNT(*) FILTER (WHERE status = 3) AS rejected_claims,
                        COUNT(*) FILTER (WHERE status = 4) AS flagged_claims
                    FROM indexed_claims
                    WHERE chain_id = %s AND contract_address = %s
                ),
                event_counts AS (
                    SELECT
                        COUNT(*) AS total_events,
                        COUNT(*) FILTER (
                            WHERE event_type = 'ClaimSubmitted'
                        ) AS submitted_events,
                        COUNT(*) FILTER (
                            WHERE event_type = 'ClaimAssessed'
                        ) AS assessed_events
                    FROM claim_index_events
                    WHERE chain_id = %s AND contract_address = %s
                ),
                checkpoint AS (
                    SELECT chain_id, contract_address, last_processed_block,
                           updated_at
                    FROM claim_index_checkpoints
                    WHERE chain_id = %s AND contract_address = %s
                ),
                reconciliation AS (
                    SELECT
                        indexed_through_block, chain_claims, indexed_claims,
                        missing_claim_ids, unexpected_claim_ids,
                        mismatched_claim_ids, consistent, duration_ms, checked_at
                    FROM claim_index_reconciliations
                    WHERE chain_id = %s AND contract_address = %s
                    ORDER BY checked_at DESC, reconciliation_id DESC
                    LIMIT 1
                )
                SELECT
                    claim_counts.*,
                    event_counts.*,
                    checkpoint.chain_id AS checkpoint_chain_id,
                    checkpoint.contract_address AS checkpoint_contract_address,
                    checkpoint.last_processed_block,
                    checkpoint.updated_at AS checkpoint_updated_at,
                    reconciliation.indexed_through_block
                        AS reconciliation_indexed_through_block,
                    reconciliation.chain_claims AS reconciliation_chain_claims,
                    reconciliation.indexed_claims AS reconciliation_indexed_claims,
                    reconciliation.missing_claim_ids,
                    reconciliation.unexpected_claim_ids,
                    reconciliation.mismatched_claim_ids,
                    reconciliation.consistent AS reconciliation_consistent,
                    reconciliation.duration_ms AS reconciliation_duration_ms,
                    reconciliation.checked_at AS reconciliation_checked_at
                FROM claim_counts
                CROSS JOIN event_counts
                LEFT JOIN checkpoint ON TRUE
                LEFT JOIN reconciliation ON TRUE
                """,
                (
                    chain_id,
                    normalized_contract,
                    chain_id,
                    normalized_contract,
                    chain_id,
                    normalized_contract,
                    chain_id,
                    normalized_contract,
                ),
            )
            summary = cursor.fetchone()
            if summary is None:
                raise PostgresStorageError(
                    "PostgreSQL returned no claim index operations snapshot"
                )

            cursor.execute(
                """
                SELECT
                    event_id, claim_id, event_type, block_number,
                    transaction_hash, log_index, event_timestamp,
                    status, fraud_score, indexed_at
                FROM claim_index_events
                WHERE chain_id = %s AND contract_address = %s
                ORDER BY block_number DESC, log_index DESC
                LIMIT %s
                """,
                (chain_id, normalized_contract, recent_event_limit),
            )
            recent_events = tuple(
                _event_from_row(row) for row in cursor.fetchall()
            )

        checkpoint = None
        if summary["last_processed_block"] is not None:
            checkpoint_updated_at = summary["checkpoint_updated_at"]
            if not isinstance(checkpoint_updated_at, datetime):
                raise PostgresStorageError(
                    "PostgreSQL returned an invalid claim index checkpoint"
                )
            checkpoint = ClaimIndexStatus(
                chain_id=int(summary["checkpoint_chain_id"]),
                contract_address=str(summary["checkpoint_contract_address"]),
                last_processed_block=int(summary["last_processed_block"]),
                updated_at=checkpoint_updated_at,
            )

        reconciliation = None
        if summary["reconciliation_checked_at"] is not None:
            reconciliation = _reconciliation_from_row(summary)

        return ClaimIndexOperationsSnapshot(
            checkpoint=checkpoint,
            total_claims=int(summary["total_claims"]),
            total_events=int(summary["total_events"]),
            submitted_events=int(summary["submitted_events"]),
            assessed_events=int(summary["assessed_events"]),
            claim_status_counts=(
                int(summary["submitted_claims"]),
                int(summary["under_review_claims"]),
                int(summary["approved_claims"]),
                int(summary["rejected_claims"]),
                int(summary["flagged_claims"]),
            ),
            recent_events=recent_events,
            last_reconciliation=reconciliation,
        )

    def record_reconciliation(
        self,
        *,
        chain_id: int,
        contract_address: str,
        indexed_through_block: int,
        chain_claims: int,
        indexed_claims: int,
        missing_claim_ids: tuple[int, ...],
        unexpected_claim_ids: tuple[int, ...],
        mismatched_claim_ids: tuple[int, ...],
        consistent: bool,
        duration_ms: int,
    ) -> None:
        """Append one reconciliation audit result without changing projection."""

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO claim_index_reconciliations (
                    chain_id, contract_address, indexed_through_block,
                    chain_claims, indexed_claims, missing_claim_ids,
                    unexpected_claim_ids, mismatched_claim_ids, consistent,
                    duration_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    chain_id,
                    contract_address.lower(),
                    indexed_through_block,
                    chain_claims,
                    indexed_claims,
                    list(missing_claim_ids),
                    list(unexpected_claim_ids),
                    list(mismatched_claim_ids),
                    consistent,
                    duration_ms,
                ),
            )

    def load_checkpoint(
        self, *, chain_id: int, contract_address: str, default: int
    ) -> int:
        """Load durable progress, using the configured deployment start once."""

        status = self.get_status(
            chain_id=chain_id,
            contract_address=contract_address,
        )
        return status.last_processed_block if status is not None else default

    def save_checkpoint(
        self, *, chain_id: int, contract_address: str, block_number: int
    ) -> None:
        """Advance progress monotonically after the entire block range succeeds."""

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO claim_index_checkpoints (
                    chain_id, contract_address, last_processed_block
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (chain_id, contract_address) DO UPDATE SET
                    last_processed_block = EXCLUDED.last_processed_block,
                    updated_at = NOW()
                WHERE claim_index_checkpoints.last_processed_block
                    <= EXCLUDED.last_processed_block
                """,
                (chain_id, contract_address.lower(), block_number),
            )


class PostgresClaimIndexCheckpoint:
    """Adapt the repository checkpoint to ``ConfirmedBlockPoller``'s interface."""

    def __init__(
        self,
        repository: PostgresClaimIndexRepository,
        *,
        chain_id: int,
        contract_address: str,
    ) -> None:
        self.repository = repository
        self.chain_id = chain_id
        self.contract_address = contract_address

    def load(self, *, default: int) -> int:
        return self.repository.load_checkpoint(
            chain_id=self.chain_id,
            contract_address=self.contract_address,
            default=default,
        )

    def save(self, block_number: int) -> None:
        self.repository.save_checkpoint(
            chain_id=self.chain_id,
            contract_address=self.contract_address,
            block_number=block_number,
        )
