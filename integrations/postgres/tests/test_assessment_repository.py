import pytest

from integrations.postgres import (
    AssessmentRecord,
    PostgresAssessmentRepository,
    PostgresStorageError,
)
from model.scorer import FraudReason


class FakeCursor:
    def __init__(self, row=None):
        self.row = row
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, parameters=None):
        self.executions.append((statement, parameters))

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self._cursor


class FakeConnect:
    def __init__(self, row=None):
        self.cursor = FakeCursor(row)
        self.urls = []

    def __call__(self, database_url):
        self.urls.append(database_url)
        return FakeConnection(self.cursor)


def assessment_record() -> AssessmentRecord:
    return AssessmentRecord(
        event_id="11155111:0xtransaction:0",
        chain_id=11_155_111,
        contract_address="0xcontract",
        claim_id=7,
        model_version="african-motor-xgboost-v1",
        probability=0.68,
        threshold=0.47,
        fraud_score=6800,
        status="Flagged",
        reasons=(FraudReason("claim_amount_usd", "Claim amount", 0.42),),
    )


def test_repository_creates_table_and_index_as_separate_statements():
    connect = FakeConnect()
    repository = PostgresAssessmentRepository(
        "postgresql://test",
        connect=connect,
    )

    repository.ensure_schema()

    assert len(connect.cursor.executions) == 2
    assert "CREATE TABLE IF NOT EXISTS claim_assessments" in (
        connect.cursor.executions[0][0]
    )
    assert "CREATE INDEX IF NOT EXISTS" in connect.cursor.executions[1][0]


def test_repository_saves_reasons_as_bound_json():
    connect = FakeConnect()
    repository = PostgresAssessmentRepository(
        "postgresql://test",
        connect=connect,
    )

    repository.save_scored(assessment_record())

    statement, parameters = connect.cursor.executions[0]
    assert "ON CONFLICT (event_id)" in statement
    assert parameters[0] == "11155111:0xtransaction:0"
    assert '"label": "Claim amount"' in parameters[-1]


def test_repository_rebuilds_a_typed_record_from_postgres_row():
    row = {
        "event_id": "11155111:0xtransaction:0",
        "chain_id": 11_155_111,
        "contract_address": "0xcontract",
        "claim_id": 7,
        "model_version": "african-motor-xgboost-v1",
        "probability": 0.68,
        "threshold": 0.47,
        "fraud_score": 6800,
        "assessment_status": "Flagged",
        "reasons": [
            {
                "feature": "claim_amount_usd",
                "label": "Claim amount",
                "contribution": 0.42,
            }
        ],
        "processing_status": "completed",
        "transaction_hash": "0xassessment",
        "block_number": 101,
        "error": None,
    }
    connect = FakeConnect(row)
    repository = PostgresAssessmentRepository(
        "postgresql://test",
        connect=connect,
    )

    record = repository.get_by_event_id("11155111:0xtransaction:0")

    assert record == AssessmentRecord(
        **{
            **assessment_record().__dict__,
            "processing_status": "completed",
            "transaction_hash": "0xassessment",
            "block_number": 101,
        }
    )


def test_repository_hides_driver_errors_behind_its_interface():
    def fail_to_connect(_database_url):
        raise OSError("database is offline")

    repository = PostgresAssessmentRepository(
        "postgresql://test",
        connect=fail_to_connect,
    )

    with pytest.raises(PostgresStorageError, match="unavailable"):
        repository.ensure_schema()
