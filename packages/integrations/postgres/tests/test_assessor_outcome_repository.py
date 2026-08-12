from datetime import UTC, datetime
from uuid import UUID

from packages.integrations.postgres import (
    AssessorOutcomeRecord,
    PostgresAssessorOutcomeRepository,
    PostgresDatabase,
)


class FakeCursor:
    """Minimal driver-shaped cursor that records the repository interface."""

    def __init__(self, row=None):
        self.row = row
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, parameters=None):
        self.executions.append((str(statement), parameters))

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


def outcome_row():
    return {
        "outcome_id": UUID("11111111-1111-4111-8111-111111111111"),
        "chain_id": 11_155_111,
        "contract_address": "0xcontract",
        "claim_id": 7,
        "revision": 2,
        "outcome": "Inconclusive",
        "assessor_reference": "research-assessor-1",
        "notes": "Evidence does not support a binary conclusion.",
        "assessed_at": datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
    }


def repository_for(cursor):
    database = PostgresDatabase(
        "postgresql://test",
        connect=lambda _url: FakeConnection(cursor),
    )
    return PostgresAssessorOutcomeRepository(database)


def test_latest_human_outcome_is_scoped_and_revision_ordered():
    cursor = FakeCursor(outcome_row())

    result = repository_for(cursor).get_latest_for_claim(
        chain_id=11_155_111,
        contract_address="0xCONTRACT",
        claim_id=7,
    )

    assert result == AssessorOutcomeRecord(**outcome_row())
    statement, parameters = cursor.executions[0]
    assert "ORDER BY revision DESC" in statement
    assert parameters == (11_155_111, "0xcontract", 7)


def test_record_appends_a_locked_revision_without_model_or_chain_fields():
    cursor = FakeCursor(outcome_row())
    outcome_id = UUID("11111111-1111-4111-8111-111111111111")

    result = repository_for(cursor).record(
        chain_id=11_155_111,
        contract_address="0xCONTRACT",
        claim_id=7,
        outcome="Inconclusive",
        assessor_reference="research-assessor-1",
        notes="  Evidence does not support a binary conclusion.  ",
        outcome_id=outcome_id,
    )

    assert result.revision == 2
    assert len(cursor.executions) == 2
    assert "pg_advisory_xact_lock" in cursor.executions[0][0]
    assert "hashtextextended" in cursor.executions[0][0]
    assert cursor.executions[0][1] == ("11155111:0xcontract:7",)
    statement, parameters = cursor.executions[1]
    assert "COALESCE(MAX(revision), 0) + 1" in statement
    assert "claim_assessments" not in statement
    assert parameters == (
        outcome_id,
        11_155_111,
        "0xcontract",
        7,
        "Inconclusive",
        "research-assessor-1",
        "Evidence does not support a binary conclusion.",
        11_155_111,
        "0xcontract",
        7,
    )
