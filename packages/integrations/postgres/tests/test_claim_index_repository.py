"""Unit tests for the replay-safe PostgreSQL blockchain projection."""

from datetime import UTC, datetime

import pytest

from packages.integrations.postgres import (
    ClaimIndexEventPage,
    ClaimIndexEventRecord,
    ClaimIndexOperationsSnapshot,
    ClaimIndexReconciliationRecord,
    ClaimIndexStatus,
    IndexedClaim,
    PostgresClaimIndexCheckpoint,
    PostgresClaimIndexRepository,
    PostgresDatabase,
    PostgresStorageError,
    claim_index_event_id,
)


class FakeCursor:
    def __init__(self, *, one=(), rows=(), update_rowcount=1):
        self.one = list(one)
        self.rows = list(rows)
        self.update_rowcount = update_rowcount
        self.rowcount = 1
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, parameters=None):
        self.executions.append((str(statement), parameters))
        self.rowcount = (
            self.update_rowcount if "UPDATE indexed_claims" in str(statement) else 1
        )

    def fetchone(self):
        return self.one.pop(0) if self.one else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self._cursor


def repository_for(cursor: FakeCursor) -> PostgresClaimIndexRepository:
    database = PostgresDatabase(
        "postgresql://test",
        connect=lambda _url: FakeConnection(cursor),
    )
    return PostgresClaimIndexRepository(database)


def submission_values() -> dict:
    return {
        "chain_id": 11_155_111,
        "contract_address": "0xABCDEF",
        "claim_id": 7,
        "claimant": "0xClaimant",
        "claim_hash": "0xAABB",
        "data_pointer": "ipfs://verified-claim",
        "block_number": 102,
        "block_hash": "0xBLOCK",
        "transaction_hash": "0xTRANSACTION",
        "log_index": 3,
        "event_timestamp": 1_750_000_000,
    }


def test_event_identity_is_stable_and_chain_scoped():
    assert (
        claim_index_event_id(
            chain_id=11_155_111,
            transaction_hash="0xABCDEF",
            log_index=4,
        )
        == "11155111:0xabcdef:4"
    )


def test_submission_is_audit_logged_and_upserted_without_regressing_newer_state():
    cursor = FakeCursor()
    repository = repository_for(cursor)

    repository.index_claim_submitted(**submission_values())

    assert len(cursor.executions) == 2
    event_statement, event_parameters = cursor.executions[0]
    upsert_statement, upsert_parameters = cursor.executions[1]
    assert "INSERT INTO claim_index_events" in event_statement
    assert "ON CONFLICT (event_id) DO NOTHING" in event_statement
    assert event_parameters[0] == "11155111:0xtransaction:3"
    assert event_parameters[2] == "0xabcdef"
    assert "ON CONFLICT (chain_id, contract_address, claim_id)" in upsert_statement
    assert "state_block_number" in upsert_statement
    assert upsert_parameters[1] == "0xabcdef"
    assert upsert_parameters[4] == "0xaabb"


def test_assessment_updates_only_when_its_chain_position_is_not_older():
    cursor = FakeCursor(update_rowcount=1)
    repository = repository_for(cursor)

    repository.index_claim_assessed(
        chain_id=11_155_111,
        contract_address="0xABCDEF",
        claim_id=7,
        status=4,
        fraud_score=8_500,
        block_number=110,
        block_hash="0xBLOCK2",
        transaction_hash="0xASSESSMENT",
        log_index=2,
        event_timestamp=1_750_000_100,
    )

    update_statement, update_parameters = cursor.executions[1]
    assert "(state_block_number, state_log_index) <= (%s, %s)" in update_statement
    assert update_parameters[:3] == (4, 8_500, 1_750_000_100)
    assert update_parameters[6:9] == (11_155_111, "0xabcdef", 7)


def test_assessment_without_submission_stops_checkpoint_progress():
    cursor = FakeCursor(update_rowcount=0)
    repository = repository_for(cursor)

    with pytest.raises(PostgresStorageError, match="deployment block"):
        repository.index_claim_assessed(
            chain_id=11_155_111,
            contract_address="0xABCDEF",
            claim_id=7,
            status=1,
            fraud_score=4_200,
            block_number=110,
            block_hash="0xBLOCK2",
            transaction_hash="0xASSESSMENT",
            log_index=2,
            event_timestamp=1_750_000_100,
        )

    assert "SELECT 1 FROM indexed_claims" in cursor.executions[-1][0]


