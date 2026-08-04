"""Unit tests for the replay-safe PostgreSQL blockchain projection."""

from datetime import UTC, datetime

import pytest

from packages.integrations.postgres import (
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
            self.update_rowcount
            if "UPDATE indexed_claims" in str(statement)
            else 1
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
    assert claim_index_event_id(
        chain_id=11_155_111,
        transaction_hash="0xABCDEF",
        log_index=4,
    ) == "11155111:0xabcdef:4"


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
