"""Unit tests for immutable coverage-decision proposal persistence."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from packages.integrations.postgres import (
    CoverageDecisionConflictError,
    CoverageDecisionProposalRecord,
    PostgresCoverageDecisionRepository,
    PostgresDatabase,
)

DECISION_ID = UUID("11111111-1111-4111-8111-111111111111")
OUTCOME_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeCursor:
    def __init__(self, rows) -> None:
        self.rows = list(rows)
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, parameters=None):
        self.executions.append((str(statement), parameters))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self._cursor


def proposal_row(**overrides):
    row = {
        "decision_id": DECISION_ID,
        "chain_id": 11_155_111,
        "contract_address": "0xabcdef",
        "claim_id": 7,
        "decision_status": "Approved",
        "decision_hash": "0x" + "77" * 32,
        "decision_maker_address": "0x" + "33" * 20,
        "proposed_by": "coverage-maker-1",
        "human_outcome_id": OUTCOME_ID,
        "human_outcome_revision": 2,
        "created_at": datetime(2026, 8, 18, tzinfo=UTC),
        "confirmed_transaction_hash": None,
        "confirmed_at": None,
    }
    row.update(overrides)
    return row


def repository_for(cursor: FakeCursor) -> PostgresCoverageDecisionRepository:
    database = PostgresDatabase(
        "postgresql://test",
        connect=lambda _url: FakeConnection(cursor),
    )
    return PostgresCoverageDecisionRepository(database)


def create(repository: PostgresCoverageDecisionRepository):
    return repository.create_or_get(
        decision_id=DECISION_ID,
        chain_id=11_155_111,
        contract_address="0xABCDEF",
        claim_id=7,
        decision_status="Approved",
        decision_hash="0x" + "77" * 32,
        decision_maker_address="0x" + "33" * 20,
        proposed_by="coverage-maker-1",
        human_outcome_id=OUTCOME_ID,
        human_outcome_revision=2,
    )


def test_create_normalizes_identity_and_returns_the_inserted_proposal():
    cursor = FakeCursor([proposal_row()])

    result = create(repository_for(cursor))

    assert result == CoverageDecisionProposalRecord(**proposal_row())
    statement, parameters = cursor.executions[0]
    assert "ON CONFLICT (chain_id, contract_address, claim_id) DO NOTHING" in statement
    assert parameters[2] == "0xabcdef"


def test_competing_proposal_is_rejected_instead_of_overwriting_audit_history():
    cursor = FakeCursor([None, proposal_row(decision_status="Rejected")])

    with pytest.raises(CoverageDecisionConflictError, match="different"):
        create(repository_for(cursor))

    assert len(cursor.executions) == 2
    assert "SELECT" in cursor.executions[1][0]