def test_terminal_decision_updates_projection_and_confirms_matching_proposal():
    cursor = FakeCursor(
        one=(
            {
                "decision_hash": "0x" + "77" * 32,
                "confirmed_transaction_hash": None,
            },
        ),
        update_rowcount=1,
    )

    repository_for(cursor).index_claim_decided(
        chain_id=11_155_111,
        contract_address="0xABCDEF",
        claim_id=7,
        status=2,
        fraud_score=4_200,
        block_number=120,
        block_hash="0xBLOCK3",
        transaction_hash="0xDECISION",
        log_index=4,
        event_timestamp=1_750_000_200,
        decision_hash="0x" + "77" * 32,
    )

    assert "ClaimDecided" in cursor.executions[0][1]
    projection_statement, projection_parameters = cursor.executions[1]
    assert "UPDATE indexed_claims" in projection_statement
    assert projection_parameters[:3] == (2, 4_200, 1_750_000_200)
    assert "FOR UPDATE" in cursor.executions[2][0]
    confirmation_statement, confirmation_parameters = cursor.executions[3]
    assert "UPDATE coverage_decision_proposals" in confirmation_statement
    assert confirmation_parameters == (
        "0xdecision",
        1_750_000_200,
        11_155_111,
        "0xabcdef",
        7,
        "0x" + "77" * 32,
    )


def test_terminal_decision_with_a_different_hash_stops_checkpoint_progress():
    cursor = FakeCursor(
        one=(
            {
                "decision_hash": "0x" + "66" * 32,
                "confirmed_transaction_hash": None,
            },
        ),
        update_rowcount=1,
    )

    with pytest.raises(PostgresStorageError, match="does not match"):
        repository_for(cursor).index_claim_decided(
            chain_id=11_155_111,
            contract_address="0xABCDEF",
            claim_id=7,
            status=2,
            fraud_score=4_200,
            block_number=120,
            block_hash="0xBLOCK3",
            transaction_hash="0xDECISION",
            log_index=4,
            event_timestamp=1_750_000_200,
            decision_hash="0x" + "77" * 32,
        )


def test_claim_page_is_deployment_scoped_and_newest_first():
    cursor = FakeCursor(
        rows=(
            {
                "claim_id": 7,
                "claimant": "0xclaimant",
                "claim_hash": "0xhash",
                "data_pointer": "ipfs://claim",
                "status": 4,
                "fraud_score": 8_500,
                "submitted_at": 1_750_000_000,
                "updated_at": 1_750_000_100,
                "total_items": 12,
            },
        ),
    )
    repository = repository_for(cursor)

    claims, total = repository.list_claims(
        chain_id=11_155_111,
        contract_address="0xABCDEF",
        page=2,
        page_size=5,
    )

    assert total == 12
    assert claims == [
        IndexedClaim(
            claim_id=7,
            claimant="0xclaimant",
            claim_hash="0xhash",
            data_pointer="ipfs://claim",
            status=4,
            fraud_score=8_500,
            submitted_at=1_750_000_000,
            updated_at=1_750_000_100,
        )
    ]
    assert len(cursor.executions) == 1
    assert "LEFT JOIN page_rows" in cursor.executions[0][0]
    assert "ORDER BY claim_id DESC" in cursor.executions[0][0]
    assert cursor.executions[0][1] == (
        11_155_111,
        "0xabcdef",
        5,
        5,
        11_155_111,
        "0xabcdef",
    )


def test_empty_or_out_of_range_page_still_returns_the_total():
    cursor = FakeCursor(rows=({"claim_id": None, "total_items": 12},))

    claims, total = repository_for(cursor).list_claims(
        chain_id=11_155_111,
        contract_address="0xABCDEF",
        page=99,
        page_size=10,
    )

    assert claims == []
    assert total == 12


def test_database_checkpoint_is_shared_monotonic_progress():
    updated_at = datetime(2026, 8, 4, tzinfo=UTC)
    load_cursor = FakeCursor(
        one=(
            {
                "chain_id": 11_155_111,
                "contract_address": "0xabcdef",
                "last_processed_block": 120,
                "updated_at": updated_at,
            },
        )
    )
    repository = repository_for(load_cursor)
    checkpoint = PostgresClaimIndexCheckpoint(
        repository,
        chain_id=11_155_111,
        contract_address="0xABCDEF",
    )

    assert checkpoint.load(default=99) == 120

    save_cursor = FakeCursor()
    saving_repository = repository_for(save_cursor)
    PostgresClaimIndexCheckpoint(
        saving_repository,
        chain_id=11_155_111,
        contract_address="0xABCDEF",
    ).save(125)

    statement, parameters = save_cursor.executions[0]
    assert "claim_index_checkpoints.last_processed_block" in statement
    assert parameters == (11_155_111, "0xabcdef", 125)


def test_status_rejects_an_untyped_database_timestamp():
    cursor = FakeCursor(
        one=(
            {
                "chain_id": 11_155_111,
                "contract_address": "0xabcdef",
                "last_processed_block": 120,
                "updated_at": "not-a-datetime",
            },
        )
    )

    with pytest.raises(PostgresStorageError, match="invalid"):
        repository_for(cursor).get_status(
            chain_id=11_155_111,
            contract_address="0xABCDEF",
        )


def test_status_record_shape_is_explicit():
    status = ClaimIndexStatus(
        chain_id=11_155_111,
        contract_address="0xabcdef",
        last_processed_block=120,
        updated_at=datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert status.last_processed_block == 120


def test_operations_snapshot_is_bounded_and_deployment_scoped():
    checkpoint_at = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    reconciled_at = datetime(2026, 8, 5, 12, 5, tzinfo=UTC)
    event_indexed_at = datetime(2026, 8, 5, 12, 4, tzinfo=UTC)
    cursor = FakeCursor(
        one=(
            {
                "total_claims": 7,
                "submitted_claims": 2,
                "under_review_claims": 1,
                "approved_claims": 1,
                "rejected_claims": 1,
                "flagged_claims": 2,
                "total_events": 12,
                "submitted_events": 7,
                "assessed_events": 5,
                "checkpoint_chain_id": 11_155_111,
                "checkpoint_contract_address": "0xabcdef",
                "last_processed_block": 11_424_283,
                "checkpoint_updated_at": checkpoint_at,
                "reconciliation_indexed_through_block": 11_424_283,
                "reconciliation_chain_claims": 7,
                "reconciliation_indexed_claims": 7,
                "missing_claim_ids": [],
                "unexpected_claim_ids": [],
                "mismatched_claim_ids": [],
                "reconciliation_consistent": True,
                "reconciliation_duration_ms": 132,
                "reconciliation_checked_at": reconciled_at,
            },
        ),
        rows=(
            {
                "event_id": "11155111:0xtx:1",
                "claim_id": 6,
                "event_type": "ClaimAssessed",
                "block_number": 11_424_280,
                "transaction_hash": "0xtx",
                "log_index": 1,
                "event_timestamp": 1_754_395_200,
                "status": 4,
                "fraud_score": 8_500,
                "indexed_at": event_indexed_at,
            },
        ),
    )

    snapshot = repository_for(cursor).get_operations_snapshot(
        chain_id=11_155_111,
        contract_address="0xABCDEF",
        recent_event_limit=10,
    )

    assert snapshot == ClaimIndexOperationsSnapshot(
        checkpoint=ClaimIndexStatus(
            chain_id=11_155_111,
            contract_address="0xabcdef",
            last_processed_block=11_424_283,
            updated_at=checkpoint_at,
        ),
        total_claims=7,
        total_events=12,
        submitted_events=7,
        assessed_events=5,
        claim_status_counts=(2, 1, 1, 1, 2),
        recent_events=(
            ClaimIndexEventRecord(
                event_id="11155111:0xtx:1",
                claim_id=6,
                event_type="ClaimAssessed",
                block_number=11_424_280,
                transaction_hash="0xtx",
                log_index=1,
                event_timestamp=1_754_395_200,
                status=4,
                fraud_score=8_500,
                indexed_at=event_indexed_at,
            ),
        ),
        last_reconciliation=ClaimIndexReconciliationRecord(
            indexed_through_block=11_424_283,
            chain_claims=7,
            indexed_claims=7,
            missing_claim_ids=(),
            unexpected_claim_ids=(),
            mismatched_claim_ids=(),
            consistent=True,
            duration_ms=132,
            checked_at=reconciled_at,
        ),
    )
    assert len(cursor.executions) == 2
    assert "LIMIT %s" in cursor.executions[1][0]
    assert cursor.executions[1][1] == (11_155_111, "0xabcdef", 10)


def test_event_search_binds_filters_and_uses_a_stable_keyset_cursor():
    indexed_at = datetime(2026, 8, 5, 12, 4, tzinfo=UTC)

    def event_row(event_id: str, block_number: int, log_index: int) -> dict:
        return {
            "event_id": event_id,
            "claim_id": 6,
            "event_type": "ClaimAssessed",
            "block_number": block_number,
            "transaction_hash": "0xabcdef",
            "log_index": log_index,
            "event_timestamp": 1_754_395_200,
            "status": 4,
            "fraud_score": 8_500,
            "indexed_at": indexed_at,
        }

    cursor = FakeCursor(
        rows=(
            event_row("event-3", 200, 3),
            event_row("event-2", 199, 2),
            event_row("event-1", 198, 1),
        )
    )
    page = repository_for(cursor).search_events(
        chain_id=11_155_111,
        contract_address="0xABCDEF",
        claim_id=6,
        transaction_hash="0xABCDEF",
        event_type="ClaimAssessed",
        status=4,
        from_block=100,
        to_block=200,
        before=(201, 4, "event-4"),
        limit=2,
    )

    assert page == ClaimIndexEventPage(
        events=(
            ClaimIndexEventRecord(
                event_id="event-3",
                claim_id=6,
                event_type="ClaimAssessed",
                block_number=200,
                transaction_hash="0xabcdef",
                log_index=3,
                event_timestamp=1_754_395_200,
                status=4,
                fraud_score=8_500,
                indexed_at=indexed_at,
            ),
            ClaimIndexEventRecord(
                event_id="event-2",
                claim_id=6,
                event_type="ClaimAssessed",
                block_number=199,
                transaction_hash="0xabcdef",
                log_index=2,
                event_timestamp=1_754_395_200,
                status=4,
                fraud_score=8_500,
                indexed_at=indexed_at,
            ),
        ),
        has_more=True,
    )
    statement, parameters = cursor.executions[0]
    assert "claim_id = %s" in statement
    assert "transaction_hash = %s" in statement
    assert "event_type = %s" in statement
    assert "status = %s" in statement
    assert "block_number >= %s" in statement
    assert "block_number <= %s" in statement
    assert "(block_number, log_index, event_id) < (%s, %s, %s)" in statement
    assert "ORDER BY block_number DESC, log_index DESC, event_id DESC" in statement
    assert parameters == (
        11_155_111,
        "0xabcdef",
        6,
        "0xabcdef",
        "ClaimAssessed",
        4,
        100,
        200,
        201,
        4,
        "event-4",
        3,
    )


def test_reconciliation_audit_append_does_not_update_projection():
    cursor = FakeCursor()

    repository_for(cursor).record_reconciliation(
        chain_id=11_155_111,
        contract_address="0xABCDEF",
        indexed_through_block=120,
        chain_claims=7,
        indexed_claims=6,
        missing_claim_ids=(4,),
        unexpected_claim_ids=(),
        mismatched_claim_ids=(5,),
        consistent=False,
        duration_ms=25,
    )

    statement, parameters = cursor.executions[0]
    assert "INSERT INTO claim_index_reconciliations" in statement
    assert "UPDATE indexed_claims" not in statement
    assert parameters == (
        11_155_111,
        "0xabcdef",
        120,
        7,
        6,
        [4],
        [],
        [5],
        False,
        25,
    )
